# -*- coding: utf-8 -*-
print("### DEPLOY MARKER: V22_POSTGRES_FINAL_FIX ###", flush=True)
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

# --- PostgreSQL Library ---
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

# --- BEGIN: Render/Koyeb-safe gradio_client bool-schema hotfix ---
try:
    from gradio_client import utils as _gc_utils
    if not getattr(_gc_utils, "_OSEYO_PATCHED_BOOL_SCHEMA", False):
        _APIInfoParseError = getattr(_gc_utils, "APIInfoParseError", Exception)
        def _schema_from(args, kwargs):
            if "schema" in kwargs: return kwargs.get("schema")
            return args[0] if args else None
        def _wrap(orig):
            def _wrapped(*args, **kwargs):
                schema = _schema_from(args, kwargs)
                if isinstance(schema, bool): return "Any"
                try: return orig(*args, **kwargs)
                except _APIInfoParseError: return "Any"
                except TypeError:
                    try: return orig(schema)
                    except Exception: return "Any"
                except Exception: raise
            return _wrapped
        if hasattr(_gc_utils, "json_schema_to_python_type"):
            _gc_utils.json_schema_to_python_type = _wrap(_gc_utils.json_schema_to_python_type)
        if hasattr(_gc_utils, "_json_schema_to_python_type"):
            _gc_utils._json_schema_to_python_type = _wrap(_gc_utils._json_schema_to_python_type)
        if hasattr(_gc_utils, "get_type"):
            _gc_utils.get_type = _wrap(_gc_utils.get_type)
        _gc_utils._OSEYO_PATCHED_BOOL_SCHEMA = True
except Exception: pass
# --- END ---

import gradio as gr

# =========================================================
# 0) 환경 설정 및 DB 연결 (PostgreSQL)
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

# DB 연결 풀 설정
try:
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)
    print("[DB] PostgreSQL Pool Connected.")
except Exception as e:
    print(f"[DB] Connection Error: {e}")
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

# 테이블 초기화 (PostgreSQL 문법)
def init_db():
    with get_cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, pw_hash TEXT, name TEXT, gender TEXT, birth TEXT, created_at TEXT);")
        cur.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT, expires_at TEXT);")
        cur.execute("CREATE TABLE IF NOT EXISTS email_otps (email TEXT PRIMARY KEY, otp TEXT, expires_at TEXT);")
        cur.execute('CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, title TEXT, photo TEXT, "start" TEXT, "end" TEXT, addr TEXT, lat DOUBLE PRECISION, lng DOUBLE PRECISION, created_at TEXT, user_id TEXT, capacity INTEGER DEFAULT 10, is_unlimited INTEGER DEFAULT 0);')
        cur.execute("CREATE TABLE IF NOT EXISTS favs (name TEXT PRIMARY KEY, count INTEGER DEFAULT 1);")
        cur.execute("CREATE TABLE IF NOT EXISTS event_participants (event_id TEXT, user_id TEXT, joined_at TEXT, PRIMARY KEY(event_id, user_id));")

if db_pool:
    init_db()

# =========================================================
# 1) FastAPI 설정 (슬래시 리다이렉트 방지 추가)
# =========================================================
app = FastAPI(redirect_slashes=False)

# 인증 미들웨어
PUBLIC_PATHS = ["/login", "/signup", "/send_email_otp", "/static", "/healthz"]

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if path == "/" or any(path.startswith(p) for p in PUBLIC_PATHS) or path.startswith(("/assets", "/favicon")):
        return await call_next(request)
    
    # 세션 확인
    uid = None
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with get_cursor() as cur:
            cur.execute("SELECT user_id, expires_at FROM sessions WHERE token=%s", (token,))
            row = cur.fetchone()
            if row and datetime.fromisoformat(row[1]) > now_kst():
                uid = row[0]
    
    if not uid:
        if path.startswith("/api/"): return JSONResponse({"ok": False, "message": "Login Required"}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)

# =========================================================
# 2) 유틸리티 함수 (PW, 날짜 등)
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

def parse_dt(s, assume_end: bool = False):
    if not s: return None
    s = str(s).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None: dt = dt.replace(tzinfo=KST)
        else: dt = dt.astimezone(KST)
        return dt
    except:
        for fmt in _DT_FORMATS:
            try: return datetime.strptime(s, fmt).replace(tzinfo=KST)
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
    return dt.strftime("%m월 %d일 %H:%M") if dt else ""

# =========================================================
# 3) 로그인 / 회원가입 화면 및 로직
# =========================================================

LOGIN_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><title>오세요 - 로그인</title><style>body{font-family:Pretendard,sans-serif;background:#faf9f6;margin:0;display:flex;justify-content:center;padding-top:50px;}.card{background:#fff;border:1px solid #e5e3dd;border-radius:18px;padding:24px;width:100%;max-width:380px;box-shadow:0 10px 25px rgba(0,0,0,.05);}h1{font-size:22px;margin:0 0 20px;}label{display:block;font-size:13px;margin-bottom:6px;color:#4b5563;}input{width:100%;padding:12px;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:16px;box-sizing:border-box;}.btn{width:100%;padding:13px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;font-weight:600;}.err{color:#ef4444;font-size:13px;margin-bottom:10px;}.link{text-align:center;margin-top:16px;font-size:13px;color:#6b7280;}a{color:#111;text-decoration:none;font-weight:600;}</style></head><body><div class="card"><h1>로그인</h1><form method="post" action="/login"><label>이메일</label><input name="email" type="email" required/><label>비밀번호</label><input name="password" type="password" required/><button class="btn">로그인</button></form>__ERROR_BLOCK__<div class="link">계정이 없으신가요? <a href="/signup">회원가입</a></div></div></body></html>"""

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

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with get_cursor() as cur: cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

SIGNUP_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><title>오세요 - 회원가입</title><style>body{font-family:Pretendard,sans-serif;background:#faf9f6;margin:0;display:flex;justify-content:center;padding:30px 10px;}.card{background:#fff;border:1px solid #e5e3dd;border-radius:18px;padding:24px;width:100%;max-width:420px;box-shadow:0 10px 25px rgba(0,0,0,.05);}h1{font-size:22px;margin:0 0 10px;}label{display:block;font-size:13px;margin:12px 0 6px;color:#4b5563;}input, select{width:100%;padding:12px;border:1px solid #e5e7eb;border-radius:12px;box-sizing:border-box;}.btn{width:100%;padding:13px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;margin-top:20px;font-weight:600;}.btn-verify{background:#f3f4f6;color:#111;margin-top:8px;font-size:13px;}.err{color:#ef4444;font-size:13px;margin-top:10px;}.ok{color:#10b981;font-size:13px;margin-top:10px;}.row{display:flex;gap:8px;align-items:center;}.link{text-align:center;margin-top:16px;font-size:13px;}</style></head><body><div class="card"><h1>회원가입</h1><form id="signupForm" method="post" action="/signup"><label>이메일</label><div class="row"><input id="email" name="email" type="email" placeholder="you@example.com" required/><button type="button" class="btn btn-verify" onclick="sendOtp()">인증번호 발송</button></div><div id="otp_msg"></div><label>인증번호</label><input name="otp" placeholder="6자리 입력" required/><label>비밀번호</label><input name="password" type="password" required/><label>이름</label><input name="name" required/><button class="btn">가입 완료</button></form>__ERROR_BLOCK__<div class="link">이미 계정이 있나요? <a href="/login">로그인</a></div></div><script>async function sendOtp(){const email=document.getElementById('email').value;const msg=document.getElementById('otp_msg');if(!email){alert('이메일을 입력하세요');return;}msg.innerText='발송 중...';const res=await fetch('/send_email_otp',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:email})});const d=await res.json();msg.innerText=d.ok?'인증번호가 발송되었습니다.':(d.message||'발송 실패');msg.className=d.ok?'ok':'err';}</script></body></html>"""

@app.get("/signup")
async def signup_get(err: str = ""):
    eb = f'<div class="err">{html.escape(err)}</div>' if err else ""
    return HTMLResponse(render_safe(SIGNUP_HTML, ERROR_BLOCK=eb))

@app.post("/signup")
async def signup_post(email: str = Form(...), otp: str = Form(...), password: str = Form(...), name: str = Form(...)):
    email = email.strip().lower()
    with get_cursor() as cur:
        cur.execute("SELECT otp, expires_at FROM email_otps WHERE email=%s", (email,))
        row = cur.fetchone()
        if not row or row[0] != otp or datetime.fromisoformat(row[1]) < now_kst():
            return RedirectResponse(url="/signup?err=" + requests.utils.quote("인증번호가 잘못되었거나 만료되었습니다."), status_code=303)
        
        cur.execute("SELECT 1 FROM users WHERE email=%s", (email,))
        if cur.fetchone():
            return RedirectResponse(url="/signup?err=" + requests.utils.quote("이미 가입된 이메일입니다."), status_code=303)
        
        uid = uuid.uuid4().hex
        cur.execute("INSERT INTO users(id,email,pw_hash,name,created_at) VALUES(%s,%s,%s,%s,%s)", (uid, email, pw_hash(password, uuid.uuid4().hex[:12]), name.strip(), now_kst().isoformat()))
        cur.execute("DELETE FROM email_otps WHERE email=%s", (email,))
    return RedirectResponse(url="/login?err=" + requests.utils.quote("회원가입 성공! 로그인해 주세요."), status_code=303)

@app.post("/send_email_otp")
async def send_email_otp(request: Request):
    try:
        data = await request.json()
        email = data.get("email", "").strip().lower()
        otp = "".join([str(re.import_module('random').randint(0,9)) for _ in range(6)])
        with get_cursor() as cur:
            cur.execute("INSERT INTO email_otps(email, otp, expires_at) VALUES(%s,%s,%s) ON CONFLICT(email) DO UPDATE SET otp=EXCLUDED.otp, expires_at=EXCLUDED.expires_at", (email, otp, (now_kst()+timedelta(minutes=10)).isoformat()))
        
        # SMTP 발송
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(f"인증번호: {otp}", "plain", "utf-8")
        msg["Subject"] = "[오세요] 인증번호"
        msg["From"] = os.getenv("FROM_EMAIL")
        msg["To"] = email
        with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", 587))) as s:
            s.starttls(); s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            s.send_message(msg)
        return JSONResponse({"ok": True})
    except Exception as e: return JSONResponse({"ok": False, "message": str(e)})

# =========================================================
# 4) 지도 및 API
# =========================================================

@app.get("/api/events_json")
async def api_events_json(request: Request):
    uid = None # 미들웨어에서 확인됨
    token = request.cookies.get(COOKIE_NAME)
    with get_cursor() as cur:
        cur.execute("SELECT user_id FROM sessions WHERE token=%s", (token,))
        uid = cur.fetchone()[0]
        
        cur.execute('SELECT id,title,photo,"start","end",addr,lat,lng,capacity,is_unlimited FROM events ORDER BY created_at DESC LIMIT 500')
        rows = cur.fetchall()
        
    out = []
    for r in rows:
        if is_active_event(r[4], r[3]):
            out.append({
                "id": r[0], "title": r[1], "photo": r[2], "addr": r[5], "lat": r[6], "lng": r[7],
                "start_fmt": fmt_start(r[3]), "remain": remain_text(r[4], r[3])
            })
    return JSONResponse({"ok": True, "events": out})

@app.get("/map")
async def map_page():
    MAP_HTML = """<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><style>html,body,#map{width:100%;height:100%;margin:0;}</style></head><body><div id="map"></div><script src="//dapi.kakao.com/v2/maps/sdk.js?appkey=__KEY__"></script><script>const map=new kakao.maps.Map(document.getElementById('map'),{center:new kakao.maps.LatLng(36.019,129.343),level:5});fetch('/api/events_json').then(r=>r.json()).then(d=>{d.events.forEach(e=>{if(e.lat&&e.lng){new kakao.maps.Marker({map:map,position:new kakao.maps.LatLng(e.lat,e.lng)});}});});</script></body></html>"""
    return HTMLResponse(render_safe(MAP_HTML, KEY=KAKAO_JAVASCRIPT_KEY))

# =========================================================
# 5) Gradio UI (/app)
# =========================================================

def get_my_id(req: gr.Request):
    token = req.request.cookies.get(COOKIE_NAME)
    with get_cursor() as cur:
        cur.execute("SELECT user_id FROM sessions WHERE token=%s", (token,))
        return cur.fetchone()[0]

def save_event_gr(title, addr, start_d, start_h, start_m, end_h, end_m, req: gr.Request):
    uid = get_my_id(req)
    eid = uuid.uuid4().hex
    start_s = f"{start_d} {start_h}:{start_m}"
    end_s = f"{start_d} {end_h}:{end_m}"
    
    # 좌표 가져오기
    lat, lng = 36.019, 129.343
    try:
        r = requests.get("https://dapi.kakao.com/v2/local/search/address.json", headers={"Authorization":f"KakaoAK {KAKAO_REST_API_KEY}"}, params={"query":addr}, timeout=5).json()
        if r['documents']: lat, lng = float(r['documents'][0]['y']), float(r['documents'][0]['x'])
    except: pass

    with get_cursor() as cur:
        cur.execute('INSERT INTO events(id,title,"start","end",addr,lat,lng,created_at,user_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (eid, title, start_s, end_s, addr, lat, lng, now_kst().isoformat(), uid))
    return "등록되었습니다!"

with gr.Blocks(title="오세요") as demo:
    gr.Markdown("# 📍 지금 오세요")
    with gr.Row():
        with gr.Column():
            t = gr.Textbox(label="활동 이름", placeholder="예: 도서관에서 책읽기")
            a = gr.Textbox(label="장소", placeholder="도로명 주소 입력")
            sd = gr.Textbox(label="날짜", value=now_kst().strftime("%Y-%m-%d"))
            with gr.Row():
                sh = gr.Dropdown([f"{i:02d}" for i in range(24)], label="시작 시", value="14")
                sm = gr.Dropdown([f"{i:02d}" for i in range(0,60,10)], label="분", value="00")
            with gr.Row():
                eh = gr.Dropdown([f"{i:02d}" for i in range(24)], label="종료 시", value="16")
                em = gr.Dropdown([f"{i:02d}" for i in range(0,60,10)], label="분", value="00")
            btn = gr.Button("활동 만들기", variant="primary")
            msg = gr.Markdown()
        with gr.Column():
            gr.HTML("<iframe src='/map' style='width:100%; height:500px; border:0; border-radius:15px; box-shadow:0 4px 12px rgba(0,0,0,0.1);'></iframe>")
    
    btn.click(save_event_gr, [t,a,sd,sh,sm,eh,em], msg)

app = gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
