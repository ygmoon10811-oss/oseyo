# -*- coding: utf-8 -*-
import os
import re
import hmac
import time
import uuid
import base64
import io
import json
import html
import sqlite3
import hashlib
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image

import gradio as gr
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import uvicorn


# =============================================================================
# 0) 설정
# =============================================================================
KST = timezone(timedelta(hours=9))
APP_NAME = "오세요"

# 세션/보안
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me").strip()
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "168"))  # 7일
COOKIE_NAME = "oseyo_session"

# 카카오/네이버 OAuth (선택)
KAKAO_CLIENT_ID = os.getenv("KAKAO_CLIENT_ID", "").strip()
KAKAO_CLIENT_SECRET = os.getenv("KAKAO_CLIENT_SECRET", "").strip()  # 선택
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "").strip()
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "").strip()
OAUTH_REDIRECT_BASE = os.getenv("OAUTH_REDIRECT_BASE", "").strip()  # 예: https://oseyo.onrender.com

# 카카오 지도/검색
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

# 휴대폰 인증 (DEV 모드 기본)
DEV_SMS = os.getenv("DEV_SMS", "1").strip() == "1"


def now_kst():
    return datetime.now(tz=KST)


def pick_db_path():
    candidates = ["/var/data", "/tmp"]
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            test = os.path.join(d, ".writetest")
            with open(test, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test)
            return os.path.join(d, "oseyo_final.db")
        except Exception:
            continue
    return "/tmp/oseyo_final.db"


DB_PATH = pick_db_path()
print(f"[DB] Using: {DB_PATH}")
print(f"[SMS] DEV_SMS={DEV_SMS}")


def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


# =============================================================================
# 1) DB 스키마
# =============================================================================
with db_conn() as con:
    def migrate_events_table():
    with db_conn() as con:
        cols = [r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()]
        # 구버전(9컬럼) -> 신버전(11컬럼)으로 확장
        if "owner_user_id" not in cols:
            con.execute("ALTER TABLE events ADD COLUMN owner_user_id TEXT DEFAULT ''")
        if "max_people" not in cols:
            con.execute("ALTER TABLE events ADD COLUMN max_people INTEGER DEFAULT 10")
        con.commit()

migrate_events_table()

    con.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE,
            pw_hash TEXT,
            name TEXT,
            birth TEXT,
            gender TEXT,
            phone TEXT,
            phone_verified INTEGER DEFAULT 0,
            created_at TEXT
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS oauth_accounts (
            provider TEXT,
            provider_user_id TEXT,
            user_id TEXT,
            PRIMARY KEY(provider, provider_user_id)
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT,
            expires_at TEXT
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS phone_codes (
            phone TEXT PRIMARY KEY,
            code TEXT,
            expires_at TEXT
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            owner_user_id TEXT,
            title TEXT,
            photo TEXT,
            start TEXT,
            end TEXT,
            addr TEXT,
            lat REAL,
            lng REAL,
            max_people INTEGER DEFAULT 10,
            created_at TEXT
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS participants (
            event_id TEXT,
            user_id TEXT,
            joined_at TEXT,
            PRIMARY KEY(event_id, user_id)
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS favs (
            name TEXT PRIMARY KEY,
            count INTEGER DEFAULT 1
        );
        """
    )
    con.commit()


# =============================================================================
# 2) 보안/비번/세션 헬퍼
# =============================================================================
def pbkdf2_hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return base64.b64encode(dk).decode("utf-8")


def make_password_hash(password: str) -> str:
    salt = uuid.uuid4().hex
    return f"pbkdf2_sha256${salt}${pbkdf2_hash(password, salt)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, hv = stored.split("$", 2)
        if algo != "pbkdf2_sha256":
            return False
        return hmac.compare_digest(pbkdf2_hash(password, salt), hv)
    except Exception:
        return False


def new_session(user_id: str) -> str:
    token = uuid.uuid4().hex + uuid.uuid4().hex
    expires = now_kst() + timedelta(hours=SESSION_TTL_HOURS)
    with db_conn() as con:
        con.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
                    (token, user_id, expires.isoformat()))
        con.commit()
    return token


def get_user_by_session(token: str):
    if not token:
        return None
    with db_conn() as con:
        row = con.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token=?",
            (token,)
        ).fetchone()
        if not row:
            return None
        user_id, expires_at = row
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=KST)
        except Exception:
            return None
        if exp < now_kst():
            con.execute("DELETE FROM sessions WHERE token=?", (token,))
            con.commit()
            return None

        u = con.execute(
            "SELECT id, username, name, birth, gender, phone, phone_verified FROM users WHERE id=?",
            (user_id,)
        ).fetchone()
        if not u:
            return None
        return {
            "id": u[0],
            "username": u[1],
            "name": u[2],
            "birth": u[3],
            "gender": u[4],
            "phone": u[5],
            "phone_verified": int(u[6] or 0),
        }


def require_user(request: Request):
    token = request.cookies.get(COOKIE_NAME, "")
    return get_user_by_session(token)


# =============================================================================
# 3) FastAPI 앱 + 인증 미들웨어
# =============================================================================
app = FastAPI()


PUBLIC_PATH_PREFIXES = (
    "/login",
    "/signup",
    "/logout",
    "/oauth",
    "/api/public",
    "/health",
)


PROTECTED_PATH_PREFIXES = (
    "/app",
    "/explore",
    "/map",
    "/api",
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # public
    for p in PUBLIC_PATH_PREFIXES:
        if path.startswith(p):
            return await call_next(request)

    # protected
    if any(path.startswith(p) for p in PROTECTED_PATH_PREFIXES):
        user = require_user(request)
        if not user:
            return RedirectResponse(url="/login", status_code=302)

    return await call_next(request)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root(request: Request):
    user = require_user(request)
    if user:
        return RedirectResponse(url="/app", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


# =============================================================================
# 4) 로그인/회원가입/휴대폰 인증 (HTML 폼)
# =============================================================================
BASE_CSS = """
<style>
body { font-family: ui-sans-serif, system-ui, -apple-system; background:#FAF9F6; margin:0; }
.wrap { max-width: 420px; margin: 0 auto; padding: 24px; }
.card { background: white; border:1px solid #eee; border-radius:16px; padding:18px; box-shadow:0 8px 24px rgba(0,0,0,0.06); }
h1 { margin: 12px 0 16px; font-size: 24px; }
label { display:block; font-size:13px; margin:10px 0 6px; color:#444; }
input, select { width:100%; padding:12px; border-radius:12px; border:1px solid #ddd; font-size:14px; }
button { width:100%; padding:12px; border-radius:12px; border:none; background:#ff6b00; color:white; font-weight:800; font-size:15px; cursor:pointer; margin-top:14px; }
.muted { color:#666; font-size:13px; margin-top:10px; }
.row { display:flex; gap:10px; }
.row > * { flex:1; }
.hr { height:1px; background:#eee; margin:18px 0; }
.btn2 { background:#111; }
.btn3 { background:#03C75A; }
.btn4 { background:#FEE500; color:#111; }
.small { font-size:12px; color:#777; margin-top:6px; }
.err { color:#c00; font-weight:700; margin:10px 0 0; }
.ok { color:#0a7; font-weight:700; margin:10px 0 0; }
a { color:#ff6b00; text-decoration:none; font-weight:800; }
</style>
"""


def page(title: str, body_html: str):
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'/>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'/>"
        f"<title>{html.escape(title)}</title>{BASE_CSS}</head><body>{body_html}</body></html>"
    )


def valid_username(u: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_]{4,20}", u or ""))


def valid_phone(p: str) -> bool:
    return bool(re.fullmatch(r"01[016789]\d{7,8}", (p or "").replace("-", "")))


@app.get("/login")
def login_page(request: Request, msg: str = ""):
    kakao_ok = bool(KAKAO_CLIENT_ID and OAUTH_REDIRECT_BASE)
    naver_ok = bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET and OAUTH_REDIRECT_BASE)

    oauth_buttons = ""
    if kakao_ok:
        oauth_buttons += f"<button class='btn4' onclick=\"location.href='/oauth/kakao/start'\">카카오로 계속</button>"
    else:
        oauth_buttons += "<div class='small'>카카오 간편가입/로그인은 환경변수(KAKAO_CLIENT_ID, OAUTH_REDIRECT_BASE) 설정 후 활성화됨</div>"

    if naver_ok:
        oauth_buttons += f"<button class='btn3' onclick=\"location.href='/oauth/naver/start'\">네이버로 계속</button>"
    else:
        oauth_buttons += "<div class='small'>네이버 간편가입/로그인은 환경변수(NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, OAUTH_REDIRECT_BASE) 설정 후 활성화됨</div>"

    body = f"""
    <div class="wrap">
      <h1>{APP_NAME} 로그인</h1>
      <div class="card">
        {f"<div class='err'>{html.escape(msg)}</div>" if msg else ""}
        <form method="post" action="/login">
          <label>아이디</label>
          <input name="username" placeholder="아이디" autocomplete="username" />
          <label>비밀번호</label>
          <input name="password" type="password" placeholder="비밀번호" autocomplete="current-password" />
          <button type="submit">로그인</button>
        </form>
        <div class="muted">계정이 없으면 <a href="/signup">회원가입</a></div>
        <div class="hr"></div>
        {oauth_buttons}
      </div>
    </div>
    """
    return page(f"{APP_NAME} 로그인", body)


@app.post("/login")
def login_action(username: str = Form(...), password: str = Form(...)):
    username = (username or "").strip()
    password = (password or "").strip()

    with db_conn() as con:
        row = con.execute(
            "SELECT id, pw_hash FROM users WHERE username=?",
            (username,)
        ).fetchone()

    if not row:
        return RedirectResponse(url="/login?msg=아이디/비밀번호를+확인해+주세요", status_code=302)

    user_id, pw_hash = row
    if not pw_hash or not verify_password(password, pw_hash):
        return RedirectResponse(url="/login?msg=아이디/비밀번호를+확인해+주세요", status_code=302)

    token = new_session(user_id)
    resp = RedirectResponse(url="/app", status_code=302)
    resp.set_cookie(COOKIE_NAME, token, httponly=True, secure=False, samesite="lax", max_age=SESSION_TTL_HOURS * 3600)
    return resp


@app.get("/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME, "")
    if token:
        with db_conn() as con:
            con.execute("DELETE FROM sessions WHERE token=?", (token,))
            con.commit()
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


@app.get("/signup")
def signup_page(msg: str = ""):
    body = f"""
    <div class="wrap">
      <h1>{APP_NAME} 회원가입</h1>
      <div class="card">
        {f"<div class='err'>{html.escape(msg)}</div>" if msg else ""}

        <form method="post" action="/signup">
          <label>이름</label>
          <input name="name" placeholder="이름" />

          <div class="row">
            <div>
              <label>생년월일</label>
              <input name="birth" placeholder="YYYY-MM-DD" />
            </div>
            <div>
              <label>성별</label>
              <select name="gender">
                <option value="F">여</option>
                <option value="M">남</option>
                <option value="N">선택안함</option>
              </select>
            </div>
          </div>

          <label>아이디 (영문/숫자/_ 4~20자)</label>
          <input name="username" placeholder="userid" autocomplete="username" />

          <label>비밀번호</label>
          <input name="password" type="password" placeholder="비밀번호" autocomplete="new-password" />

          <label>비밀번호 확인</label>
          <input name="password2" type="password" placeholder="비밀번호 확인" autocomplete="new-password" />

          <label>휴대폰 번호 (숫자만)</label>
          <input name="phone" placeholder="01012345678" />

          <div class="row">
            <button class="btn2" type="button" onclick="sendCode()">인증번호 받기</button>
            <button class="btn2" type="button" onclick="verifyCode()">인증 확인</button>
          </div>

          <label>인증번호</label>
          <input id="code" placeholder="6자리" />

          <input type="hidden" name="phone_verified" id="phone_verified" value="0" />

          <button type="submit">회원가입 완료</button>
        </form>

        <div class="muted">이미 계정이 있으면 <a href="/login">로그인</a></div>
        <div class="small">* DEV 모드에서는 인증번호가 서버 로그에 출력됨</div>
      </div>
    </div>

    <script>
      async function sendCode() {{
        const phone = document.querySelector('input[name="phone"]').value.trim();
        const r = await fetch('/api/public/send_code', {{
          method:'POST',
          headers:{{'Content-Type':'application/json'}},
          body: JSON.stringify({{phone}})
        }});
        const j = await r.json();
        alert(j.message || '전송 처리됨');
      }}
      async function verifyCode() {{
        const phone = document.querySelector('input[name="phone"]').value.trim();
        const code = document.getElementById('code').value.trim();
        const r = await fetch('/api/public/verify_code', {{
          method:'POST',
          headers:{{'Content-Type':'application/json'}},
          body: JSON.stringify({{phone, code}})
        }});
        const j = await r.json();
        if (j.ok) {{
          document.getElementById('phone_verified').value = '1';
          alert('인증 완료');
        }} else {{
          alert(j.message || '인증 실패');
        }}
      }}
    </script>
    """
    return page(f"{APP_NAME} 회원가입", body)


@app.post("/signup")
def signup_action(
    name: str = Form(...),
    birth: str = Form(...),
    gender: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    phone: str = Form(...),
    phone_verified: str = Form("0"),
):
    name = (name or "").strip()
    birth = (birth or "").strip()
    gender = (gender or "N").strip().upper()
    username = (username or "").strip()
    password = (password or "").strip()
    password2 = (password2 or "").strip()
    phone = (phone or "").replace("-", "").strip()

    if not name:
        return RedirectResponse(url="/signup?msg=이름을+입력해+주세요", status_code=302)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", birth):
        return RedirectResponse(url="/signup?msg=생년월일은+YYYY-MM-DD+형식입니다", status_code=302)
    if gender not in ("F", "M", "N"):
        gender = "N"
    if not valid_username(username):
        return RedirectResponse(url="/signup?msg=아이디+형식을+확인해+주세요", status_code=302)
    if len(password) < 6:
        return RedirectResponse(url="/signup?msg=비밀번호는+6자+이상+권장", status_code=302)
    if password != password2:
        return RedirectResponse(url="/signup?msg=비밀번호+확인이+일치하지+않습니다", status_code=302)
    if not valid_phone(phone):
        return RedirectResponse(url="/signup?msg=휴대폰+번호를+확인해+주세요", status_code=302)
    if phone_verified != "1":
        return RedirectResponse(url="/signup?msg=휴대폰+인증을+완료해+주세요", status_code=302)

    user_id = uuid.uuid4().hex
    pw_hash = make_password_hash(password)

    try:
        with db_conn() as con:
            con.execute(
                "INSERT INTO users (id, username, pw_hash, name, birth, gender, phone, phone_verified, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, username, pw_hash, name, birth, gender, phone, 1, now_kst().isoformat()),
            )
            con.commit()
    except sqlite3.IntegrityError:
        return RedirectResponse(url="/signup?msg=이미+사용중인+아이디입니다", status_code=302)

    token = new_session(user_id)
    resp = RedirectResponse(url="/app", status_code=302)
    resp.set_cookie(COOKIE_NAME, token, httponly=True, secure=False, samesite="lax", max_age=SESSION_TTL_HOURS * 3600)
    return resp


# =============================================================================
# 5) 휴대폰 인증 API (PUBLIC)
# =============================================================================
@app.post("/api/public/send_code")
async def api_send_code(payload: dict):
    phone = (payload.get("phone") or "").replace("-", "").strip()
    if not valid_phone(phone):
        return JSONResponse({"ok": False, "message": "휴대폰 번호 형식이 올바르지 않습니다"}, status_code=400)

    code = f"{int(time.time()) % 1000000:06d}"
    exp = now_kst() + timedelta(minutes=5)

    with db_conn() as con:
        con.execute(
            "INSERT INTO phone_codes (phone, code, expires_at) VALUES (?,?,?) "
            "ON CONFLICT(phone) DO UPDATE SET code=excluded.code, expires_at=excluded.expires_at",
            (phone, code, exp.isoformat()),
        )
        con.commit()

    if DEV_SMS:
        print(f"[DEV_SMS] phone={phone}, code={code} (expires {exp.isoformat()})")
        return {"ok": True, "message": "DEV 모드: 인증번호가 서버 로그에 출력되었습니다"}

    # TODO: 실제 SMS 발송(예: Nurigo/CoolSMS/Twilio) 연동 지점
    return {"ok": True, "message": "인증번호 전송을 처리했습니다"}


@app.post("/api/public/verify_code")
async def api_verify_code(payload: dict):
    phone = (payload.get("phone") or "").replace("-", "").strip()
    code = (payload.get("code") or "").strip()

    if not valid_phone(phone) or not re.fullmatch(r"\d{6}", code):
        return JSONResponse({"ok": False, "message": "입력값을 확인해 주세요"}, status_code=400)

    with db_conn() as con:
        row = con.execute("SELECT code, expires_at FROM phone_codes WHERE phone=?", (phone,)).fetchone()
        if not row:
            return {"ok": False, "message": "인증번호를 먼저 요청해 주세요"}
        saved_code, expires_at = row
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=KST)
        except Exception:
            return {"ok": False, "message": "인증정보가 손상되었습니다"}

        if exp < now_kst():
            return {"ok": False, "message": "인증번호가 만료되었습니다"}
        if saved_code != code:
            return {"ok": False, "message": "인증번호가 일치하지 않습니다"}

        # 통과
        con.execute("DELETE FROM phone_codes WHERE phone=?", (phone,))
        con.commit()

    return {"ok": True, "message": "인증 완료"}


# =============================================================================
# 6) OAuth: 카카오/네이버 (간편가입/로그인)
# =============================================================================
def oauth_redirect_uri(provider: str) -> str:
    # 반드시 Render의 실제 도메인으로 지정 필요
    return f"{OAUTH_REDIRECT_BASE}/oauth/{provider}/callback"


@app.get("/oauth/kakao/start")
def kakao_start():
    if not (KAKAO_CLIENT_ID and OAUTH_REDIRECT_BASE):
        return RedirectResponse(url="/login?msg=카카오+OAuth+설정이+필요합니다", status_code=302)

    state = uuid.uuid4().hex
    url = (
        "https://kauth.kakao.com/oauth/authorize"
        f"?response_type=code&client_id={KAKAO_CLIENT_ID}"
        f"&redirect_uri={oauth_redirect_uri('kakao')}"
        f"&state={state}"
    )
    return RedirectResponse(url=url, status_code=302)


@app.get("/oauth/kakao/callback")
def kakao_callback(code: str = "", state: str = ""):
    if not code:
        return RedirectResponse(url="/login?msg=카카오+인증이+취소되었습니다", status_code=302)

    # 토큰 교환
    token_url = "https://kauth.kakao.com/oauth/token"
    data = {
        "grant_type": "authorization_code",
        "client_id": KAKAO_CLIENT_ID,
        "redirect_uri": oauth_redirect_uri("kakao"),
        "code": code,
    }
    if KAKAO_CLIENT_SECRET:
        data["client_secret"] = KAKAO_CLIENT_SECRET

    r = requests.post(token_url, data=data, timeout=15)
    tj = r.json()
    access_token = tj.get("access_token")
    if not access_token:
        return RedirectResponse(url="/login?msg=카카오+토큰+교환+실패", status_code=302)

    # 유저 정보
    ur = requests.get(
        "https://kapi.kakao.com/v2/user/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    uj = ur.json()
    kakao_id = str(uj.get("id") or "")
    if not kakao_id:
        return RedirectResponse(url="/login?msg=카카오+사용자+정보+조회+실패", status_code=302)

    user_id = None
    with db_conn() as con:
        row = con.execute(
            "SELECT user_id FROM oauth_accounts WHERE provider=? AND provider_user_id=?",
            ("kakao", kakao_id),
        ).fetchone()
        if row:
            user_id = row[0]
        else:
            # 신규 유저 생성 (폰인증은 추후 추가로 하게 할 수도 있음)
            user_id = uuid.uuid4().hex
            username = f"kakao_{kakao_id}"
            name = (uj.get("properties") or {}).get("nickname") or "카카오회원"
            con.execute(
                "INSERT INTO users (id, username, pw_hash, name, birth, gender, phone, phone_verified, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, username, "", name, "", "N", "", 0, now_kst().isoformat()),
            )
            con.execute(
                "INSERT INTO oauth_accounts (provider, provider_user_id, user_id) VALUES (?,?,?)",
                ("kakao", kakao_id, user_id),
            )
            con.commit()

    token = new_session(user_id)
    resp = RedirectResponse(url="/app", status_code=302)
    resp.set_cookie(COOKIE_NAME, token, httponly=True, secure=False, samesite="lax", max_age=SESSION_TTL_HOURS * 3600)
    return resp


@app.get("/oauth/naver/start")
def naver_start():
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET and OAUTH_REDIRECT_BASE):
        return RedirectResponse(url="/login?msg=네이버+OAuth+설정이+필요합니다", status_code=302)

    state = uuid.uuid4().hex
    url = (
        "https://nid.naver.com/oauth2.0/authorize"
        f"?response_type=code&client_id={NAVER_CLIENT_ID}"
        f"&redirect_uri={oauth_redirect_uri('naver')}"
        f"&state={state}"
    )
    return RedirectResponse(url=url, status_code=302)


@app.get("/oauth/naver/callback")
def naver_callback(code: str = "", state: str = ""):
    if not code:
        return RedirectResponse(url="/login?msg=네이버+인증이+취소되었습니다", status_code=302)

    token_url = "https://nid.naver.com/oauth2.0/token"
    params = {
        "grant_type": "authorization_code",
        "client_id": NAVER_CLIENT_ID,
        "client_secret": NAVER_CLIENT_SECRET,
        "code": code,
        "state": state,
    }
    r = requests.get(token_url, params=params, timeout=15)
    tj = r.json()
    access_token = tj.get("access_token")
    if not access_token:
        return RedirectResponse(url="/login?msg=네이버+토큰+교환+실패", status_code=302)

    ur = requests.get(
        "https://openapi.naver.com/v1/nid/me",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    uj = ur.json()
    resp = uj.get("response") or {}
    naver_id = str(resp.get("id") or "")
    if not naver_id:
        return RedirectResponse(url="/login?msg=네이버+사용자+정보+조회+실패", status_code=302)

    user_id = None
    with db_conn() as con:
        row = con.execute(
            "SELECT user_id FROM oauth_accounts WHERE provider=? AND provider_user_id=?",
            ("naver", naver_id),
        ).fetchone()
        if row:
            user_id = row[0]
        else:
            user_id = uuid.uuid4().hex
            username = f"naver_{naver_id}"
            name = resp.get("name") or resp.get("nickname") or "네이버회원"
            con.execute(
                "INSERT INTO users (id, username, pw_hash, name, birth, gender, phone, phone_verified, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (user_id, username, "", name, "", "N", "", 0, now_kst().isoformat()),
            )
            con.execute(
                "INSERT INTO oauth_accounts (provider, provider_user_id, user_id) VALUES (?,?,?)",
                ("naver", naver_id, user_id),
            )
            con.commit()

    token = new_session(user_id)
    resp2 = RedirectResponse(url="/app", status_code=302)
    resp2.set_cookie(COOKIE_NAME, token, httponly=True, secure=False, samesite="lax", max_age=SESSION_TTL_HOURS * 3600)
    return resp2


# =============================================================================
# 7) 이벤트/참여 API (로그인 필요)
# =============================================================================
def parse_dt(s: str):
    s = (s or "").strip()
    if not s:
        return None
    # "YYYY-MM-DD HH:MM"
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        return dt
    except Exception:
        return None


def participant_count(con, event_id: str) -> int:
    row = con.execute("SELECT COUNT(*) FROM participants WHERE event_id=?", (event_id,)).fetchone()
    return int(row[0] or 0)


@app.post("/api/events/{event_id}/toggle")
def api_toggle(event_id: str, request: Request):
    user = require_user(request)
    uid = user["id"]

    with db_conn() as con:
        ev = con.execute(
            "SELECT id, owner_user_id, max_people, end FROM events WHERE id=?",
            (event_id,)
        ).fetchone()
        if not ev:
            return JSONResponse({"ok": False, "message": "이벤트가 없습니다"}, status_code=404)

        _, owner_id, max_people, end_s = ev
        end_dt = parse_dt(end_s) if end_s else None
        if end_dt and end_dt < now_kst():
            return JSONResponse({"ok": False, "message": "종료된 이벤트입니다"}, status_code=400)

        joined = con.execute(
            "SELECT 1 FROM participants WHERE event_id=? AND user_id=?",
            (event_id, uid)
        ).fetchone() is not None

        if joined:
            con.execute("DELETE FROM participants WHERE event_id=? AND user_id=?", (event_id, uid))
            con.commit()
            cnt = participant_count(con, event_id)
            return {"ok": True, "joined": False, "count": cnt, "max": int(max_people or 0)}

        # join
        cnt = participant_count(con, event_id)
        max_people = int(max_people or 0)
        if max_people > 0 and cnt >= max_people:
            return {"ok": False, "message": "정원이 가득 찼습니다", "joined": False, "count": cnt, "max": max_people}

        con.execute(
            "INSERT OR IGNORE INTO participants (event_id, user_id, joined_at) VALUES (?,?,?)",
            (event_id, uid, now_kst().isoformat())
        )
        con.commit()
        cnt2 = participant_count(con, event_id)
        return {"ok": True, "joined": True, "count": cnt2, "max": max_people}


@app.post("/api/events/{event_id}/delete")
def api_delete_event(event_id: str, request: Request):
    user = require_user(request)
    uid = user["id"]

    with db_conn() as con:
        ev = con.execute("SELECT owner_user_id FROM events WHERE id=?", (event_id,)).fetchone()
        if not ev:
            return JSONResponse({"ok": False, "message": "이벤트가 없습니다"}, status_code=404)
        owner_id = ev[0]
        if owner_id != uid:
            return JSONResponse({"ok": False, "message": "삭제 권한이 없습니다"}, status_code=403)

        con.execute("DELETE FROM participants WHERE event_id=?", (event_id,))
        con.execute("DELETE FROM events WHERE id=?", (event_id,))
        con.commit()

    return {"ok": True, "message": "삭제 완료"}


# =============================================================================
# 8) 탐색 페이지(HTML+JS) / 지도 페이지(카카오맵+JS)
# =============================================================================
def explore_page_html(user):
    uid = user["id"]
    now_s = now_kst().strftime("%Y-%m-%d %H:%M")

    with db_conn() as con:
        rows = con.execute(
            """
            SELECT id, owner_user_id, title, photo, start, end, addr, max_people
            FROM events
            WHERE (end IS NULL OR end = '' OR end > ?)
            ORDER BY created_at DESC
            """,
            (now_s,)
        ).fetchall()

        items = []
        for (eid, owner_id, title, photo, start, end, addr, max_people) in rows:
            cnt = participant_count(con, eid)
            joined = con.execute(
                "SELECT 1 FROM participants WHERE event_id=? AND user_id=?",
                (eid, uid)
            ).fetchone() is not None
            items.append({
                "id": eid,
                "owner": owner_id,
                "title": title or "",
                "photo": photo or "",
                "start": start or "",
                "end": end or "",
                "addr": addr or "",
                "count": int(cnt),
                "max": int(max_people or 0),
                "joined": bool(joined),
            })

    # 카드 렌더
    cards = ""
    for it in items:
        img_html = ""
        if it["photo"]:
            img_html = f"<img class='ph' src='data:image/jpeg;base64,{it['photo']}' />"
        else:
            img_html = "<div class='ph ph2'></div>"

        is_owner = (it["owner"] == uid)
        full = (it["max"] > 0 and it["count"] >= it["max"] and not it["joined"])

        join_label = "빠지기" if it["joined"] else "참여하기"
        join_disabled = "disabled" if full else ""
        del_btn = f"<button class='del' onclick=\"delEv('{it['id']}')\">삭제</button>" if is_owner else ""

        max_txt = f"/ {it['max']}" if it["max"] > 0 else ""
        cards += f"""
        <div class="card">
          <div class="info">
            <div class="t">{html.escape(it["title"])}</div>
            <div class="m">📅 {html.escape(it["start"])} ~ {html.escape(it["end"])}</div>
            <div class="m">📍 {html.escape(it["addr"])}</div>
            <div class="m"><b>👥 <span id="cnt-{it['id']}">{it['count']}</span>{max_txt}</b></div>
            <div class="btnrow">
              <button class="join" id="btn-{it['id']}" {join_disabled}
                onclick="toggleJoin('{it['id']}')">{join_label}</button>
              {del_btn}
            </div>
          </div>
          {img_html}
        </div>
        """

    if not cards:
        cards = "<div class='empty'>등록된 이벤트가 없습니다.</div>"

    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body {{ font-family: ui-sans-serif, system-ui; background:#FAF9F6; margin:0; }}
    .wrap {{ padding: 14px; max-width: 820px; margin: 0 auto; }}
    .top {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin: 10px 0 14px; }}
    .h {{ font-size:18px; font-weight:900; }}
    .small {{ font-size:12px; color:#666; }}
    .card {{
      background: rgba(255,255,255,0.9);
      border: 1px solid #e8e8e8;
      border-radius: 16px;
      padding: 14px;
      margin-bottom: 12px;
      display: grid;
      grid-template-columns: 1fr 120px;
      gap: 12px;
      align-items: center;
    }}
    .t {{ font-size: 17px; font-weight: 900; color:#111; margin-bottom:6px; }}
    .m {{ font-size: 13px; color:#666; margin: 3px 0; }}
    .ph {{ width:120px; height:120px; border-radius: 12px; border:1px solid #ddd; object-fit: cover; }}
    .ph2 {{ background:#e0e0e0; }}
    .btnrow {{ display:flex; gap:8px; margin-top:10px; }}
    button {{ border:none; border-radius: 12px; padding: 10px 12px; font-weight:900; cursor:pointer; }}
    .join {{ background:#ff6b00; color:white; }}
    .join:disabled {{ background:#ccc; cursor:not-allowed; }}
    .del {{ background:#111; color:white; }}
    .empty {{ text-align:center; padding: 60px 0; color:#999; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <div class="h">탐색</div>
        <div class="small">로그인: {html.escape(user["username"])} · 종료된 이벤트는 자동 제외</div>
      </div>
      <div class="small"><a href="/logout" style="color:#ff6b00;font-weight:900;text-decoration:none;">로그아웃</a></div>
    </div>

    {cards}
  </div>

<script>
async function toggleJoin(id) {{
  const btn = document.getElementById('btn-' + id);
  btn.disabled = true;
  const r = await fetch('/api/events/' + id + '/toggle', {{ method:'POST' }});
  const j = await r.json();
  if (!j.ok) {{
    alert(j.message || '처리 실패');
    btn.disabled = false;
    return;
  }}
  const cnt = document.getElementById('cnt-' + id);
  cnt.textContent = j.count;

  // 버튼 토글
  if (j.joined) {{
    btn.textContent = '빠지기';
    btn.disabled = false;
  }} else {{
    btn.textContent = '참여하기';
    // 정원 꽉 참이면 비활성
    if (j.max > 0 && j.count >= j.max) btn.disabled = true;
    else btn.disabled = false;
  }}
}}

async function delEv(id) {{
  if (!confirm('이 이벤트를 삭제할까요?')) return;
  const r = await fetch('/api/events/' + id + '/delete', {{ method:'POST' }});
  const j = await r.json();
  if (!j.ok) {{
    alert(j.message || '삭제 실패');
    return;
  }}
  location.reload();
}}
</script>
</body>
</html>
    """


@app.get("/explore")
def explore(request: Request):
    user = require_user(request)
    return HTMLResponse(explore_page_html(user))


@app.get("/map")
def map_h(request: Request):
    user = require_user(request)
    uid = user["id"]

    now_s = now_kst().strftime("%Y-%m-%d %H:%M")
    with db_conn() as con:
        rows = con.execute(
            """
            SELECT id, owner_user_id, title, photo, lat, lng, addr, start, end, max_people
            FROM events
            WHERE (end IS NULL OR end = '' OR end > ?)
            ORDER BY created_at DESC
            """,
            (now_s,)
        ).fetchall()

        payload = []
        for (eid, owner_id, title, photo, lat, lng, addr, start, end, max_people) in rows:
            try:
                lat = float(lat) if lat is not None else 0.0
                lng = float(lng) if lng is not None else 0.0
            except Exception:
                lat, lng = 0.0, 0.0
            cnt = participant_count(con, eid)
            joined = con.execute(
                "SELECT 1 FROM participants WHERE event_id=? AND user_id=?",
                (eid, uid)
            ).fetchone() is not None
            payload.append({
                "id": eid,
                "owner": owner_id,
                "title": title or "",
                "photo": photo or "",
                "lat": lat,
                "lng": lng,
                "addr": addr or "",
                "start": start or "",
                "end": end or "",
                "count": int(cnt),
                "max": int(max_people or 0),
                "joined": bool(joined),
            })

    center_lat, center_lng = 37.56, 126.97
    if payload and payload[0]["lat"] and payload[0]["lng"]:
        center_lat, center_lng = payload[0]["lat"], payload[0]["lng"]

    if not KAKAO_JAVASCRIPT_KEY:
        return HTMLResponse("<div style='padding:24px;'>⚠️ KAKAO_JAVASCRIPT_KEY 환경변수 필요</div>")

    return HTMLResponse(f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    html, body, #m {{ width:100%; height:100%; margin:0; padding:0; }}
    .iw-wrap {{ width:260px; padding:12px; font-family: ui-sans-serif, system-ui; }}
    .iw-title {{ font-weight:900; font-size:14px; margin:0 0 8px 0; }}
    .iw-meta {{ font-size:12px; color:#666; margin:4px 0; }}
    .iw-img {{ width:100%; height:120px; object-fit:cover; border-radius:10px; margin:8px 0; border:1px solid #ddd; }}
    .btnrow {{ display:flex; gap:8px; margin-top:10px; }}
    .btn {{ border:none; border-radius:12px; padding:10px 12px; font-weight:900; cursor:pointer; }}
    .join {{ background:#ff6b00; color:white; }}
    .join[disabled] {{ background:#ccc; cursor:not-allowed; }}
    .del {{ background:#111; color:white; }}
  </style>
</head>
<body>
  <div id="m"></div>
  <script src="//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}"></script>
  <script>
    const uid = "{uid}";
    const data = {json.dumps(payload)};
    const map = new kakao.maps.Map(document.getElementById('m'), {{
      center: new kakao.maps.LatLng({center_lat}, {center_lng}),
      level: 7
    }});

    let openInfo = null;

    async function toggleJoin(id) {{
      const r = await fetch('/api/events/' + id + '/toggle', {{ method:'POST' }});
      const j = await r.json();
      if (!j.ok) {{
        alert(j.message || '처리 실패');
        return;
      }}
      // 인포윈도우 내부 DOM 업데이트는 단순화를 위해 reload로 처리
      location.reload();
    }}

    async function delEv(id) {{
      if (!confirm('이 이벤트를 삭제할까요?')) return;
      const r = await fetch('/api/events/' + id + '/delete', {{ method:'POST' }});
      const j = await r.json();
      if (!j.ok) {{
        alert(j.message || '삭제 실패');
        return;
      }}
      location.reload();
    }}

    data.forEach(ev => {{
      if (!ev.lat || !ev.lng) return;

      const marker = new kakao.maps.Marker({{
        map: map,
        position: new kakao.maps.LatLng(ev.lat, ev.lng),
        title: ev.title
      }});

      let imgHtml = "";
      if (ev.photo) {{
        imgHtml = `<img class="iw-img" src="data:image/jpeg;base64,${{ev.photo}}" />`;
      }}

      const maxTxt = (ev.max && ev.max > 0) ? ` / ${{ev.max}}` : '';
      const isOwner = (ev.owner === uid);
      const full = (ev.max && ev.max > 0 && ev.count >= ev.max && !ev.joined);
      const btnLabel = ev.joined ? '빠지기' : '참여하기';
      const dis = full ? 'disabled' : '';

      const delBtn = isOwner ? `<button class="btn del" onclick="delEv('${{ev.id}}')">삭제</button>` : '';

      const content = `
        <div class="iw-wrap">
          <div class="iw-title">${{ev.title}}</div>
          ${{imgHtml}}
          <div class="iw-meta">📅 ${{ev.start}} ~ ${{ev.end}}</div>
          <div class="iw-meta">📍 ${{ev.addr}}</div>
          <div class="iw-meta"><b>👥 ${{ev.count}}${{maxTxt}}</b></div>
          <div class="btnrow">
            <button class="btn join" ${{dis}} onclick="toggleJoin('${{ev.id}}')">${{btnLabel}}</button>
            ${{delBtn}}
          </div>
        </div>
      `;

      const infowindow = new kakao.maps.InfoWindow({{
        content: content,
        removable: true
      }});

      kakao.maps.event.addListener(marker, 'click', function() {{
        if (openInfo) openInfo.close();
        infowindow.open(map, marker);
        openInfo = infowindow;
      }});
    }});
  </script>
</body>
</html>
    """)


# =============================================================================
# 9) Gradio 앱 (로그인한 사용자만 /app에서 접근)
#    - 이벤트 생성 모달 + 즐겨찾기 + 주소 검색 + iframe(탐색/지도)
# =============================================================================
CSS_GRADIO = """
html, body { margin: 0 !important; padding: 0 !important; overflow-x: hidden !important; }
.gradio-container { max-width: 100% !important; padding-bottom: 100px !important; }

/* FAB */
.fab-wrapper {
  position: fixed !important;
  right: 30px !important;
  bottom: 30px !important;
  z-index: 9999 !important;
  width: auto !important;
  height: auto !important;
}
.fab-wrapper button {
  width: 65px !important;
  height: 65px !important;
  min-width: 65px !important;
  min-height: 65px !important;
  border-radius: 50% !important;
  background: #ff6b00 !important;
  color: white !important;
  font-size: 40px !important;
  border: none !important;
  box-shadow: 0 4px 15px rgba(0,0,0,0.4) !important;
  cursor: pointer !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  line-height: 1 !important;
}

/* overlay/modal */
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 10000; }

.main-modal {
  position: fixed !important;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 92vw;
  max-width: 520px;
  height: 86vh;
  background: white;
  z-index: 10001;
  border-radius: 24px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.4);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.modal-header {
  padding: 20px;
  border-bottom: 2px solid #eee;
  font-weight: 900;
  font-size: 20px;
  flex-shrink: 0;
}
.modal-body {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.modal-footer {
  padding: 16px 20px;
  border-top: 2px solid #eee;
  background: #f9f9f9;
  display: flex;
  gap: 10px;
  flex-shrink: 0;
}
.modal-footer button { flex: 1; padding: 12px; border-radius: 12px; font-weight: 900; }

/* sub modal */
.sub-modal {
  position: fixed !important;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 88vw;
  max-width: 420px;
  max-height: 70vh;
  background: white;
  z-index: 10005;
  border-radius: 20px;
  box-shadow: 0 12px 40px rgba(0,0,0,0.4);
  overflow: hidden;
}
.sub-body { height: 100%; overflow-y: auto; padding: 20px; }

/* fav grid */
.fav-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  overflow: visible;
}
.fav-grid button {
  font-size: 13px;
  padding: 10px 8px;
  border-radius: 10px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ✅ title textarea fixed */
#event_title textarea {
  max-height: 120px !important;
  overflow-y: auto !important;
  resize: none !important;
  line-height: 1.4 !important;
}
#event_title { flex: 0 0 auto !important; }

/* ✅ image never collapses */
#event_photo {
  flex: 0 0 auto !important;
  min-height: 240px !important;
  display: block !important;
}
#event_photo > * { min-height: 240px !important; }
#event_photo * { box-sizing: border-box !important; }
"""


def top10_favs_updates():
    with db_conn() as con:
        rows = con.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 10").fetchall()
    updates = [gr.update(visible=False, value="")] * 10
    for i, r in enumerate(rows):
        updates[i] = gr.update(visible=True, value=r[0])
    return updates


def save_event(owner_user_id: str, title, img, start, end, addr_obj, max_people):
    title = (title or "").strip()
    if not title:
        return False, "제목을 입력해 주세요"

    # 날짜 검증
    sdt = parse_dt(start)
    edt = parse_dt(end)
    if not sdt or not edt:
        return False, "시작/종료일시 형식을 확인해 주세요 (YYYY-MM-DD HH:MM)"
    if edt <= sdt:
        return False, "종료일시는 시작일시보다 이후여야 합니다"

    try:
        max_people = int(max_people)
    except Exception:
        max_people = 10
    if max_people < 1:
        max_people = 1
    if max_people > 999:
        max_people = 999

    if addr_obj is None:
        addr_obj = {}

    pic_b64 = ""
    if img is not None:
        try:
            im = Image.fromarray(img).convert("RGB")
            im.thumbnail((800, 800))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            pic_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception:
            pic_b64 = ""

    addr_name = (addr_obj.get("name") or "").strip()
    lat = addr_obj.get("y") or 0
    lng = addr_obj.get("x") or 0
    try:
        lat = float(lat)
        lng = float(lng)
    except Exception:
        lat, lng = 0.0, 0.0

    eid = uuid.uuid4().hex[:10]
    with db_conn() as con:
        con.execute(
    """
    INSERT INTO events
    (id, owner_user_id, title, photo, start, end, addr, lat, lng, max_people, created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """,
    (
        eid,
        owner_user_id,
        title,
        pic_b64,
        start,
        end,
        addr_name,
        lat,
        lng,
        max_people,
        now_kst().strftime("%Y-%m-%d %H:%M:%S"),
    ),
)

        con.execute(
            "INSERT INTO favs (name, count) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET count = count + 1",
            (title,),
        )
        con.commit()

    return True, "✅ 이벤트가 생성되었습니다"


# Gradio UI
now_dt = now_kst()
later_dt = now_dt + timedelta(hours=2)

with gr.Blocks(css=CSS_GRADIO, title=APP_NAME) as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})
    iframe_nonce = gr.State(int(time.time()))

    gr.Markdown(f"# {APP_NAME}\n로그인한 회원만 접근 가능")

    with gr.Tabs():
        with gr.Tab("탐색"):
            explore_iframe = gr.HTML(f'<iframe src="/explore?t={int(time.time())}" style="width:100%;height:74vh;border:none;border-radius:16px;"></iframe>')
            ref_btn1 = gr.Button("🔄 새로고침", size="sm")
        with gr.Tab("지도"):
            map_iframe = gr.HTML(f'<iframe src="/map?t={int(time.time())}" style="width:100%;height:74vh;border:none;border-radius:16px;"></iframe>')
            ref_btn2 = gr.Button("🔄 새로고침", size="sm")

    # FAB
    with gr.Row(elem_classes=["fab-wrapper"]):
        fab = gr.Button("+")
    overlay = gr.HTML("<div class='overlay'></div>", visible=False)

    # 메인 모달
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_m:
        gr.HTML("<div class='modal-header'>새 이벤트 만들기</div>")

        with gr.Column(elem_classes=["modal-body"]):
            with gr.Row():
                t_in = gr.Textbox(
                    label="📝 이벤트명",
                    placeholder="예: 산책, 커피",
                    scale=3,
                    elem_id="event_title",
                    lines=2,
                    max_lines=4,
                )
                add_fav_btn = gr.Button("⭐", scale=1, size="sm")
                manage_fav_btn = gr.Button("🗑", scale=1, size="sm")

            fav_msg = gr.Markdown("")
            gr.Markdown("**⭐ 즐겨찾기 (최근 사용 순)**")
            with gr.Column(elem_classes=["fav-grid"]):
                f_btns = [gr.Button("", visible=False, size="sm") for _ in range(10)]

            img_in = gr.Image(label="📸 사진 (선택)", type="numpy", height=180, elem_id="event_photo")

            with gr.Row():
                s_in = gr.Textbox(label="📅 시작일시", value=now_dt.strftime("%Y-%m-%d %H:%M"))
                e_in = gr.Textbox(label="⏰ 종료일시", value=later_dt.strftime("%Y-%m-%d %H:%M"))

            max_in = gr.Number(label="👥 제한 인원", value=10, precision=0)

            addr_v = gr.Textbox(label="📍 장소", interactive=False, value="")
            addr_btn = gr.Button("🔍 장소 검색하기")

            msg_out = gr.Markdown("")

        with gr.Row(elem_classes=["modal-footer"]):
            m_close = gr.Button("취소", variant="secondary")
            m_save = gr.Button("✅ 생성", variant="primary")

    # 서브 모달 (주소 검색)
    with gr.Column(visible=False, elem_classes=["sub-modal"]) as modal_s:
        with gr.Column(elem_classes=["sub-body"]):
            gr.Markdown("### 📍 장소 검색")
            q_in = gr.Textbox(label="검색어", placeholder="예: 포항시청, 영일대")
            q_btn = gr.Button("검색")
            q_res = gr.Radio(label="결과 (클릭하면 선택)", choices=[], interactive=True)
            with gr.Row():
                s_close = gr.Button("뒤로", variant="secondary")
                s_final = gr.Button("✅ 확정", variant="primary")

    # 서브 모달 (즐겨찾기 관리/삭제)
    with gr.Column(visible=False, elem_classes=["sub-modal"]) as modal_f:
        with gr.Column(elem_classes=["sub-body"]):
            gr.Markdown("### ⭐ 즐겨찾기 관리")
            fav_list = gr.Radio(label="즐겨찾기 목록", choices=[], interactive=True)
            with gr.Row():
                f_close = gr.Button("닫기", variant="secondary")
                f_del = gr.Button("선택 삭제", variant="primary")
            fav_del_msg = gr.Markdown("")

    # ---------------- handlers ----------------
    def reload_iframes(_nonce):
        n = int(time.time())
        return (
            gr.update(value=f'<iframe src="/explore?t={n}" style="width:100%;height:74vh;border:none;border-radius:16px;"></iframe>'),
            gr.update(value=f'<iframe src="/map?t={n}" style="width:100%;height:74vh;border:none;border-radius:16px;"></iframe>'),
            n,
        )

    ref_btn1.click(reload_iframes, iframe_nonce, [explore_iframe, map_iframe, iframe_nonce])
    ref_btn2.click(reload_iframes, iframe_nonce, [explore_iframe, map_iframe, iframe_nonce])

    def open_m():
        updates = top10_favs_updates()
        return [gr.update(visible=True), gr.update(visible=True)] + updates

    fab.click(open_m, None, [overlay, modal_m, *f_btns])

    def close_main():
        return gr.update(visible=False), gr.update(visible=False)

    m_close.click(close_main, None, [overlay, modal_m])

    def add_fav(title):
        title = (title or "").strip()
        if not title:
            msg = "⚠️ 이벤트명을 입력해 주세요"
            updates = [gr.update()] * 10
            return [msg] + updates

        with db_conn() as con:
            con.execute(
                "INSERT INTO favs (name, count) VALUES (?, 1) "
                "ON CONFLICT(name) DO UPDATE SET count = count + 1",
                (title,),
            )
            con.commit()

        updates = top10_favs_updates()
        msg = f"✅ '{title}'를 즐겨찾기에 추가했습니다"
        return [msg] + updates

    add_fav_btn.click(add_fav, t_in, [fav_msg] + f_btns)

    for b in f_btns:
        b.click(lambda x: x, b, t_in)

    # 주소 모달
    addr_btn.click(lambda: gr.update(visible=True), None, modal_s)
    s_close.click(lambda: gr.update(visible=False), None, modal_s)

    def search_k(q):
        q = (q or "").strip()
        if not q:
            return [], gr.update(choices=[])
        if not KAKAO_REST_API_KEY:
            return [], gr.update(choices=["⚠️ KAKAO_REST_API_KEY 환경변수 필요"])

        try:
            headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
            res = requests.get(
                "https://dapi.kakao.com/v2/local/search/keyword.json",
                headers=headers,
                params={"query": q, "size": 8},
                timeout=10,
            )
            data = res.json()
            docs = data.get("documents", []) or []
            cands = []
            for d in docs:
                label = f"{d.get('place_name','')} | {d.get('address_name','')}"
                cands.append(
                    {
                        "label": label,
                        "name": d.get("place_name", ""),
                        "y": d.get("y", 0),
                        "x": d.get("x", 0),
                    }
                )
            return cands, gr.update(choices=[x["label"] for x in cands], value=None)
        except Exception as e:
            return [], gr.update(choices=[f"⚠️ 검색 오류: {str(e)}"])

    q_btn.click(search_k, q_in, [search_state, q_res])

    def confirm_k(sel, cands):
        if not sel or not cands:
            return "", {}, gr.update(visible=False)
        item = next((x for x in cands if x.get("label") == sel), None)
        if not item:
            return "", {}, gr.update(visible=False)
        return item["label"], item, gr.update(visible=False)

    s_final.click(confirm_k, [q_res, search_state], [addr_v, selected_addr, modal_s])

    # 즐겨찾기 관리
    def load_favs():
        with db_conn() as con:
            rows = con.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 50").fetchall()
        names = [r[0] for r in rows]
        return gr.update(choices=names, value=None), gr.update(visible=True), ""

    manage_fav_btn.click(load_favs, None, [fav_list, modal_f, fav_del_msg])
    f_close.click(lambda: gr.update(visible=False), None, modal_f)

    def delete_fav(sel):
        sel = (sel or "").strip()
        if not sel:
            msg = "⚠️ 삭제할 즐겨찾기를 선택해 주세요"
            keep_list = gr.update()
            keep_btns = [gr.update()] * 10
            return [msg, keep_list] + keep_btns

        with db_conn() as con:
            con.execute("DELETE FROM favs WHERE name = ?", (sel,))
            con.commit()
            rows50 = con.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 50").fetchall()
        names50 = [r[0] for r in rows50]

        updates = top10_favs_updates()
        msg = f"✅ '{sel}' 즐겨찾기를 삭제했습니다"
        return [msg, gr.update(choices=names50, value=None)] + updates

    f_del.click(delete_fav, fav_list, [fav_del_msg, fav_list, *f_btns])

    # 이벤트 저장 (요청에서 세션 유저 뽑기)
    def save_and_close(title, img, start, end, addr, max_people, nonce, request: gr.Request):
        # FastAPI 미들웨어에서 /app 접근은 이미 로그인 보장
        token = (request.cookies.get(COOKIE_NAME) or "").strip()
        user = get_user_by_session(token)
        if not user:
            return "⚠️ 로그인이 필요합니다", gr.update(), gr.update(visible=False), gr.update(visible=False), nonce

        ok, msg = save_event(user["id"], title, img, start, end, addr, max_people)
        if not ok:
            return msg, gr.update(), gr.update(), gr.update(), nonce

        # 성공 시 iframe 새로고침
        n = int(time.time())
        exp_if = f'<iframe src="/explore?t={n}" style="width:100%;height:74vh;border:none;border-radius:16px;"></iframe>'
        map_if = f'<iframe src="/map?t={n}" style="width:100%;height:74vh;border:none;border-radius:16px;"></iframe>'
        return msg, gr.update(value=exp_if), gr.update(visible=False), gr.update(visible=False), n

    m_save.click(
        save_and_close,
        [t_in, img_in, s_in, e_in, selected_addr, max_in, iframe_nonce],
        [msg_out, explore_iframe, overlay, modal_m, iframe_nonce],
    )
    # map_iframe도 같이 갱신되게: 저장 후 새로고침 버튼 누르지 않게
    # (Gradio 출력 슬롯 한 번 더 연결)
    def sync_map(nonce):
        n = int(nonce) if nonce else int(time.time())
        return gr.update(value=f'<iframe src="/map?t={n}" style="width:100%;height:74vh;border:none;border-radius:16px;"></iframe>')
    iframe_nonce.change(sync_map, iframe_nonce, map_iframe)


# Gradio mount
app = gr.mount_gradio_app(app, demo, path="/app")


# =============================================================================
# 10) 실행
# =============================================================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

