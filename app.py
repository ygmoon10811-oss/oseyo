# -*- coding: utf-8 -*-
import os
import io
import re
import uuid
import json
import base64
import hashlib
import html
import random
from datetime import datetime, timedelta, timezone

import uvicorn
import requests
from PIL import Image
from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# --- PostgreSQL Library ---
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager

import gradio as gr

# =========================================================
# 0) DB 및 시간 설정
# =========================================================
KST = timezone(timedelta(hours=9))
def now_kst(): return datetime.now(KST)

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()
COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 24 * 7

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

try:
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)
except Exception as e:
    print(f"DB Pool Error: {e}")
    db_pool = None

@contextmanager
def get_cursor():
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur: yield cur
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        db_pool.putconn(conn)

def init_db():
    with get_cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, pw_hash TEXT, name TEXT, gender TEXT, birth TEXT, created_at TEXT);")
        cur.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT, expires_at TEXT);")
        cur.execute("CREATE TABLE IF NOT EXISTS email_otps (email TEXT PRIMARY KEY, otp TEXT, expires_at TEXT);")
        cur.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, title TEXT, photo TEXT, "start" TEXT, "end" TEXT, addr TEXT, lat DOUBLE PRECISION, lng DOUBLE PRECISION, created_at TEXT, user_id TEXT, capacity INTEGER DEFAULT 10, is_unlimited INTEGER DEFAULT 0);')
        cur.execute("CREATE TABLE IF NOT EXISTS favs (name TEXT PRIMARY KEY, count INTEGER DEFAULT 1);")
        cur.execute("CREATE TABLE IF NOT EXISTS event_participants (event_id TEXT, user_id TEXT, joined_at TEXT, PRIMARY KEY(event_id, user_id));")

if db_pool: init_db()

# =========================================================
# 1) 유틸리티 (암호, 날짜, 이미지)
# =========================================================
def pw_hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 150_000)
    return f"{salt}${dk.hex()}"

def pw_verify(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
        return pw_hash(password, salt) == stored
    except: return False

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

def fmt_start(start_s):
    try:
        dt = datetime.fromisoformat(start_s.replace("Z", "+00:00"))
        return dt.strftime("%m월 %d일 %H:%M")
    except: return str(start_s or "")

def remain_text(end_s, start_s=None):
    now = now_kst()
    try:
        edt = datetime.fromisoformat(end_s.replace("Z", "+00:00"))
        if edt < now: return "종료됨"
        diff = edt - now
        mins = int(diff.total_seconds() // 60)
        if mins > 1440: return f"남음 {mins // 1440}일"
        if mins > 60: return f"남음 {mins // 60}시간"
        return f"남음 {mins}분"
    except: return ""

# =========================================================
# 2) FastAPI & Auth
# =========================================================
app = FastAPI(redirect_slashes=False)

def get_user_id_from_req(request: Request):
    t = request.cookies.get(COOKIE_NAME)
    if not t: return None
    with get_cursor() as cur:
        cur.execute("SELECT user_id, expires_at FROM sessions WHERE token=%s", (t,))
        row = cur.fetchone()
        if row and datetime.fromisoformat(row[1]) > now_kst(): return row[0]
    return None

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    # 인증 제외 경로
    if path == "/" or path.startswith(("/login", "/signup", "/send_email_otp", "/static", "/healthz")):
        return await call_next(request)
    
    uid = get_user_id_from_req(request)
    if not uid:
        if path.startswith("/api/"): return JSONResponse({"ok": False}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)

# =========================================================
# 3) 로그인 / 회원가입 (분할 입력 및 약관 복구)
# =========================================================

LOGIN_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>오세요 - 로그인</title>
  <style>
    body{font-family:Pretendard,sans-serif;background:#FAF9F6;margin:0;display:flex;justify-content:center;padding-top:60px;}
    .card{background:#fff;border:1px solid #E5E3DD;border-radius:20px;padding:30px;width:100%;max-width:380px;box-shadow:0 10px 25px rgba(0,0,0,0.05);}
    h1{font-size:24px;margin:0 0 20px;font-weight:800;text-align:center;}
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
      <div id="err_box">__ERROR_BLOCK__</div>
      <button class="btn">로그인</button>
    </form>
    <div class="link">계정이 없으신가요? <a href="/signup">회원가입</a></div>
  </div>
</body>
</html>
"""

SIGNUP_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>오세요 - 회원가입</title>
  <style>
    body{font-family:Pretendard,sans-serif;background:#FAF9F6;margin:0;display:flex;justify-content:center;padding:30px 10px;}
    .card{background:#fff;border:1px solid #E5E3DD;border-radius:20px;padding:24px;width:100%;max-width:480px;box-shadow:0 10px 25px rgba(0,0,0,0.05);}
    h1{font-size:22px;margin:0 0 10px;font-weight:800;text-align:center;}
    label{display:block;font-size:13px;margin:12px 0 6px;color:#444;font-weight:600;}
    input, select{width:100%;padding:12px;border:1px solid #e5e7eb;border-radius:12px;box-sizing:border-box;font-size:15px;}
    .email-row{display:flex;gap:8px;align-items:center;}
    .btn-verify{padding:10px 15px;background:#f3f4f6;border:0;border-radius:10px;font-size:13px;cursor:pointer;white-space:nowrap;margin-top:8px;}
    .btn-main{width:100%;padding:15px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;margin-top:20px;font-weight:700;}
    .terms-box{border:1px solid #e5e7eb;border-radius:12px;padding:12px;margin-top:15px;background:#f9fafb;}
    .term-item{display:flex;align-items:center;gap:8px;font-size:13px;margin-bottom:8px;color:#333;}
    .term-item input{width:16px;height:16px;margin:0;}
    .err{color:#ef4444;font-size:13px;margin-top:10px;text-align:center;}
    .ok{color:#10b981;font-size:13px;margin-top:10px;text-align:center;}
  </style>
</head>
<body>
  <div class="card">
    <h1>회원가입</h1>
    <form id="signupForm" method="post" action="/signup" onsubmit="return combineEmail()">
      <label>이메일</label>
      <div class="email-row">
        <input id="email_id" type="text" placeholder="아이디" required style="flex:1;"/>
        <span style="color:#888;font-weight:bold;">@</span>
        <select id="email_domain" style="flex:1;">
          <option value="naver.com">naver.com</option>
          <option value="gmail.com">gmail.com</option>
          <option value="daum.net">daum.net</option>
          <option value="kakao.com">kakao.com</option>
        </select>
      </div>
      <input type="hidden" id="full_email" name="email"/>
      <button type="button" class="btn-verify" onclick="sendOtp()">인증번호 발송</button>
      <div id="otp_status"></div>

      <label>인증번호</label>
      <input name="otp" placeholder="6자리 입력" required/>
      <label>비밀번호</label>
      <input name="password" type="password" required/>
      <label>이름</label>
      <input name="name" required/>

      <div class="terms-box">
        <div class="term-item"><input type="checkbox" id="all_agree" onclick="toggleAll(this)"> <b>전체 동의</b></div>
        <hr style="border:0; border-top:1px solid #e5e7eb; margin:10px 0;">
        <div class="term-item"><input type="checkbox" class="req" required> (필수) 만 14세 이상입니다.</div>
        <div class="term-item"><input type="checkbox" class="req" required> (필수) 이용약관 동의</div>
        <div class="term-item"><input type="checkbox" class="req" required> (필수) 개인정보 처리방침 동의</div>
      </div>
      <button class="btn-main">가입 완료</button>
    </form>
    __ERROR_BLOCK__
  </div>
  <script>
    function combineEmail() {
      const id = document.getElementById('email_id').value;
      const domain = document.getElementById('email_domain').value;
      document.getElementById('full_email').value = id + '@' + domain;
      return true;
    }
    async function sendOtp() {
      combineEmail();
      const email = document.getElementById('full_email').value;
      const status = document.getElementById('otp_status');
      if(!document.getElementById('email_id').value) { alert('이메일을 입력하세요'); return; }
      status.innerText = '발송 중...'; status.className = 'ok';
      const r = await fetch('/send_email_otp', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({email: email})
      });
      const d = await r.json();
      status.innerText = d.ok ? '인증번호가 발송되었습니다.' : (d.message || '실패');
      status.className = d.ok ? 'ok' : 'err';
    }
    function toggleAll(el) {
      document.querySelectorAll('.req').forEach(cb => cb.checked = el.checked);
    }
  </script>
</body>
</html>
"""

@app.get("/login")
async def login_get(err: str = ""):
    eb = f'<div class="err">{err}</div>' if err else ""
    return HTMLResponse(LOGIN_HTML.replace("__ERROR_BLOCK__", eb))

@app.post("/login")
async def login_post(email: str = Form(...), password: str = Form(...)):
    with get_cursor() as cur:
        cur.execute("SELECT id, pw_hash FROM users WHERE email=%s", (email.strip().lower(),))
        row = cur.fetchone()
        if row and pw_verify(password, row[1]):
            token = uuid.uuid4().hex
            cur.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES(%s,%s,%s)", (token, row[0], (now_kst() + timedelta(hours=SESSION_HOURS)).isoformat()))
            resp = RedirectResponse(url="/", status_code=303) # PWA 껍데기로 리다이렉트
            resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS*3600, httponly=True, samesite="lax", path="/")
            return resp
    return RedirectResponse(url="/login?err=LoginFail", status_code=303)

@app.get("/signup")
async def signup_get(err: str = ""):
    eb = f'<div class="err">{err}</div>' if err else ""
    return HTMLResponse(SIGNUP_HTML.replace("__ERROR_BLOCK__", eb))

@app.post("/send_email_otp")
async def send_email_otp(request: Request):
    try:
        data = await request.json()
        email = data.get("email", "").strip().lower()
        otp = "".join([str(random.randint(0, 9)) for _ in range(6)])
        with get_cursor() as cur:
            cur.execute("INSERT INTO email_otps (email, otp, expires_at) VALUES (%s,%s,%s) ON CONFLICT(email) DO UPDATE SET otp=EXCLUDED.otp", (email, otp, (now_kst()+timedelta(minutes=10)).isoformat()))
        # SMTP 발송 로직 (생략 - 필요시 환경변수 설정 확인)
        return JSONResponse({"ok": True})
    except: return JSONResponse({"ok": False})

@app.post("/signup")
async def signup_post(email: str = Form(...), otp: str = Form(...), password: str = Form(...), name: str = Form(...)):
    with get_cursor() as cur:
        cur.execute("SELECT otp FROM email_otps WHERE email=%s", (email.strip().lower(),))
        row = cur.fetchone()
        if not row or row[0] != otp: return RedirectResponse(url="/signup?err=OTP_Error", status_code=303)
        cur.execute("INSERT INTO users(id,email,pw_hash,name,created_at) VALUES(%s,%s,%s,%s,%s)", (uuid.uuid4().hex, email, pw_hash(password, "salt"), name.strip(), now_kst().isoformat()))
    return RedirectResponse(url="/login?err=SignupSuccess", status_code=303)

# =========================================================
# 4) PWA Shell & Gradio UI
# =========================================================

@app.get("/", response_class=HTMLResponse)
async def pwa_shell(request: Request):
    # 로그인 여부 확인
    uid = get_user_id_from_req(request)
    if not uid: return RedirectResponse(url="/login", status_code=303)
    
    return """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
  <link rel="manifest" href="/static/manifest.webmanifest"/><title>오세요</title>
  <style>html,body{height:100%;margin:0;background:#FAF9F6;overflow:hidden;}iframe{border:0;width:100%;height:100%;}</style>
</head>
<body>
  <iframe src="/app" title="오세요"></iframe>
  <script>if("serviceWorker" in navigator){navigator.serviceWorker.register("/static/sw.js");}</script>
</body>
</html>
"""

# Gradio UI (60개 카드)
MAX_CARDS = 60
with gr.Blocks(css=".event-card { border-radius:18px; padding:15px; background:white; box-shadow:0 4px 15px rgba(0,0,0,0.05); margin-bottom:10px; }") as demo:
    gr.Markdown("# 📍 지금 오세요")
    card_boxes = []; card_imgs = []; card_titles = []; card_metas = []; card_ids = []; card_btns = []
    with gr.Row():
        for i in range(MAX_CARDS):
            with gr.Column(visible=False, elem_classes=["event-card"], min_width=300) as box:
                img = gr.Image(show_label=False, interactive=False); title = gr.Markdown(); meta = gr.Markdown(); hid = gr.Textbox(visible=False); btn = gr.Button("참여하기")
                card_boxes.append(box); card_imgs.append(img); card_titles.append(title); card_metas.append(meta); card_ids.append(hid); card_btns.append(btn)
    
    def refresh_view(req: gr.Request):
        uid = get_user_id_from_req(req.request)
        with get_cursor() as cur:
            cur.execute('SELECT id,title,photo,"start","end",addr FROM events ORDER BY created_at DESC LIMIT %s', (MAX_CARDS,))
            events = cur.fetchall()
        updates = []
        for i in range(MAX_CARDS):
            if i < len(events):
                e = events[i]
                updates.extend([gr.update(visible=True), decode_photo(e[2]), f"### {e[1]}", f"📍 {e[5]}\n⏰ {fmt_start(e[3])}", e[0], "참여하기"])
            else:
                updates.extend([gr.update(visible=False), None, "", "", "", ""])
        return tuple(updates)

    demo.load(refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)

app = gr.mount_gradio_app(app, demo, path="/app")
app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
