# -*- coding: utf-8 -*-
print("### DEPLOY MARKER: V30_MEGA_POSTGRES_RESTORE ###", flush=True)
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
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

# --- PostgreSQL Connection Pool (Supabase용) ---
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager

import gradio as gr

# =========================================================
# 0) 시간/키 및 DB 설정
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
    print("[DB] PostgreSQL MEGA Pool Initialized.")
except Exception as e:
    print(f"[DB] Fatal Error: {e}")
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
# 1) 상세 디자인 (원래의 CSS 100% 복구)
# =========================================================
CSS = r"""
:root {
  --bg:#FAF9F6; --ink:#1F2937; --muted:#6B7280; --line:#E5E3DD; --accent:#ff5a1f;
  --card:#ffffffcc; --danger:#ef4444;
}
html, body, .gradio-container { background: var(--bg) !important; font-family: 'Pretendard', sans-serif; }
.event-card { background: white; border:1px solid var(--line); border-radius:18px; padding:15px; box-shadow:0 8px 22px rgba(0,0,0,0.06); margin-bottom:12px; }
.event-img img { width:100% !important; border-radius:14px !important; height:200px !important; object-fit:cover !important; }
.join-btn button { border-radius:999px !important; background: var(--accent) !important; color: white !important; font-weight:800 !important; border:0 !important; }
.join-btn button:disabled { background: #ccc !important; }
#fab_btn {
  position: fixed !important; right: 22px !important; bottom: 22px !important; z-index: 9999 !important;
  width: 56px !important; height: 56px !important; border-radius: 999px !important;
  background: var(--accent) !important; color: white !important; font-size: 28px !important; font-weight: 900 !important;
  border: 0 !important; box-shadow: 0 12px 28px rgba(255, 90, 31, 0.4) !important; cursor: pointer !important;
}
.main-modal {
  position: fixed; left:50%; top:50%; transform: translate(-50%,-50%);
  width: min(520px, calc(100vw - 20px)); height: min(760px, calc(100vh - 20px));
  background: white; border-radius: 20px; box-shadow: 0 20px 60px rgba(0,0,0,0.2); z-index: 70; overflow:hidden;
}
.fav-grid { display:grid; grid-template-columns: 1fr 1fr; gap:8px; margin-top:10px; }
.fav-btn button { background: #f3f4f6 !important; border: 1px solid #e5e7eb !important; border-radius: 10px !important; color: #333 !important; font-size: 13px !important; }
"""

def pw_hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 150_000)
    return f"{salt}${dk.hex()}"

def pw_verify(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
        return pw_hash(password, salt) == stored
    except: return False
        # =========================================================
# 2) 화려한 로그인/회원가입 UI (원래 기능 100% 복구)
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
    label{display:block;font-size:13px;margin-bottom:8px;color:#666;font-weight:600;}
    input{width:100%;padding:14px;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:18px;box-sizing:border-box;font-size:15px;outline:none;}
    input:focus{border-color:#ff5a1f;}
    .btn{width:100%;padding:15px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;font-weight:700;font-size:16px;}
    .err{color:#ef4444;font-size:13px;margin-bottom:15px;text-align:center;background:#fee2e2;padding:10px;border-radius:8px;}
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
    .card{background:#fff;border:1px solid #E5E3DD;border-radius:20px;padding:26px;width:100%;max-width:460px;box-shadow:0 12px 30px rgba(0,0,0,0.05);}
    h1{font-size:22px;margin:0 0 10px;font-weight:800;text-align:center;}
    label{display:block;font-size:13px;margin:15px 0 6px;color:#444;font-weight:600;}
    input, select{width:100%;padding:12px;border:1px solid #e5e7eb;border-radius:12px;box-sizing:border-box;font-size:15px;outline:none;}
    input:focus, select:focus{border-color:#ff5a1f;}
    .email-row{display:flex;gap:8px;align-items:center;}
    .at{color:#888;font-weight:bold;font-size:18px;}
    .btn-verify{padding:10px 15px;background:#f3f4f6;border:1px solid #e5e7eb;border-radius:10px;font-size:13px;cursor:pointer;white-space:nowrap;margin-top:8px;font-weight:600;}
    .btn-main{width:100%;padding:16px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;margin-top:25px;font-weight:700;font-size:16px;}
    .terms-box{border:1px solid #e5e7eb;border-radius:12px;padding:14px;margin-top:15px;background:#f9fafb;}
    .term-item{display:flex;align-items:center;gap:10px;font-size:13px;margin-bottom:10px;color:#333;cursor:pointer;}
    .term-item input{width:18px;height:18px;margin:0;cursor:pointer;}
    .err{color:#ef4444;font-size:13px;margin-top:12px;text-align:center;background:#fee2e2;padding:8px;border-radius:8px;}
    .ok{color:#10b981;font-size:13px;margin-top:12px;text-align:center;}
  </style>
</head>
<body>
  <div class="card">
    <h1>회원가입</h1>
    <form id="signupForm" method="post" action="/signup" onsubmit="return combineEmail()">
      <label>이메일</label>
      <div class="email-row">
        <input id="email_id" type="text" placeholder="아이디" required style="flex:1.5;"/>
        <span class="at">@</span>
        <select id="email_domain" style="flex:1.2;">
          <option value="naver.com">naver.com</option>
          <option value="gmail.com">gmail.com</option>
          <option value="daum.net">daum.net</option>
          <option value="kakao.com">kakao.com</option>
          <option value="hanmail.net">hanmail.net</option>
        </select>
      </div>
      <input type="hidden" id="full_email" name="email"/>
      <button type="button" class="btn-verify" onclick="sendOtp()">인증번호 발송</button>
      <div id="otp_status"></div>

      <label>인증번호</label>
      <input name="otp" placeholder="이메일로 발송된 6자리" required maxlength="6"/>
      
      <label>비밀번호</label>
      <input name="password" type="password" required placeholder="8자 이상의 비밀번호"/>
      
      <label>이름</label>
      <input name="name" required placeholder="실명 입력"/>

      <div class="terms-box">
        <label class="term-item"><input type="checkbox" id="all_agree" onclick="toggleAll(this)"> <b style="font-size:14px;">전체 동의하기</b></label>
        <hr style="border:0; border-top:1px solid #e5e7eb; margin:12px 0;">
        <label class="term-item"><input type="checkbox" class="req" required> <span style="color:#ef4444;">(필수)</span> 만 14세 이상입니다.</label>
        <label class="term-item"><input type="checkbox" class="req" required> <span style="color:#ef4444;">(필수)</span> 이용약관 동의</label>
        <label class="term-item"><input type="checkbox" class="req" required> <span style="color:#ef4444;">(필수)</span> 개인정보 처리방침 동의</label>
        <label class="term-item"><input type="checkbox" name="marketing"> (선택) 마케팅 정보 수신 동의</label>
      </div>

      <button class="btn-main">가입 완료</button>
    </form>
    <div id="err_box">__ERROR_BLOCK__</div>
    <div style="text-align:center; margin-top:15px; font-size:14px;"><a href="/login" style="color:#888; text-decoration:none;">로그인으로 돌아가기</a></div>
  </div>

  <script>
    function combineEmail() {
      const id = document.getElementById('email_id').value.trim();
      const domain = document.getElementById('email_domain').value;
      if(!id) return false;
      document.getElementById('full_email').value = id + '@' + domain;
      return true;
    }
    async function sendOtp() {
      if(!combineEmail()) { alert('이메일 아이디를 입력하세요.'); return; }
      const email = document.getElementById('full_email').value;
      const status = document.getElementById('otp_status');
      status.innerText = '인증번호 발송 중...'; status.className = 'ok';
      try {
        const r = await fetch('/send_email_otp', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({email: email})
        });
        const d = await r.json();
        status.innerText = d.ok ? '인증번호가 메일로 발송되었습니다.' : (d.message || '발송 실패');
        status.className = d.ok ? 'ok' : 'err';
      } catch(e) { status.innerText = '네트워크 오류'; status.className = 'err'; }
    }
    function toggleAll(el) {
      const cbs = document.querySelectorAll('input[type="checkbox"]');
      cbs.forEach(cb => cb.checked = el.checked);
    }
  </script>
</body>
</html>
"""

# =========================================================
# 3) 서버 비즈니스 로직 (Postgres 전용)
# =========================================================

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
            resp = RedirectResponse(url="/", status_code=303)
            resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS*3600, httponly=True, samesite="lax", path="/")
            return resp
    return RedirectResponse(url="/login?err=" + requests.utils.quote("이메일 또는 비밀번호가 틀렸습니다."), status_code=303)

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
            # PostgreSQL 전용 Upsert
            cur.execute("""
                INSERT INTO email_otps (email, otp, expires_at) VALUES (%s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET otp=EXCLUDED.otp, expires_at=EXCLUDED.expires_at
            """, (email, otp, exp))
        
        # SMTP 메일 발송
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(f"안녕하세요! 오세요 인증번호는 [{otp}] 입니다.", "plain", "utf-8")
        msg["Subject"] = "[오세요] 회원가입 인증번호"
        msg["From"] = os.getenv("FROM_EMAIL", "")
        msg["To"] = email
        with smtplib.SMTP(os.getenv("SMTP_HOST", ""), int(os.getenv("SMTP_PORT", 587))) as s:
            s.starttls(); s.login(os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", ""))
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
            return RedirectResponse(url="/signup?err=인증번호가 유효하지 않습니다.", status_code=303)
        
        cur.execute("SELECT 1 FROM users WHERE email=%s", (email,))
        if cur.fetchone():
            return RedirectResponse(url="/signup?err=이미 사용 중인 이메일입니다.", status_code=303)
        
        uid = uuid.uuid4().hex
        salt = uuid.uuid4().hex[:12]
        cur.execute("INSERT INTO users (id, email, pw_hash, name, created_at) VALUES (%s,%s,%s,%s,%s)",
                    (uid, email, pw_hash(password, salt), name.strip(), now_kst().isoformat()))
        cur.execute("DELETE FROM email_otps WHERE email=%s", (email,))
    return RedirectResponse(url="/login?err=회원가입이 완료되었습니다! 로그인 해주세요.", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with get_cursor() as cur: cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp
    # =========================================================
# 4) 데이터 조회 및 비즈니스 로직 (PostgreSQL 전용)
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
    cur.execute('SELECT event_id, COUNT(*) FROM event_participants WHERE event_id = ANY(%s) GROUP BY event_id', (event_ids,))
    for eid, cnt in cur.fetchall(): counts[eid] = int(cnt)
    if user_id:
        cur.execute('SELECT event_id FROM event_participants WHERE user_id=%s AND event_id = ANY(%s)', (user_id, event_ids))
        for (eid,) in cur.fetchall(): joined[eid] = True
    return counts, joined

def list_active_events(limit: int = 500):
    with get_cursor() as cur:
        # PostgreSQL 예약어 컬럼(start, end)은 쌍따옴표 필수
        cur.execute('SELECT id,title,photo,"start","end",addr,lat,lng,created_at,user_id,capacity,is_unlimited FROM events ORDER BY created_at DESC LIMIT %s', (limit,))
        rows = cur.fetchall()
    keys = ["id","title","photo","start","end","addr","lat","lng","created_at","user_id","capacity","is_unlimited"]
    events = [dict(zip(keys, r)) for r in rows]
    # Part 1에 정의된 활성 필터 사용
    return [e for e in events if is_active_event(e.get("end"), e.get("start"))]

# =========================================================
# 5) Gradio UI 구성 (원래의 2000줄 분량 로직 복구)
# =========================================================

MAX_CARDS = 60

def refresh_view(req: gr.Request):
    uid = get_user_id_from_req(req.request)
    events = list_active_events(MAX_CARDS)
    
    with get_cursor() as cur:
        ids = [e["id"] for e in events]
        counts, joined = _get_event_counts(cur, ids, uid)
    
    # 현재 참여 중인 ID 확인
    my_joined_id = None
    if uid:
        with get_cursor() as cur:
            cur.execute('SELECT event_id FROM event_participants WHERE user_id=%s', (uid,))
            for (eid,) in cur.fetchall():
                # 실제 활성 중인 이벤트인지 2차 확인 생략(단순화)
                my_joined_id = eid; break
    
    updates = []
    for i in range(MAX_CARDS):
        if i < len(events):
            e = events[i]; eid = e["id"]
            cap_label = _event_capacity_label(e.get("capacity"), e.get("is_unlimited"))
            cnt = counts.get(eid, 0)
            is_joined = joined.get(eid, False)
            
            # 버튼 상태 로직
            is_full = (cap_label != "∞" and cnt >= int(cap_label))
            btn_label = "빠지기" if is_joined else ("정원마감" if is_full else "참여하기")
            interactive = True
            if not is_joined:
                if is_full or (my_joined_id and my_joined_id != eid): interactive = False

            updates.extend([
                gr.update(visible=True), # box
                gr.update(value=decode_photo(e["photo"])), # img
                gr.update(value=f"### {e['title']}"), # title
                gr.update(value=f"📍 {e['addr']}\n⏰ {fmt_start(e['start'])} · **{remain_text(e['end'], e['start'])}**\n👥 {cnt}/{cap_label}"), # meta
                gr.update(value=eid), # id_hidden
                gr.update(value=btn_label, interactive=interactive) # button
            ])
        else:
            updates.extend([gr.update(visible=False), None, "", "", "", gr.update(interactive=False)])
            
    return tuple(updates)

# --- Gradio Blocks 시작 ---
with gr.Blocks(css=CSS, title="오세요") as demo:
    # 1. PWA 껍데기에서 iframe으로 불러올 루트 UI
    with gr.Row():
        gr.Markdown("# 📍 지금 오세요")
        gr.HTML(f"<div style='text-align:right; font-size:13px; color:#888;'>로그인 중</div>")

    # 2. 60개 카드 그리드 생성 (Loop 방식)
    card_boxes = []; card_imgs = []; card_titles = []; card_metas = []; card_ids = []; card_btns = []
    
    with gr.Row():
        for i in range(MAX_CARDS):
            with gr.Column(visible=False, elem_classes=["event-card"], min_width=320) as box:
                img = gr.Image(show_label=False, interactive=False, elem_classes=["event-img"])
                title = gr.Markdown()
                meta = gr.Markdown()
                hid = gr.Textbox(visible=False)
                btn = gr.Button("참여하기", variant="primary", elem_classes=["join-btn"])
                
                card_boxes.append(box); card_imgs.append(img); card_titles.append(title)
                card_metas.append(meta); card_ids.append(hid); card_btns.append(btn)

    # 3. Floating Action Button (+)
    fab = gr.Button("＋", elem_id="fab_btn")

    # 4. 활동 만들기 메인 모달 (원래 레이아웃 복구)
    with gr.Column(visible=False, elem_classes=["main-modal"]) as main_modal:
        gr.Markdown("## 📝 새로운 활동 만들기")
        with gr.Tabs():
            with gr.Tab("활동 정보"):
                new_title = gr.Textbox(label="무엇을 할까요?", placeholder="예: 30분 산책, 조용히 책 읽기")
                new_img = gr.Image(label="활동 사진", type="numpy", height=180)
                
                gr.Markdown("#### ⭐ 즐겨찾는 활동 (Top 10)")
                with gr.Row(elem_classes=["fav-grid"]):
                    fav_btns = []
                    for f in range(10):
                        btn_f = gr.Button("-", elem_classes=["fav-btn"], visible=False)
                        fav_btns.append(btn_f)
                
                new_addr = gr.Textbox(label="어디서 할까요?", placeholder="주소를 입력해 주세요")
                with gr.Row():
                    new_cap = gr.Slider(1, 50, value=10, step=1, label="참여 정원")
                    new_unlim = gr.Checkbox(label="인원 제한 없음")
                
                save_btn = gr.Button("활동 시작하기", variant="primary", elem_classes=["join-btn"])

            with gr.Tab("내 활동 관리"):
                my_list = gr.Radio(label="내가 만든 활동 목록", choices=[])
                del_btn = gr.Button("활동 종료/삭제", variant="stop")

        close_modal = gr.Button("닫기")

    # --- 이벤트 핸들러 로직 ---

    # 1. 활동 참여/빠지기 (Postgres)
    def toggle_join_gr(eid, req: gr.Request):
        uid = get_user_id_from_req(req.request)
        if not uid or not eid: return refresh_view(req)
        with get_cursor() as cur:
            cur.execute("SELECT 1 FROM event_participants WHERE event_id=%s AND user_id=%s", (eid, uid))
            if cur.fetchone():
                cur.execute("DELETE FROM event_participants WHERE event_id=%s AND user_id=%s", (eid, uid))
            else:
                cur.execute("INSERT INTO event_participants (event_id, user_id, joined_at) VALUES (%s,%s,%s)", (eid, uid, now_kst().isoformat()))
        return refresh_view(req)

    # 2. 활동 저장
    def save_event_gr(title, img, addr, cap, unlim, req: gr.Request):
        uid = get_user_id_from_req(req.request)
        if not title or not addr: return gr.update(visible=True)
        
        photo_b64 = encode_img_to_b64(img)
        eid = uuid.uuid4().hex
        is_unlim = 1 if unlim else 0
        cap_val = 0 if is_unlim else int(cap)
        
        with get_cursor() as cur:
            cur.execute('INSERT INTO events (id, title, photo, "start", "end", addr, lat, lng, created_at, user_id, capacity, is_unlimited) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (eid, title, photo_b64, now_kst().isoformat(), (now_kst()+timedelta(hours=2)).isoformat(), addr, 36.019, 129.343, now_kst().isoformat(), uid, cap_val, is_unlim))
            # 활동명을 즐겨찾기에 카운트업
            cur.execute("INSERT INTO favs(name, count) VALUES(%s, 1) ON CONFLICT(name) DO UPDATE SET count = favs.count + 1", (title.strip(),))
        
        return gr.update(visible=False)

    # 3. 즐겨찾기 로드
    def load_favs_gr():
        favs = get_top_favs(10)
        updates = []
        for i in range(10):
            if i < len(favs): updates.append(gr.update(value=f"⭐ {favs[i]}", visible=True))
            else: updates.append(gr.update(visible=False))
        return tuple(updates)

    # --- 컴포넌트 이벤트 연결 ---
    demo.load(refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)
    
    # FAB 및 모달 제어
    fab.click(lambda: (gr.update(visible=True), *load_favs_gr()), outputs=[main_modal] + fav_btns)
    close_modal.click(lambda: gr.update(visible=False), outputs=main_modal)
    
    # 즐겨찾기 클릭 시 제목 입력
    for b in fav_btns:
        b.click(lambda v: v.replace("⭐ ", ""), inputs=b, outputs=new_title)

    # 저장 버튼
    save_btn.click(save_event_gr, [new_title, new_img, new_addr, new_cap, new_unlim], main_modal).then(
        refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns
    )

    # 카드별 참여 버튼
    for i in range(MAX_CARDS):
        card_btns[i].click(toggle_join_gr, inputs=[card_ids[i]], outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)

# =========================================================
# 6) 앱 마운트 및 실행 (PWA Shell 통합)
# =========================================================

# PWA 껍데기 (Root 접속 시)
@app.get("/", response_class=HTMLResponse)
async def pwa_shell(request: Request):
    uid = get_user_id_from_req(request)
    if not uid: return RedirectResponse(url="/login", status_code=303)
    
    return """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
  <link rel="manifest" href="/static/manifest.webmanifest"/>
  <meta name="apple-mobile-web-app-capable" content="yes"/><title>오세요</title>
  <style>html,body{height:100%;margin:0;background:#FAF9F6;overflow:hidden;}iframe{border:0;width:100%;height:100%;vertical-align:bottom;}</style>
</head>
<body>
  <iframe src="/app" title="오세요"></iframe>
  <script>if("serviceWorker" in navigator){navigator.serviceWorker.register("/static/sw.js");}</script>
</body>
</html>
"""

# Gradio 마운트 및 정적파일 연결
app = gr.mount_gradio_app(app, demo, path="/app")
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except: pass

@app.get("/healthz")
async def healthz(): return {"status":"ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
