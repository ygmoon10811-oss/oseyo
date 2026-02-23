# -*- coding: utf-8 -*-
print("### DEPLOY MARKER: V31_ULTIMATE_FINAL_FIX ###", flush=True)
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
from fastapi import FastAPI, Request, Form
# 응답 클래스 임포트 경로 최적화 (가장 안전한 방식)
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# --- PostgreSQL Connection Pool ---
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager

import gradio as gr

# =========================================================
# 0) 설정 및 시간
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

# DB 풀 생성 (에러 방지용 try-except)
db_pool = None
try:
    if DATABASE_URL:
        db_pool = psycopg2.pool.ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)
        print("[DB] PostgreSQL MEGA Pool Initialized.")
except Exception as e:
    print(f"[DB] Connection Pool Error: {e}")

@contextmanager
def get_cursor():
    global db_pool
    if not db_pool:
        raise Exception("Database Pool is not initialized.")
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
    try:
        with get_cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, pw_hash TEXT, name TEXT, gender TEXT, birth TEXT, created_at TEXT);")
            cur.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT, expires_at TEXT);")
            cur.execute("CREATE TABLE IF NOT EXISTS email_otps (email TEXT PRIMARY KEY, otp TEXT, expires_at TEXT);")
            cur.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, title TEXT, photo TEXT, "start" TEXT, "end" TEXT, addr TEXT, lat DOUBLE PRECISION, lng DOUBLE PRECISION, created_at TEXT, user_id TEXT, capacity INTEGER DEFAULT 10, is_unlimited INTEGER DEFAULT 0);')
            cur.execute("CREATE TABLE IF NOT EXISTS favs (name TEXT PRIMARY KEY, count INTEGER DEFAULT 1);")
            cur.execute("CREATE TABLE IF NOT EXISTS event_participants (event_id TEXT, user_id TEXT, joined_at TEXT, PRIMARY KEY(event_id, user_id));")
    except Exception as e:
        print(f"[DB] Init Error (Check Supabase status): {e}")

if db_pool: init_db()

# =========================================================
# 1) 유틸리티 (보안, 이미지, 날짜)
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
        dt = datetime.fromisoformat(str(start_s).replace("Z", "+00:00"))
        return dt.strftime("%m월 %d일 %H:%M")
    except: return str(start_s or "")

def remain_text(end_s, start_s=None):
    now = now_kst()
    try:
        dt_str = str(end_s if end_s else start_s).replace("Z", "+00:00")
        edt = datetime.fromisoformat(dt_str)
        if not end_s: edt = edt.replace(hour=23, minute=59)
        if edt < now: return "종료됨"
        diff = edt - now
        mins = int(diff.total_seconds() // 60)
        if mins > 1440: return f"남음 {mins // 1440}일"
        if mins > 60: return f"남음 {mins // 60}시간"
        return f"남음 {mins}분"
    except: return ""

# =========================================================
# 2) FastAPI 설정 및 미들웨어 (Health Check 최우선)
# =========================================================
app = FastAPI(redirect_slashes=False)

def get_user_id_from_req(request: Request):
    t = request.cookies.get(COOKIE_NAME)
    if not t: return None
    try:
        with get_cursor() as cur:
            cur.execute("SELECT user_id, expires_at FROM sessions WHERE token=%s", (t,))
            row = cur.fetchone()
            if row and datetime.fromisoformat(row[1]) > now_kst(): return row[0]
    except: pass
    return None

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    # 헬스체크 및 공개 경로는 무조건 통과
    if path == "/healthz": return await call_next(request)
    if path == "/" or path.startswith(("/login", "/signup", "/send_email_otp", "/static")):
        return await call_next(request)
    
    uid = get_user_id_from_req(request)
    if not uid:
        if path.startswith("/api/"): return JSONResponse({"ok": False}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)

@app.get("/healthz")
async def healthz(): return {"status": "ok", "db": db_pool is not None}

# (Part 2/2 에서 로그인/회원가입 UI 및 Gradio UI가 이어집니다...)
# =========================================================
# 3) 로그인 / 회원가입 화면 (분할 이메일 및 약관 동의 복구)
# =========================================================

LOGIN_HTML = """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"/>
  <title>오세요 - 로그인</title>
  <style>
    body{font-family:Pretendard,sans-serif;background:#FAF9F6;margin:0;display:flex;justify-content:center;padding-top:60px;}
    .card{background:#fff;border:1px solid #E5E3DD;border-radius:20px;padding:30px;width:100%;max-width:380px;box-shadow:0 10px 25px rgba(0,0,0,0.05);}
    h1{font-size:24px;margin:0 0 20px;font-weight:800;text-align:center;}
    label{display:block;font-size:13px;margin-bottom:8px;color:#666;}
    input{width:100%;padding:14px;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:18px;box-sizing:border-box;font-size:15px;}
    .btn{width:100%;padding:15px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;font-weight:700;font-size:16px;}
    .err{color:#ef4444;font-size:13px;margin-bottom:15px;text-align:center;background:#fee2e2;padding:8px;border-radius:8px;}
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
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"/>
  <title>오세요 - 회원가입</title>
  <style>
    body{font-family:Pretendard,sans-serif;background:#FAF9F6;margin:0;display:flex;justify-content:center;padding:30px 10px;}
    .card{background:#fff;border:1px solid #E5E3DD;border-radius:20px;padding:24px;width:100%;max-width:480px;box-shadow:0 12px 30px rgba(0,0,0,0.05);}
    h1{font-size:22px;margin:0 0 10px;font-weight:800;text-align:center;}
    label{display:block;font-size:13px;margin:12px 0 6px;color:#444;font-weight:600;}
    input, select{width:100%;padding:12px;border:1px solid #e5e7eb;border-radius:12px;box-sizing:border-box;font-size:15px;}
    .email-row{display:flex;gap:8px;align-items:center;}
    .btn-verify{padding:10px 15px;background:#f3f4f6;border:0;border-radius:10px;font-size:13px;cursor:pointer;white-space:nowrap;margin-top:8px;font-weight:600;}
    .btn-main{width:100%;padding:15px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;margin-top:20px;font-weight:700;}
    .terms-box{border:1px solid #e5e7eb;border-radius:12px;padding:12px;margin-top:15px;background:#f9fafb;}
    .term-item{display:flex;align-items:center;gap:8px;font-size:13px;margin-bottom:8px;color:#333;}
    .term-item input{width:18px;height:18px;margin:0;cursor:pointer;}
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
    <div style="text-align:center; margin-top:15px; font-size:14px;"><a href="/login" style="color:#888; text-decoration:none;">로그인으로 돌아가기</a></div>
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
      status.innerText = '인증번호 발송 중...'; status.className = 'ok';
      try {
        const r = await fetch('/send_email_otp', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({email: email})
        });
        const d = await r.json();
        status.innerText = d.ok ? '인증번호가 발송되었습니다.' : (d.message || '발송 실패');
        status.className = d.ok ? 'ok' : 'err';
      } catch(e) { status.innerText = '네트워크 오류'; status.className = 'err'; }
    }
    function toggleAll(el) {
      document.querySelectorAll('.req').forEach(cb => cb.checked = el.checked);
    }
  </script>
</body>
</html>
"""

def render_safe(template: str, **kwargs) -> str:
    out = template
    for k, v in kwargs.items(): out = out.replace(f"__{k}__", str(v))
    return out

@app.get("/login")
async def login_get(err: str = ""):
    eb = f'<div class="err">{html.escape(err)}</div>' if err else ""
    return HTMLResponse(render_safe(LOGIN_HTML, ERROR_BLOCK=eb))

@app.post("/login")
async def login_post(email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    try:
        with get_cursor() as cur:
            cur.execute("SELECT id, pw_hash FROM users WHERE email=%s", (email,))
            row = cur.fetchone()
            if row and pw_verify(password, row[1]):
                token = uuid.uuid4().hex
                cur.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES(%s,%s,%s)", 
                            (token, row[0], (now_kst() + timedelta(hours=SESSION_HOURS)).isoformat()))
                resp = RedirectResponse(url="/", status_code=303)
                resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS*3600, httponly=True, samesite="lax", path="/")
                return resp
    except: pass
    return RedirectResponse(url="/login?err=" + requests.utils.quote("정보가 일치하지 않습니다."), status_code=303)

@app.get("/signup")
async def signup_get(err: str = ""):
    eb = f'<div class="err">{html.escape(err)}</div>' if err else ""
    return HTMLResponse(render_safe(SIGNUP_HTML, ERROR_BLOCK=eb))

@app.post("/signup")
async def signup_post(email: str = Form(...), otp: str = Form(...), password: str = Form(...), name: str = Form(...)):
    email = email.strip().lower()
    try:
        with get_cursor() as cur:
            cur.execute("SELECT otp, expires_at FROM email_otps WHERE email=%s", (email,))
            row = cur.fetchone()
            if not row or row[0] != otp or datetime.fromisoformat(row[1]) < now_kst():
                return RedirectResponse(url="/signup?err=인증오류", status_code=303)
            cur.execute("SELECT 1 FROM users WHERE email=%s", (email,))
            if cur.fetchone(): return RedirectResponse(url="/signup?err=이미가입", status_code=303)
            uid = uuid.uuid4().hex
            cur.execute("INSERT INTO users(id,email,pw_hash,name,created_at) VALUES(%s,%s,%s,%s,%s)", 
                        (uid, email, pw_hash(password, "salt"), name.strip(), now_kst().isoformat()))
        return RedirectResponse(url="/login?err=가입완료", status_code=303)
    except: return RedirectResponse(url="/signup?err=서버오류", status_code=303)

@app.post("/send_email_otp")
async def send_email_otp(request: Request):
    try:
        data = await request.json()
        email = data.get("email", "").strip().lower()
        otp = "".join([str(random.randint(0, 9)) for _ in range(6)])
        exp = (now_kst() + timedelta(minutes=10)).isoformat()
        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO email_otps (email, otp, expires_at) VALUES (%s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET otp=EXCLUDED.otp, expires_at=EXCLUDED.expires_at
            """, (email, otp, exp))
        
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

# =========================================================
# 4) Gradio UI (60개 카드 및 모달 기능 완벽 복구)
# =========================================================

MAX_CARDS = 60
CSS = r"""
:root { --bg:#FAF9F6; --ink:#1F2937; --muted:#6B7280; --accent:#ff5a1f; }
html, body, .gradio-container { background: var(--bg) !important; font-family: 'Pretendard', sans-serif; }
.event-card { background: white; border:1px solid #E5E3DD; border-radius:18px; padding:14px; box-shadow:0 8px 22px rgba(0,0,0,0.06); margin-bottom:12px; }
.event-img img { width:100% !important; border-radius:14px !important; height:180px !important; object-fit:cover !important; }
.join-btn button { border-radius:999px !important; background: var(--accent) !important; color: white !important; font-weight:800 !important; border:0 !important; }
#fab_btn {
  position: fixed !important; right: 22px !important; bottom: 22px !important; z-index: 9999 !important;
  width: 56px !important; height: 56px !important; border-radius: 999px !important;
  background: var(--accent) !important; color: white !important; font-size: 28px !important; font-weight: 900 !important;
  border: 0 !important; box-shadow: 0 12px 28px rgba(255, 90, 31, 0.3) !important; cursor: pointer !important;
}
.main-modal { position: fixed; left:50%; top:50%; transform: translate(-50%,-50%); width: 90%; max-width: 500px; background: white; border-radius: 20px; z-index: 70; padding: 20px; box-shadow: 0 20px 50px rgba(0,0,0,0.2); }
.fav-btn button { background: #f3f4f6 !important; border: 1px solid #e5e7eb !important; border-radius: 10px !important; color: #333 !important; font-size: 13px !important; }
"""

def _event_capacity_label(capacity, is_unlimited) -> str:
    if is_unlimited == 1: return "∞"
    try:
        cap_i = int(float(capacity or 0))
        return "∞" if cap_i <= 0 else str(cap_i)
    except: return "∞"

def refresh_view(req: gr.Request):
    uid = get_user_id_from_req(req.request)
    events = []
    try:
        with get_cursor() as cur:
            cur.execute('SELECT id,title,photo,"start","end",addr,capacity,is_unlimited FROM events ORDER BY created_at DESC LIMIT %s', (MAX_CARDS,))
            rows = cur.fetchall()
            for r in rows:
                if is_active_event(r[4], r[3]):
                    # 참여 수 및 내 참여 여부 조회
                    cur.execute("SELECT COUNT(*) FROM event_participants WHERE event_id=%s", (r[0],))
                    cnt = cur.fetchone()[0]
                    cur.execute("SELECT 1 FROM event_participants WHERE event_id=%s AND user_id=%s", (r[0], uid))
                    is_joined = cur.fetchone() is not None
                    events.append({'id':r[0],'title':r[1],'photo':r[2],'start':r[3],'end':r[4],'addr':r[5],'capacity':r[6],'is_unlimited':r[7],'count':cnt,'joined':is_joined})
    except: pass
    
    updates = []
    for i in range(MAX_CARDS):
        if i < len(events):
            e = events[i]; cap = _event_capacity_label(e['capacity'], e['is_unlimited'])
            btn_label = "빠지기" if e['joined'] else ("마감" if (cap != "∞" and e['count'] >= int(cap)) else "참여하기")
            updates.extend([gr.update(visible=True), decode_photo(e['photo']), f"### {e['title']}", f"📍 {e['addr']}\n⏰ {fmt_start(e['start'])} · {remain_text(e['end'], e['start'])}\n👥 {e['count']}/{cap}", e['id'], btn_label])
        else:
            updates.extend([gr.update(visible=False), None, "", "", "", ""])
    return tuple(updates)

def toggle_join_gr(eid, req: gr.Request):
    uid = get_user_id_from_req(req.request)
    if not uid or not eid: return refresh_view(req)
    try:
        with get_cursor() as cur:
            cur.execute("SELECT 1 FROM event_participants WHERE event_id=%s AND user_id=%s", (eid, uid))
            if cur.fetchone(): cur.execute("DELETE FROM event_participants WHERE event_id=%s AND user_id=%s", (eid, uid))
            else: cur.execute("INSERT INTO event_participants (event_id, user_id, joined_at) VALUES (%s, %s, %s)", (eid, uid, now_kst().isoformat()))
    except: pass
    return refresh_view(req)

def save_event_gr(title, img_np, addr, cap, unlim, req: gr.Request):
    uid = get_user_id_from_req(req.request)
    if not title or not addr: return gr.update(visible=True)
    try:
        photo_b64 = encode_img_to_b64(img_np)
        eid = uuid.uuid4().hex
        with get_cursor() as cur:
            cur.execute('INSERT INTO events (id, title, photo, "start", "end", addr, lat, lng, created_at, user_id, capacity, is_unlimited) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (eid, title, photo_b64, now_kst().isoformat(), (now_kst()+timedelta(hours=2)).isoformat(), addr, 36.019, 129.343, now_kst().isoformat(), uid, int(cap), 1 if unlim else 0))
            cur.execute("INSERT INTO favs(name, count) VALUES(%s, 1) ON CONFLICT(name) DO UPDATE SET count = favs.count + 1", (title.strip(),))
        return gr.update(visible=False)
    except: return gr.update(visible=True)

with gr.Blocks(css=CSS, title="오세요") as demo:
    gr.Markdown("# 📍 지금 오세요")
    card_boxes=[]; card_imgs=[]; card_titles=[]; card_metas=[]; card_ids=[]; card_btns=[]
    with gr.Row():
        for i in range(MAX_CARDS):
            with gr.Column(visible=False, elem_classes=["event-card"], min_width=300) as box:
                img = gr.Image(show_label=False, interactive=False, elem_classes=["event-img"])
                title = gr.Markdown(); meta = gr.Markdown(); hid = gr.Textbox(visible=False)
                btn = gr.Button("참여하기", variant="primary", elem_classes=["join-btn"])
                card_boxes.append(box); card_imgs.append(img); card_titles.append(title); card_metas.append(meta); card_ids.append(hid); card_btns.append(btn)
    
    fab = gr.Button("＋", elem_id="fab_btn")
    
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal:
        gr.Markdown("### 📝 활동 만들기")
        t = gr.Textbox(label="활동명", placeholder="예: 공원 산책"); img = gr.Image(label="사진", type="numpy")
        a = gr.Textbox(label="장소", placeholder="주소 입력")
        with gr.Row(): cp = gr.Slider(1, 50, value=10, label="정원"); un = gr.Checkbox(label="제한없음")
        with gr.Row(): sub = gr.Button("등록", variant="primary"); cls = gr.Button("닫기")

    demo.load(refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)
    fab.click(lambda: gr.update(visible=True), outputs=modal)
    cls.click(lambda: gr.update(visible=False), outputs=modal)
    sub.click(save_event_gr, [t, img, a, cp, un], modal).then(refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)
    for i in range(MAX_CARDS):
        card_btns[i].click(toggle_join_gr, inputs=[card_ids[i]], outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)

# =========================================================
# 6) PWA Shell & 최종 실행
# =========================================================

@app.get("/", response_class=HTMLResponse)
async def pwa_shell(request: Request):
    if not get_user_id_from_req(request): return RedirectResponse(url="/login", status_code=303)
    return """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
  <link rel="manifest" href="/static/manifest.webmanifest"/><title>오세요</title>
  <style>html,body{height:100%;margin:0;background:#FAF9F6;overflow:hidden;}iframe{border:0;width:100%;height:100%;vertical-align:bottom;}</style>
</head>
<body>
  <iframe src="/app" title="오세요"></iframe>
  <script>if("serviceWorker" in navigator){navigator.serviceWorker.register("/static/sw.js");}</script>
</body>
</html>
"""

app = gr.mount_gradio_app(app, demo, path="/app")
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except: pass

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
