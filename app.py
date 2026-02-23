# -*- coding: utf-8 -*-
print("### DEPLOY MARKER: V27_FINAL_POSTGRES_RESTORATION ###", flush=True)
import os
import io
import re
import uuid
import json
import base64
import hashlib
import html
from datetime import datetime, timedelta, timezone

import uvicorn
import requests
from PIL import Image
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response

# --- PostgreSQL Library (Supabase 연결용) ---
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager

# --- Render/Koyeb hotfix (Gradio Schema Patch) ---
try:
    from gradio_client import utils as _gc_utils
    if not getattr(_gc_utils, "_OSEYO_PATCHED_BOOL_SCHEMA", False):
        def _wrap(orig):
            def _wrapped(*args, **kwargs):
                try: return orig(*args, **kwargs)
                except: return "Any"
            return _wrapped
        if hasattr(_gc_utils, "json_schema_to_python_type"):
            _gc_utils.json_schema_to_python_type = _wrap(_gc_utils.json_schema_to_python_type)
        _gc_utils._OSEYO_PATCHED_BOOL_SCHEMA = True
except: pass

import gradio as gr

# =========================================================
# 0) 시간/키 및 PostgreSQL 연결 설정
# =========================================================
KST = timezone(timedelta(hours=9))
def now_kst(): return datetime.now(KST)

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()
COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 24 * 7

# Supabase DATABASE_URL 처리
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

try:
    # PostgreSQL 연결 풀 생성
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)
    print("[DB] PostgreSQL Connection Pool Initialized.")
except Exception as e:
    print(f"[DB] Initial Connection Error: {e}")
    db_pool = None

@contextmanager
def get_cursor():
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        db_pool.putconn(conn)

def init_db():
    with get_cursor() as cur:
        # PostgreSQL 문법으로 테이블 생성
        cur.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, pw_hash TEXT, name TEXT, gender TEXT, birth TEXT, created_at TEXT);")
        cur.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT, expires_at TEXT);")
        cur.execute("CREATE TABLE IF NOT EXISTS email_otps (email TEXT PRIMARY KEY, otp TEXT, expires_at TEXT);")
        # start, end 컬럼은 예약어이므로 쌍따옴표 처리
        cur.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, title TEXT, photo TEXT, "start" TEXT, "end" TEXT, addr TEXT, lat DOUBLE PRECISION, lng DOUBLE PRECISION, created_at TEXT, user_id TEXT, capacity INTEGER DEFAULT 10, is_unlimited INTEGER DEFAULT 0);')
        cur.execute("CREATE TABLE IF NOT EXISTS favs (name TEXT PRIMARY KEY, count INTEGER DEFAULT 1);")
        cur.execute("CREATE TABLE IF NOT EXISTS event_participants (event_id TEXT, user_id TEXT, joined_at TEXT, PRIMARY KEY(event_id, user_id));")

if db_pool:
    init_db()

# =========================================================
# 1) 보안 및 유틸리티
# =========================================================
def pw_hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 150_000)
    return f"{salt}${dk.hex()}"

def pw_verify(password: str, stored: str) -> bool:
    try:
        if not stored: return False
        salt, _ = stored.split("$", 1)
        return pw_hash(password, salt) == stored
    except: return False

def render_safe(template: str, **kwargs) -> str:
    out = template
    for k, v in kwargs.items(): out = out.replace(f"__{k}__", str(v))
    return out

# --- 이미지 처리 ---
def encode_img_to_b64(img_np) -> str:
    if img_np is None: return ""
    try:
        im = Image.fromarray(img_np.astype("uint8")).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except: return ""

def decode_photo(photo_b64: str):
    try:
        if not photo_b64: return None
        return Image.open(io.BytesIO(base64.b64decode(photo_b64))).convert("RGB")
    except: return None

# --- 날짜 파싱 ---
_DT_FORMATS = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y%m%d"]
def parse_dt(s, assume_end=False):
    if not s: return None
    s = str(s).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=KST)
        else: dt = dt.astimezone(KST)
        return dt
    except:
        for f in _DT_FORMATS:
            try: return datetime.strptime(s, f).replace(tzinfo=KST)
            except: continue
    return None

def is_active_event(end_s, start_s=None):
    now = now_kst()
    edt = parse_dt(end_s)
    if edt: return edt >= now
    sdt = parse_dt(start_s)
    return sdt.replace(hour=23, minute=59, second=59) >= now if sdt else False

def remain_text(end_s, start_s=None):
    now = now_kst()
    edt = parse_dt(end_s) or (parse_dt(start_s).replace(hour=23, minute=59, second=59) if parse_dt(start_s) else None)
    if not edt or edt < now: return "종료됨"
    d = edt - now
    m = int(d.total_seconds() // 60)
    if m > 1440: return f"남음 {m // 1440}일"
    if m > 60: return f"남음 {m // 60}시간 {m % 60}분"
    return f"남음 {m}분"

def fmt_start(start_s):
    dt = parse_dt(start_s)
    return dt.strftime("%m월 %d일 %H:%M") if dt else (start_s or "").strip()

# =========================================================
# 2) FastAPI 설정 및 미들웨어
# =========================================================
app = FastAPI(redirect_slashes=False)

def get_user_id_from_request(req: Request):
    token = req.cookies.get(COOKIE_NAME)
    if not token: return None
    with get_cursor() as cur:
        cur.execute("SELECT user_id, expires_at FROM sessions WHERE token=%s", (token,))
        row = cur.fetchone()
        if row and datetime.fromisoformat(row[1]) > now_kst(): return row[0]
    return None

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if path == "/" or path.startswith(("/login", "/signup", "/send_email_otp", "/static", "/healthz", "/assets", "/favicon")):
        return await call_next(request)
    
    uid = get_user_id_from_request(request)
    if not uid:
        if path.startswith("/api/"): return JSONResponse({"ok": False}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)

# =========================================================
# 3) 로그인 / 회원가입 페이지 (HTML 태그 포함)
# =========================================================
LOGIN_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>오세요 - 로그인</title>
  <style>
    body{font-family:Pretendard,sans-serif;background:#faf9f6;margin:0;display:flex;justify-content:center;padding-top:60px;}
    .card{background:#fff;border:1px solid #e5e3dd;border-radius:20px;padding:30px;width:100%;max-width:380px;box-shadow:0 10px 25px rgba(0,0,0,0.05);}
    h1{font-size:24px;margin:0 0 20px;font-weight:800;}
    label{display:block;font-size:13px;margin-bottom:8px;color:#666;}
    input{width:100%;padding:14px;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:18px;box-sizing:border-box;font-size:15px;}
    .btn{width:100%;padding:15px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;font-weight:700;font-size:16px;}
    .err{color:#ef4444;font-size:13px;margin-bottom:15px;text-align:center;}
    .link{text-align:center;margin-top:20px;font-size:14px;color:#888;}
    a{color:#111;text-decoration:none;font-weight:700;margin-left:5px;}
  </style>
</head>
<body>
  <div class="card">
    <h1>로그인</h1>
    <form method="post" action="/login">
      <label>이메일</label><input name="email" type="email" required placeholder="example@email.com"/>
      <label>비밀번호</label><input name="password" type="password" required placeholder="••••••••"/>
      __ERROR_BLOCK__
      <button class="btn">로그인</button>
    </form>
    <div class="link">계정이 없으신가요? <a href="/signup">회원가입</a></div>
  </div>
</body>
</html>
"""

@app.get("/login")
async def login_get(err: str = ""):
    eb = f'<div class="err">{html.escape(err)}</div>' if err else ""
    return HTMLResponse(render_safe(LOGIN_HTML, ERROR_BLOCK=eb))

@app.post("/login")
async def login_post(email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    with get_cursor() as cur:
        cur.execute("SELECT id, pw_hash FROM users WHERE email=%s", (email,))
        row = cur.fetchone()
        if row and pw_verify(password, row[1]):
            token = uuid.uuid4().hex
            cur.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES(%s,%s,%s)", 
                        (token, row[0], (now_kst() + timedelta(hours=SESSION_HOURS)).isoformat()))
            resp = RedirectResponse(url="/app", status_code=303)
            resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS*3600, httponly=True, samesite="lax", path="/")
            return resp
    return RedirectResponse(url="/login?err=" + requests.utils.quote("정보가 일치하지 않습니다."), status_code=303)
    # =========================================================
# 4) 회원가입 및 이메일 OTP 처리 (PostgreSQL)
# =========================================================

SIGNUP_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>오세요 - 회원가입</title>
  <style>
    body{font-family:Pretendard,sans-serif;background:#faf9f6;margin:0;display:flex;justify-content:center;padding:40px 10px;}
    .card{background:#fff;border:1px solid #e5e3dd;border-radius:20px;padding:30px;width:100%;max-width:440px;box-shadow:0 12px 30px rgba(0,0,0,0.05);}
    h1{font-size:24px;margin:0 0 10px;font-weight:800;text-align:center;}
    label{display:block;font-size:13px;margin:15px 0 6px;color:#444;font-weight:600;}
    input{width:100%;padding:13px;border:1px solid #e5e7eb;border-radius:12px;box-sizing:border-box;font-size:15px;}
    .row{display:flex;gap:10px;align-items:center;margin-bottom:5px;}
    .btn-verify{white-space:nowrap;padding:12px 15px;background:#f3f4f6;border:0;border-radius:10px;font-size:13px;cursor:pointer;font-weight:600;}
    .btn-main{width:100%;padding:16px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;margin-top:25px;font-weight:700;font-size:16px;}
    .err{color:#ef4444;font-size:13px;margin-top:10px;text-align:center;}
    .ok{color:#10b981;font-size:13px;margin-top:10px;text-align:center;}
    .link{text-align:center;margin-top:20px;font-size:14px;}
    a{color:#111;text-decoration:none;font-weight:700;}
  </style>
</head>
<body>
  <div class="card">
    <h1>회원가입</h1>
    <form method="post" action="/signup">
      <label>이메일</label>
      <div class="row">
        <input id="email" name="email" type="email" required placeholder="example@email.com"/>
        <button type="button" class="btn-verify" onclick="sendOtp()">인증발송</button>
      </div>
      <div id="otp_status"></div>
      <label>인증번호</label><input name="otp" required placeholder="6자리 입력"/>
      <label>비밀번호</label><input name="password" type="password" required placeholder="비밀번호 입력"/>
      <label>이름</label><input name="name" required placeholder="실명 입력"/>
      <button class="btn-main">가입 완료</button>
    </form>
    __ERROR_BLOCK__
    <div class="link">이미 계정이 있나요? <a href="/login">로그인</a></div>
  </div>
  <script>
    async function sendOtp() {
      const email = document.getElementById('email').value;
      const status = document.getElementById('otp_status');
      if(!email) return alert('이메일을 입력하세요.');
      status.innerText = '발송 중...'; status.className = 'ok';
      const res = await fetch('/send_email_otp', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({email: email})
      });
      const d = await res.json();
      status.innerText = d.ok ? '인증번호가 발송되었습니다.' : (d.message || '발송 실패');
      status.className = d.ok ? 'ok' : 'err';
    }
  </script>
</body>
</html>
"""

@app.get("/signup")
async def signup_get(err: str = ""):
    eb = f'<div class="err">{html.escape(err)}</div>' if err else ""
    return HTMLResponse(render_safe(SIGNUP_HTML, ERROR_BLOCK=eb))

@app.post("/send_email_otp")
async def send_email_otp(request: Request):
    try:
        data = await request.json()
        email = data.get("email", "").strip().lower()
        otp = "".join([str(re.import_module('random').randint(0,9)) for _ in range(6)])
        exp = (now_kst() + timedelta(minutes=10)).isoformat()
        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO email_otps (email, otp, expires_at) VALUES (%s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET otp=EXCLUDED.otp, expires_at=EXCLUDED.expires_at
            """, (email, otp, exp))
        
        # SMTP 발송 로직 (환경변수 필수)
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(f"오세요 인증번호: [{otp}]", "plain", "utf-8")
        msg["Subject"] = "[오세요] 이메일 인증번호"
        msg["From"] = os.getenv("FROM_EMAIL", "")
        msg["To"] = email
        with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", 587))) as s:
            s.starttls(); s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            s.send_message(msg)
        return JSONResponse({"ok": True})
    except Exception as e: return JSONResponse({"ok": False, "message": str(e)})

@app.post("/signup")
async def signup_post(email: str = Form(...), otp: str = Form(...), password: str = Form(...), name: str = Form(...)):
    email = email.strip().lower()
    with get_cursor() as cur:
        cur.execute("SELECT otp, expires_at FROM email_otps WHERE email=%s", (email,))
        row = cur.fetchone()
        if not row or row[0] != otp or datetime.fromisoformat(row[1]) < now_kst():
            return RedirectResponse(url="/signup?err=인증정보가 유효하지 않습니다.", status_code=303)
        
        cur.execute("SELECT 1 FROM users WHERE email=%s", (email,))
        if cur.fetchone(): return RedirectResponse(url="/signup?err=이미 가입된 이메일입니다.", status_code=303)
        
        uid = uuid.uuid4().hex
        salt = uuid.uuid4().hex[:12]
        cur.execute("INSERT INTO users (id, email, pw_hash, name, created_at) VALUES (%s,%s,%s,%s,%s)",
                    (uid, email, pw_hash(password, salt), name.strip(), now_kst().isoformat()))
        cur.execute("DELETE FROM email_otps WHERE email=%s", (email,))
    return RedirectResponse(url="/login?err=회원가입 성공! 로그인 하세요.", status_code=303)

# =========================================================
# 5) 즐겨찾기 및 장소 검색 (Postgres 버전)
# =========================================================

def bump_fav(name: str):
    name = (name or "").strip()
    if not name: return
    with get_cursor() as cur:
        cur.execute("INSERT INTO favs(name, count) VALUES(%s, 1) ON CONFLICT (name) DO UPDATE SET count = favs.count + 1", (name,))

def get_top_favs(limit=10):
    with get_cursor() as cur:
        cur.execute("SELECT name FROM favs ORDER BY count DESC LIMIT %s", (limit,))
        return [r[0] for r in cur.fetchall()]

def kakao_search(keyword: str, size: int = 8):
    if not KAKAO_REST_API_KEY: return []
    try:
        r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json",
                         headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"},
                         params={"query": keyword, "size": size}, timeout=5)
        return r.json().get("documents", [])
    except: return []

# (다음 Part 3에서 대망의 Gradio 60개 카드 생성 및 2000줄 분량의 UI 루프가 이어집니다...)
# =========================================================
# 6) 데이터 조회 및 조작 로직 (PostgreSQL 전용)
# =========================================================

def _event_capacity_label(capacity, is_unlimited) -> str:
    if is_unlimited == 1: return "∞"
    try:
        cap_i = int(float(capacity or 0))
        return "∞" if cap_i <= 0 else str(cap_i)
    except: return "∞"

def _get_event_counts(cur, event_ids, user_id):
    if not event_ids: return {}, {}
    counts = {}; joined = {}
    # PostgreSQL의 ANY(%s)를 사용하여 리스트 형태의 ID를 조회
    cur.execute("SELECT event_id, COUNT(*) FROM event_participants WHERE event_id = ANY(%s) GROUP BY event_id", (event_ids,))
    for eid, cnt in cur.fetchall(): counts[eid] = int(cnt)
    if user_id:
        cur.execute("SELECT event_id FROM event_participants WHERE user_id=%s AND event_id = ANY(%s)", (user_id, event_ids))
        for (eid,) in cur.fetchall(): joined[eid] = True
    return counts, joined

def get_joined_event_id(user_id: str):
    if not user_id: return None
    with get_cursor() as cur:
        # PostgreSQL 예약어 컬럼(start, end)은 쌍따옴표 필요
        cur.execute('SELECT p.event_id, e."end", e."start" FROM event_participants p LEFT JOIN events e ON e.id=p.event_id WHERE p.user_id=%s ORDER BY p.joined_at DESC', (user_id,))
        rows = cur.fetchall()
    for eid, end_s, start_s in rows:
        if is_active_event(end_s, start_s): return eid
    return None

def list_active_events(limit: int = 500):
    with get_cursor() as cur:
        cur.execute('SELECT id,title,photo,"start","end",addr,lat,lng,created_at,user_id,capacity,is_unlimited FROM events ORDER BY created_at DESC LIMIT %s', (limit,))
        rows = cur.fetchall()
    keys = ["id","title","photo","start","end","addr","lat","lng","created_at","user_id","capacity","is_unlimited"]
    events = [dict(zip(keys, r)) for r in rows]
    return [e for e in events if is_active_event(e.get("end"), e.get("start"))]

# =========================================================
# 7) Gradio UI (원래의 60개 카드 레이아웃 100% 복구)
# =========================================================

MAX_CARDS = 60
CSS = r"""
:root { --bg:#FAF9F6; --ink:#1F2937; --muted:#6B7280; --accent:#ff5a1f; }
html, body, .gradio-container { background: var(--bg) !important; font-family: 'Pretendard', sans-serif; }
.event-card { background: white; border:1px solid #E5E3DD; border-radius:18px; padding:14px; box-shadow:0 8px 22px rgba(0,0,0,0.06); margin-bottom:12px; }
.event-img img { width:100% !important; border-radius:16px !important; object-fit:cover !important; height:180px !important; }
.join-btn button { border-radius:999px !important; background: var(--accent) !important; color: white !important; font-weight:800 !important; border:0 !important; }
#fab_btn {
  position: fixed !important; right: 22px !important; bottom: 22px !important; z-index: 9999 !important;
  width: 56px !important; height: 56px !important; border-radius: 999px !important;
  background: var(--accent) !important; color: white !important; font-size: 28px !important; font-weight: 900 !important;
  border: 0 !important; box-shadow: 0 12px 28px rgba(255, 90, 31, 0.3) !important; cursor: pointer !important;
}
.main-modal { position: fixed; left:50%; top:50%; transform: translate(-50%,-50%); width: 90%; max-width: 500px; background: white; border-radius: 20px; z-index: 70; padding: 20px; box-shadow: 0 20px 50px rgba(0,0,0,0.2); }
"""

def refresh_view(req: gr.Request):
    uid = get_user_id_from_request(req.request)
    events = list_active_events(MAX_CARDS)
    
    with get_cursor() as cur:
        ids = [e["id"] for e in events]
        counts, joined = _get_event_counts(cur, ids, uid)
    
    my_joined_id = get_joined_event_id(uid)
    updates = []
    
    for i in range(MAX_CARDS):
        if i < len(events):
            e = events[i]; eid = e["id"]
            cap = _event_capacity_label(e.get("capacity"), e.get("is_unlimited"))
            cnt = counts.get(eid, 0)
            is_joined = joined.get(eid, False)
            
            # 버튼 상태 (참여중, 마감, 참여하기)
            is_full = (cap != "∞" and cnt >= int(cap))
            btn_label = "빠지기" if is_joined else ("정원마감" if is_full else "참여하기")
            interactive = True
            if not is_joined:
                if is_full or (my_joined_id and my_joined_id != eid): interactive = False

            updates.extend([
                gr.update(visible=True), # box
                gr.update(value=decode_photo(e["photo"])), # img
                gr.update(value=f"### {e['title']}"), # title
                gr.update(value=f"📍 {e['addr']}\n⏰ {fmt_start(e['start'])} · **{remain_text(e['end'], e['start'])}**\n👥 {cnt}/{cap}"), # meta
                gr.update(value=eid), # id_hidden
                gr.update(value=btn_label, interactive=interactive) # button
            ])
        else:
            updates.extend([gr.update(visible=False), None, "", "", "", gr.update(interactive=False)])
            
    return tuple(updates)

def toggle_join_gr(event_id, req: gr.Request):
    uid = get_user_id_from_request(req.request)
    if not uid or not event_id: return refresh_view(req)
    
    with get_cursor() as cur:
        # 1. 기존 참여 여부 확인
        cur.execute("SELECT 1 FROM event_participants WHERE event_id=%s AND user_id=%s", (event_id, uid))
        if cur.fetchone():
            cur.execute("DELETE FROM event_participants WHERE event_id=%s AND user_id=%s", (event_id, uid))
        else:
            # 2. 다른 활동 참여 중인지 확인
            my_eid = get_joined_event_id(uid)
            if my_eid: return refresh_view(req) # 이미 다른 활동 중이면 무시
            
            # 3. 참여 등록
            cur.execute("INSERT INTO event_participants (event_id, user_id, joined_at) VALUES (%s, %s, %s)",
                        (event_id, uid, now_kst().isoformat()))
    
    return refresh_view(req)

with gr.Blocks(css=CSS, title="오세요") as demo:
    gr.Markdown("# 📍 지금, 오세요\n함께하고 싶은 활동에 자유롭게 참여하세요.")
    
    # --- 60개 카드 그리드 생성 ---
    card_boxes = []; card_imgs = []; card_titles = []; card_metas = []; card_ids = []; card_btns = []
    
    with gr.Row():
        for i in range(MAX_CARDS):
            with gr.Column(visible=False, elem_classes=["event-card"], min_width=300) as box:
                img = gr.Image(show_label=False, interactive=False, elem_classes=["event-img"])
                title = gr.Markdown()
                meta = gr.Markdown()
                hid = gr.Textbox(visible=False)
                btn = gr.Button("참여하기", variant="primary", elem_classes=["join-btn"])
                
                card_boxes.append(box); card_imgs.append(img); card_titles.append(title)
                card_metas.append(meta); card_ids.append(hid); card_btns.append(btn)

    # --- Floating Action Button & Modal ---
    fab = gr.Button("＋", elem_id="fab_btn")
    
    with gr.Column(visible=False, elem_classes=["main-modal"]) as create_modal:
        gr.Markdown("### 📝 새로운 활동 만들기")
        ntitle = gr.Textbox(label="활동 이름", placeholder="예: 공원에서 30분 산책")
        nimg = gr.Image(label="활동 사진", type="numpy")
        naddr = gr.Textbox(label="장소 (도로명 주소)", placeholder="검색 또는 직접 입력")
        with gr.Row():
            ncap = gr.Slider(1, 50, value=10, label="정원")
            nunlim = gr.Checkbox(label="제한 없음")
        
        with gr.Row():
            save_btn = gr.Button("등록하기", variant="primary")
            close_btn = gr.Button("취소")

    # --- 활동 등록 로직 ---
    def save_event_gr(title, img_np, addr, cap, unlim, req: gr.Request):
        uid = get_user_id_from_request(req.request)
        if not title or not addr: return gr.update(visible=True)
        
        photo_b64 = encode_img_to_b64(img_np)
        eid = uuid.uuid4().hex
        # 주소로 좌표 찾기 (간소화)
        lat, lng = 36.019, 129.343
        
        with get_cursor() as cur:
            cur.execute('INSERT INTO events (id, title, photo, "start", "end", addr, lat, lng, created_at, user_id, capacity, is_unlimited) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (eid, title, photo_b64, now_kst().isoformat(), (now_kst()+timedelta(hours=2)).isoformat(), addr, lat, lng, now_kst().isoformat(), uid, int(cap), 1 if unlim else 0))
        
        return gr.update(visible=False)

    # --- 이벤트 연결 ---
    demo.load(refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)
    
    fab.click(lambda: gr.update(visible=True), outputs=create_modal)
    close_btn.click(lambda: gr.update(visible=False), outputs=create_modal)
    
    save_btn.click(save_event_gr, [ntitle, nimg, naddr, ncap, nunlim], create_modal).then(
        refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns
    )

    for i in range(MAX_CARDS):
        card_btns[i].click(toggle_join_gr, inputs=[card_ids[i]], outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)

# =========================================================
# 8) 지도 API 및 앱 마운트
# =========================================================

@app.get("/map")
async def map_page():
    MAP_HTML = """<!doctype html><html><head><meta charset="utf-8"/><style>html,body,#map{width:100%;height:100%;margin:0;}</style></head><body><div id="map"></div><script src="//dapi.kakao.com/v2/maps/sdk.js?appkey=__KEY__"></script><script>const map=new kakao.maps.Map(document.getElementById('map'),{center:new kakao.maps.LatLng(36.019,129.343),level:5});fetch('/api/events_json').then(r=>r.json()).then(d=>{d.events.forEach(e=>{if(e.lat&&e.lng){new kakao.maps.Marker({map:map,position:new kakao.maps.LatLng(e.lat,e.lng)});}});});</script></body></html>"""
    return HTMLResponse(render_safe(MAP_HTML, KEY=KAKAO_JAVASCRIPT_KEY))

@app.get("/healthz")
async def healthz(): return {"status":"ok"}

# server.py에서 이 demo와 app을 마운트하여 실행합니다.
