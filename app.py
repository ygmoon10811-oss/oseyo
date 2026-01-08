# -*- coding: utf-8 -*-
import os
import uuid
import hmac
import base64
import sqlite3
import hashlib
from datetime import datetime, timedelta, timezone

import requests
import gradio as gr
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn

# =============================================================================
# 설정
# =============================================================================
APP_NAME = "오세요"
KST = timezone(timedelta(hours=9))

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")  # Render 환경변수로 꼭 바꿔두는 걸 추천
COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 168  # 7일

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "")
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "")

# =============================================================================
# 시간/DB
# =============================================================================
def now_kst():
    return datetime.now(tz=KST)


def pick_db_path():
    for d in ("/var/data", "/tmp"):
        try:
            os.makedirs(d, exist_ok=True)
            test = os.path.join(d, ".writetest")
            with open(test, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test)
            return os.path.join(d, "oseyo.db")
        except Exception:
            pass
    return "/tmp/oseyo.db"


DB_PATH = pick_db_path()
print("[DB]", DB_PATH)


def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


# =============================================================================
# DB 스키마
# =============================================================================
with db_conn() as con:
    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE,
            pw_hash TEXT,
            created_at TEXT
        );
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT,
            expires_at TEXT
        );
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            title TEXT,
            photo TEXT,
            start TEXT,
            end TEXT,
            addr TEXT,
            lat REAL,
            lng REAL,
            created_at TEXT
        );
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            event_id TEXT,
            user_id TEXT,
            PRIMARY KEY(event_id, user_id)
        );
    """)
    con.commit()


def migrate_events_table():
    with db_conn() as con:
        cols = [r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()]
        if "owner_user_id" not in cols:
            con.execute("ALTER TABLE events ADD COLUMN owner_user_id TEXT DEFAULT ''")
        if "max_people" not in cols:
            con.execute("ALTER TABLE events ADD COLUMN max_people INTEGER DEFAULT 10")
        con.commit()


migrate_events_table()

# =============================================================================
# 인증 유틸
# =============================================================================
def make_pw_hash(pw: str) -> str:
    salt = uuid.uuid4().hex
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 120000)
    return f"{salt}${base64.b64encode(dk).decode('utf-8')}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, hv = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 120000)
        return hmac.compare_digest(base64.b64encode(dk).decode("utf-8"), hv)
    except Exception:
        return False


def new_session(user_id: str) -> str:
    token = uuid.uuid4().hex
    exp = now_kst() + timedelta(hours=SESSION_HOURS)
    with db_conn() as con:
        con.execute("INSERT INTO sessions VALUES (?,?,?)", (token, user_id, exp.isoformat()))
        con.commit()
    return token


def get_user_by_token(token: str):
    if not token:
        return None
    with db_conn() as con:
        row = con.execute("SELECT user_id, expires_at FROM sessions WHERE token=?", (token,)).fetchone()
        if not row:
            return None
        uid, exp = row
        try:
            if datetime.fromisoformat(exp) < now_kst():
                return None
        except Exception:
            return None

        u = con.execute("SELECT id, username FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            return None
        return {"id": u[0], "username": u[1]}


def set_auth_cookie(resp, token: str):
    # Render는 https가 기본이므로 secure=True도 가능하지만
    # 혹시 로컬 테스트/프록시 환경이면 쿠키가 안 박힐 수 있어서 일단 False로 둠.
    # Render만 쓸 거면 secure=True로 바꿔도 됨.
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        secure=False,
        max_age=SESSION_HOURS * 3600
    )
    return resp


# =============================================================================
# FastAPI
# =============================================================================
app = FastAPI()


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path

    # 누구나 접근 허용
    if path in ("/", "/login", "/signup", "/logout", "/health"):
        return await call_next(request)

    # 보호할 경로
    if path.startswith(("/app", "/explore", "/map", "/api")):
        token = request.cookies.get(COOKIE_NAME)
        if not get_user_by_token(token):
            return RedirectResponse("/login", status_code=303)

    return await call_next(request)


@app.get("/health")
def health():
    return {"ok": True}


# ✅ 루트 접속 시 app로 보내기 (로그인 안됐으면 middleware가 /login으로 보냄)
@app.get("/")
def root():
    return RedirectResponse("/app", status_code=303)


# =============================================================================
# 로그인/회원가입/로그아웃
# =============================================================================
@app.get("/login")
def login_page():
    return HTMLResponse("""
    <div style="max-width:420px;margin:40px auto;font-family:system-ui">
      <h2>로그인</h2>
      <form method="post" action="/login">
        <input name="username" placeholder="아이디" style="width:100%;padding:10px;margin:6px 0" />
        <input name="password" type="password" placeholder="비밀번호" style="width:100%;padding:10px;margin:6px 0" />
        <button style="width:100%;padding:10px;margin-top:10px">로그인</button>
      </form>
      <p style="margin-top:14px"><a href="/signup">회원가입</a></p>
    </div>
    """)


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    username = (username or "").strip()
    with db_conn() as con:
        row = con.execute("SELECT id, pw_hash FROM users WHERE username=?", (username,)).fetchone()

    if (not row) or (not check_pw(password, row[1])):
        # 실패 시 GET /login 으로 돌려보내기 (303)
        return RedirectResponse("/login", status_code=303)

    token = new_session(row[0])
    resp = RedirectResponse("/app", status_code=303)  # ✅ POST 후 303이 핵심
    return set_auth_cookie(resp, token)


@app.get("/signup")
def signup_page():
    return HTMLResponse("""
    <div style="max-width:420px;margin:40px auto;font-family:system-ui">
      <h2>회원가입</h2>
      <form method="post" action="/signup">
        <input name="username" placeholder="아이디" style="width:100%;padding:10px;margin:6px 0" />
        <input name="password" type="password" placeholder="비밀번호" style="width:100%;padding:10px;margin:6px 0" />
        <button style="width:100%;padding:10px;margin-top:10px">가입</button>
      </form>
      <p style="margin-top:14px"><a href="/login">로그인</a></p>
    </div>
    """)


@app.post("/signup")
def signup(username: str = Form(...), password: str = Form(...)):
    username = (username or "").strip()
    if not username or not password:
        return RedirectResponse("/signup", status_code=303)

    uid = uuid.uuid4().hex
    try:
        with db_conn() as con:
            con.execute(
                "INSERT INTO users VALUES (?,?,?,?)",
                (uid, username, make_pw_hash(password), now_kst().isoformat())
            )
            con.commit()
    except sqlite3.IntegrityError:
        return RedirectResponse("/signup", status_code=303)

    token = new_session(uid)
    resp = RedirectResponse("/app", status_code=303)
    return set_auth_cookie(resp, token)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# =============================================================================
# API: 이벤트 생성 (Gradio에서 호출)
# =============================================================================
@app.post("/api/events/create")
def api_create_event(
    request: Request,
    title: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    max_people: int = Form(10)
):
    user = get_user_by_token(request.cookies.get(COOKIE_NAME))
    if not user:
        return RedirectResponse("/login", status_code=303)

    eid = uuid.uuid4().hex[:8]
    with db_conn() as con:
        con.execute("""
            INSERT INTO events
            (id, owner_user_id, title, photo, start, end, addr, lat, lng, max_people, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            eid,
            user["id"],
            (title or "").strip(),
            "",
            (start or "").strip(),
            (end or "").strip(),
            "",
            0.0,
            0.0,
            int(max_people or 10),
            now_kst().strftime("%Y-%m-%d %H:%M:%S")
        ))
        con.commit()

    return {"ok": True, "id": eid}


# =============================================================================
# 탐색(간단)
# =============================================================================
@app.get("/explore")
def explore(request: Request):
    now_s = now_kst().strftime("%Y-%m-%d %H:%M")
    with db_conn() as con:
        rows = con.execute("""
            SELECT title, start, end, max_people
            FROM events
            WHERE end > ?
            ORDER BY created_at DESC
        """, (now_s,)).fetchall()

    items = ""
    for t, s, e, mp in rows:
        items += f"<li><b>{t}</b> ({s} ~ {e}) / 제한 {mp}명</li>"
    if not items:
        items = "<li>등록된 이벤트가 없습니다.</li>"

    return HTMLResponse(f"""
    <div style="font-family:system-ui;padding:16px">
      <h3>탐색</h3>
      <ul>{items}</ul>
      <p><a href="/logout">로그아웃</a></p>
    </div>
    """)


# =============================================================================
# Gradio UI
# =============================================================================
with gr.Blocks(title=APP_NAME) as demo:
    gr.Markdown(f"# {APP_NAME}")
    gr.Markdown("로그인한 사용자만 접근 가능")

    with gr.Tabs():
        with gr.Tab("탐색"):
            gr.HTML('<iframe src="/explore" style="width:100%;height:72vh;border:none;border-radius:12px;"></iframe>')

        with gr.Tab("이벤트 생성"):
            title = gr.Textbox(label="이벤트명")
            start = gr.Textbox(label="시작일시 (YYYY-MM-DD HH:MM)")
            end = gr.Textbox(label="종료일시 (YYYY-MM-DD HH:MM)")
            max_people = gr.Number(label="제한 인원", value=10)
            btn = gr.Button("생성")
            out = gr.Markdown()

            def submit(title, start, end, max_people, request: gr.Request):
                # gradio가 제공하는 현재 origin을 기반으로 API 호출
                base = request.url_root.rstrip("/")
                r = requests.post(
                    f"{base}/api/events/create",
                    data={
                        "title": title,
                        "start": start,
                        "end": end,
                        "max_people": int(max_people or 10)
                    },
                    cookies=request.cookies,
                    timeout=15,
                )
                return "✅ 생성 완료" if r.ok else f"❌ 실패: {r.status_code}"

            btn.click(submit, [title, start, end, max_people], out)

app = gr.mount_gradio_app(app, demo, path="/app")


# =============================================================================
# Run
# =============================================================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
