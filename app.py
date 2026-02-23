# -*- coding: utf-8 -*-
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

# PostgreSQL Library
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager

import gradio as gr

# --- 0) 시간 및 DB 설정 ---
KST = timezone(timedelta(hours=9))
def now_kst(): return datetime.now(KST)

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

try:
    # Koyeb/Supabase용 연결 풀
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

# --- 1) FastAPI 설정 ---
app = FastAPI(redirect_slashes=False)
COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 24 * 7

# --- 2) 유틸리티 및 보안 ---
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

# --- 3) 날짜 관련 ---
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

# --- 4) 인증 미들웨어 ---
@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if path == "/" or path.startswith(("/login", "/signup", "/send_email_otp", "/static", "/healthz", "/favicon")):
        return await call_next(request)
    
    token = request.cookies.get(COOKIE_NAME)
    uid = None
    if token:
        with get_cursor() as cur:
            cur.execute("SELECT user_id, expires_at FROM sessions WHERE token=%s", (token,))
            row = cur.fetchone()
            if row and datetime.fromisoformat(row[1]) > now_kst():
                uid = row[0]
    
    if not uid:
        if path.startswith("/api/"): return JSONResponse({"ok": False}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    return await call_next(request)

# --- 5) 로그인 / 회원가입 로직 ---
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

SIGNUP_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><title>오세요 - 회원가입</title><style>body{font-family:Pretendard,sans-serif;background:#faf9f6;margin:0;display:flex;justify-content:center;padding:30px 10px;}.card{background:#fff;border:1px solid #e5e3dd;border-radius:18px;padding:24px;width:100%;max-width:420px;box-shadow:0 10px 25px rgba(0,0,0,.05);}h1{font-size:22px;margin:0 0 10px;}label{display:block;font-size:13px;margin:12px 0 6px;color:#4b5563;}input{width:100%;padding:12px;border:1px solid #e5e7eb;border-radius:12px;box-sizing:border-box;}.btn{width:100%;padding:13px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;margin-top:20px;font-weight:600;}.btn-verify{background:#f3f4f6;color:#111;margin-top:8px;font-size:13px;border:0;padding:8px;border-radius:8px;cursor:pointer;}.err{color:#ef4444;font-size:13px;margin-top:10px;}.ok{color:#10b981;font-size:13px;margin-top:10px;}.row{display:flex;gap:8px;align-items:center;}.link{text-align:center;margin-top:16px;font-size:13px;}</style></head><body><div class="card"><h1>회원가입</h1><form method="post" action="/signup"><label>이메일</label><div class="row"><input id="email" name="email" type="email" required/><button type="button" class="btn-verify" onclick="sendOtp()">인증발송</button></div><div id="otp_msg"></div><label>인증번호</label><input name="otp" required/><label>비밀번호</label><input name="password" type="password" required/><label>이름</label><input name="name" required/><button class="btn">가입 완료</button></form>__ERROR_BLOCK__<div class="link"><a href="/login">로그인으로 돌아가기</a></div></div><script>async function sendOtp(){const e=document.getElementById('email').value;if(!e){alert('이메일입력!');return;}document.getElementById('otp_msg').innerText='발송중...';const r=await fetch('/send_email_otp',{method:'POST',body:JSON.stringify({email:e})});const d=await r.json();document.getElementById('otp_msg').innerText=d.ok?'발송됨':'실패';}</script></body></html>"""

@app.get("/signup")
async def signup_get(err: str = ""):
    return HTMLResponse(render_safe(SIGNUP_HTML, ERROR_BLOCK=f'<div class="err">{err}</div>' if err else ""))

@app.post("/signup")
async def signup_post(email: str = Form(...), otp: str = Form(...), password: str = Form(...), name: str = Form(...)):
    email = email.strip().lower()
    with get_cursor() as cur:
        cur.execute("SELECT otp, expires_at FROM email_otps WHERE email=%s", (email,))
        row = cur.fetchone()
        if not row or row[0] != otp: return RedirectResponse(url="/signup?err=인증오류", status_code=303)
        cur.execute("SELECT 1 FROM users WHERE email=%s", (email,))
        if cur.fetchone(): return RedirectResponse(url="/signup?err=이미가입", status_code=303)
        cur.execute("INSERT INTO users(id,email,pw_hash,name,created_at) VALUES(%s,%s,%s,%s,%s)", (uuid.uuid4().hex, email, pw_hash(password, "salt"), name.strip(), now_kst().isoformat()))
    return RedirectResponse(url="/login?err=가입성공", status_code=303)

@app.post("/send_email_otp")
async def send_email_otp(request: Request):
    try:
        data = await request.json()
        email = data.get("email", "").strip().lower()
        otp = "123456" # 테스트용 고정 (나중에 SMTP 연동 필요)
        with get_cursor() as cur:
            cur.execute("INSERT INTO email_otps(email, otp, expires_at) VALUES(%s,%s,%s) ON CONFLICT(email) DO UPDATE SET otp=EXCLUDED.otp", (email, otp, (now_kst()+timedelta(minutes=10)).isoformat()))
        return JSONResponse({"ok": True})
    except: return JSONResponse({"ok": False})

# --- 6) 지도 API ---
@app.get("/api/events_json")
async def api_events_json():
    with get_cursor() as cur:
        cur.execute('SELECT id,title,addr,lat,lng FROM events LIMIT 100')
        rows = cur.fetchall()
    return JSONResponse({"ok": True, "events": [{"id":r[0],"title":r[1],"addr":r[2],"lat":r[3],"lng":r[4]} for r in rows]})

@app.get("/map")
async def map_page():
    MAP_HTML = """<!doctype html><html><head><meta charset="utf-8"/><style>html,body,#map{width:100%;height:100%;margin:0;}</style></head><body><div id="map"></div><script src="//dapi.kakao.com/v2/maps/sdk.js?appkey=__KEY__"></script><script>const map=new kakao.maps.Map(document.getElementById('map'),{center:new kakao.maps.LatLng(36.019,129.343),level:5});</script></body></html>"""
    return HTMLResponse(render_safe(MAP_HTML, KEY=os.getenv("KAKAO_JAVASCRIPT_KEY","")))

# --- 7) Gradio UI ---
def get_my_id(req: gr.Request):
    token = req.request.cookies.get(COOKIE_NAME)
    with get_cursor() as cur:
        cur.execute("SELECT user_id FROM sessions WHERE token=%s", (token,))
        return cur.fetchone()[0]

def gr_save(title, addr, req: gr.Request):
    uid = get_my_id(req)
    with get_cursor() as cur:
        cur.execute('INSERT INTO events(id,title,addr,lat,lng,created_at,user_id) VALUES(%s,%s,%s,%s,%s,%s,%s)', (uuid.uuid4().hex, title, addr, 36.019, 129.343, now_kst().isoformat(), uid))
    return "등록 완료!"

with gr.Blocks() as demo:
    gr.Markdown("# 오세요")
    t = gr.Textbox(label="활동")
    a = gr.Textbox(label="주소")
    btn = gr.Button("만들기")
    msg = gr.Markdown()
    btn.click(gr_save, [t, a], msg)
    gr.HTML("<iframe src='/map' style='width:100%;height:400px;border:0;'></iframe>")

# server.py에서 mount_gradio_app을 호출하므로 여기서는 정의만 합니다.
