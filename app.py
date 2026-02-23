# -*- coding: utf-8 -*-
print("### DEPLOY MARKER: V29_TOTAL_RESTORATION_FINAL ###", flush=True)
import os
import io
import re
import uuid
import json
import base64
import hashlib
import html
import random
import importlib
from datetime import datetime, timedelta, timezone

import uvicorn
import requests
from PIL import Image
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response

# --- PostgreSQL Library ---
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager

# --- Gradio hotfix ---
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
# 1) 유틸리티
# =========================================================
def pw_hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 150_000)
    return f"{salt}${dk.hex()}"

def pw_verify(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
        return pw_hash(password, salt) == stored
    except: return False

def render_safe(template: str, **kwargs) -> str:
    out = template
    for k, v in kwargs.items(): out = out.replace(f"__{k}__", str(v))
    return out

_DT_FORMATS = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y%m%d"]
def parse_dt(s):
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
    diff = edt - now
    mins = int(diff.total_seconds() // 60)
    if mins > 1440: return f"남음 {mins // 1440}일"
    if mins > 60: return f"남음 {mins // 60}시간"
    return f"남음 {mins}분"

def fmt_start(start_s):
    dt = parse_dt(start_s)
    return dt.strftime("%m월 %d일 %H:%M") if dt else (start_s or "").strip()

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
    if path == "/" or path.startswith(("/login", "/signup", "/send_email_otp", "/static", "/healthz")):
        return await call_next(request)
    uid = get_user_id_from_req(request)
    if not uid:
        if path.startswith("/api/"): return JSONResponse({"ok": False}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)

# =========================================================
# 3) 로그인 / 회원가입 화면 (원래 기능 모두 복구)
# =========================================================

LOGIN_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><title>오세요 - 로그인</title><style>body{font-family:Pretendard,sans-serif;background:#faf9f6;margin:0;display:flex;justify-content:center;padding-top:60px;}.card{background:#fff;border:1px solid #e5e3dd;border-radius:20px;padding:30px;width:100%;max-width:380px;box-shadow:0 10px 25px rgba(0,0,0,0.05);}h1{font-size:24px;margin:0 0 20px;font-weight:800;}label{display:block;font-size:13px;margin-bottom:8px;color:#666;}input{width:100%;padding:14px;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:18px;box-sizing:border-box;font-size:15px;}.btn{width:100%;padding:15px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;font-weight:700;font-size:16px;}.err{color:#ef4444;font-size:13px;margin-bottom:15px;text-align:center;}.link{text-align:center;margin-top:20px;font-size:14px;color:#888;}a{color:#111;text-decoration:none;font-weight:700;margin-left:5px;}</style></head><body><div class="card"><h1>로그인</h1><form method="post" action="/login"><label>이메일</label><input name="email" type="email" required/><label>비밀번호</label><input name="password" type="password" required/><button class="btn">로그인</button></form>__ERROR_BLOCK__<div class="link">계정이 없으신가요? <a href="/signup">회원가입</a></div></div></body></html>"""

# ⭐ 사용자님이 요청하신 분할 이메일 + 약관 동의 UI 복구
SIGNUP_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>오세요 - 회원가입</title>
  <style>
    body{font-family:Pretendard,sans-serif;background:#faf9f6;margin:0;display:flex;justify-content:center;padding:30px 10px;}
    .card{background:#fff;border:1px solid #e5e3dd;border-radius:20px;padding:24px;width:100%;max-width:480px;box-shadow:0 10px 25px rgba(0,0,0,0.05);}
    h1{font-size:22px;margin:0 0 10px;font-weight:800;text-align:center;}
    label{display:block;font-size:13px;margin:12px 0 6px;color:#444;font-weight:600;}
    input, select{width:100%;padding:12px;border:1px solid #e5e7eb;border-radius:12px;box-sizing:border-box;font-size:15px;}
    .email-row{display:flex;gap:8px;align-items:center;}
    .at{color:#888;font-weight:bold;}
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
        <span class="at">@</span>
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
        <div class="term-item"><input type="checkbox" name="marketing"> (선택) 마케팅 정보 수신 동의</div>
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
      if(!document.getElementById('email_id').value) { alert('이메일 아이디를 입력하세요'); return; }
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
      const cbs = document.querySelectorAll('input[type="checkbox"]');
      cbs.forEach(cb => cb.checked = el.checked);
    }
  </script>
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
            cur.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES(%s,%s,%s)", (token, row[0], (now_kst() + timedelta(hours=SESSION_HOURS)).isoformat()))
            resp = RedirectResponse(url="/app", status_code=303)
            resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS*3600, httponly=True, samesite="lax", path="/")
            return resp
    return RedirectResponse(url="/login?err=" + requests.utils.quote("정보가 일치하지 않습니다."), status_code=303)

@app.get("/signup")
async def signup_get(err: str = ""):
    eb = f'<div class="err">{html.escape(err)}</div>' if err else ""
    return HTMLResponse(render_safe(SIGNUP_HTML, ERROR_BLOCK=eb))

@app.post("/send_email_otp")
async def send_email_otp(request: Request):
    try:
        data = await request.json()
        email = data.get("email", "").strip().lower()
        otp = "".join([str(random.randint(0, 9)) for _ in range(6)])
        exp = (now_kst() + timedelta(minutes=10)).isoformat()
        with get_cursor() as cur:
            cur.execute("INSERT INTO email_otps (email, otp, expires_at) VALUES (%s,%s,%s) ON CONFLICT(email) DO UPDATE SET otp=EXCLUDED.otp, expires_at=EXCLUDED.expires_at", (email, otp, exp))
        
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(f"오세요 인증번호: {otp}", "plain", "utf-8")
        msg["Subject"] = "[오세요] 인증번호"
        msg["From"] = os.getenv("FROM_EMAIL")
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
        if not row or row[0] != otp: return RedirectResponse(url="/signup?err=인증번호가 틀렸습니다.", status_code=303)
        cur.execute("SELECT 1 FROM users WHERE email=%s", (email,))
        if cur.fetchone(): return RedirectResponse(url="/signup?err=이미 가입된 이메일입니다.", status_code=303)
        uid = uuid.uuid4().hex
        cur.execute("INSERT INTO users(id,email,pw_hash,name,created_at) VALUES(%s,%s,%s,%s,%s)", (uid, email, pw_hash(password, "salt"), name.strip(), now_kst().isoformat()))
    return RedirectResponse(url="/login?err=회원가입 성공! 로그인 하세요.", status_code=303)

# =========================================================
# 4) Gradio UI (60개 카드 복구)
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
    cur.execute("SELECT event_id, COUNT(*) FROM event_participants WHERE event_id = ANY(%s) GROUP BY event_id", (event_ids,))
    for eid, cnt in cur.fetchall(): counts[eid] = int(cnt)
    if user_id:
        cur.execute("SELECT event_id FROM event_participants WHERE user_id=%s AND event_id = ANY(%s)", (user_id, event_ids))
        for (eid,) in cur.fetchall(): joined[eid] = True
    return counts, joined

def get_joined_event_id(user_id: str):
    if not user_id: return None
    with get_cursor() as cur:
        cur.execute('SELECT p.event_id FROM event_participants p LEFT JOIN events e ON e.id=p.event_id WHERE p.user_id=%s', (user_id,))
        rows = cur.fetchall()
    for (eid,) in rows: return eid
    return None

def list_active_events(limit: int = 500):
    with get_cursor() as cur:
        cur.execute('SELECT id,title,photo,"start","end",addr,lat,lng,created_at,user_id,capacity,is_unlimited FROM events ORDER BY created_at DESC LIMIT %s', (limit,))
        rows = cur.fetchall()
    keys = ["id","title","photo","start","end","addr","lat","lng","created_at","user_id","capacity","is_unlimited"]
    return [dict(zip(keys, r)) for r in rows]

MAX_CARDS = 60
CSS = r"""
:root { --accent: #ff5a1f; }
.event-card { border-radius: 18px; padding: 15px; background: white; box-shadow: 0 4px 15px rgba(0,0,0,0.05); margin-bottom:12px; }
.event-img img { border-radius:14px !important; height:180px !important; object-fit:cover !important; }
#fab_btn { position: fixed; right: 22px; bottom: 22px; z-index: 9999; width: 56px; height: 56px; border-radius: 999px; background: #ff5a1f; color: white; font-size: 28px; border: 0; box-shadow: 0 10px 20px rgba(0,0,0,0.2); cursor:pointer;}
"""

def refresh_view(req: gr.Request):
    uid = get_user_id_from_req(req.request)
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
            btn_label = "빠지기" if is_joined else ("마감" if (cap != "∞" and cnt >= int(cap)) else "참여하기")
            updates.extend([gr.update(visible=True), decode_photo(e["photo"]), f"### {e['title']}", f"📍 {e['addr']}\n⏰ {fmt_start(e['start'])} · {remain_text(e['end'], e['start'])}\n👥 {cnt}/{cap}", eid, btn_label])
        else:
            updates.extend([gr.update(visible=False), None, "", "", "", ""])
    return tuple(updates)

def toggle_join_gr(eid, req: gr.Request):
    uid = get_user_id_from_req(req.request)
    if not uid or not eid: return refresh_view(req)
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM event_participants WHERE event_id=%s AND user_id=%s", (eid, uid))
        if cur.fetchone(): cur.execute("DELETE FROM event_participants WHERE event_id=%s AND user_id=%s", (eid, uid))
        else:
            if get_joined_event_id(uid): return refresh_view(req)
            cur.execute("INSERT INTO event_participants (event_id, user_id, joined_at) VALUES (%s, %s, %s)", (eid, uid, now_kst().isoformat()))
    return refresh_view(req)

with gr.Blocks(css=CSS) as demo:
    gr.Markdown("# 📍 지금 오세요")
    card_boxes = []; card_imgs = []; card_titles = []; card_metas = []; card_ids = []; card_btns = []
    with gr.Row():
        for i in range(MAX_CARDS):
            with gr.Column(visible=False, elem_classes=["event-card"], min_width=300) as box:
                img = gr.Image(show_label=False, interactive=False); title = gr.Markdown(); meta = gr.Markdown(); hid = gr.Textbox(visible=False); btn = gr.Button("참여하기")
                card_boxes.append(box); card_imgs.append(img); card_titles.append(title); card_metas.append(meta); card_ids.append(hid); card_btns.append(btn)
    fab = gr.Button("＋", elem_id="fab_btn")
    demo.load(refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)
    for i in range(MAX_CARDS):
        card_btns[i].click(toggle_join_gr, inputs=[card_ids[i]], outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)

app = gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
