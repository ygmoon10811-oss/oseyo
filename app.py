# -*- coding: utf-8 -*-
import os, io, re, uuid, json, base64, hashlib, html, random
from datetime import datetime, timedelta, timezone
import requests
from PIL import Image
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
import gradio as gr

# =========================================================
# 0) DB 및 기본 설정
# =========================================================
KST = timezone(timedelta(hours=9))
def now_kst(): return datetime.now(KST)

KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()
COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 24 * 7

DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
db_pool = None
try:
    if DATABASE_URL:
        db_pool = pool.ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)
except Exception as e:
    print(f"DB Error: {e}")

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

# =========================================================
# 1) 유틸리티 (암호, 이미지, 날짜)
# =========================================================
def pw_hash(password, salt):
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 150_000)
    return f"{salt}${dk.hex()}"

def pw_verify(password, stored):
    try:
        salt, _ = stored.split("$", 1)
        return pw_hash(password, salt) == stored
    except: return False

def encode_img_to_b64(img_np):
    if img_np is None: return ""
    im = Image.fromarray(img_np.astype("uint8")).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def decode_photo(photo_b64):
    if not photo_b64: return None
    try: return Image.open(io.BytesIO(base64.b64decode(photo_b64))).convert("RGB")
    except: return None

def fmt_start(start_s):
    try: return datetime.fromisoformat(str(start_s).replace("Z", "+00:00")).strftime("%m월 %d일 %H:%M")
    except: return str(start_s or "")

def remain_text(end_s, start_s=None):
    now = now_kst()
    try:
        edt = datetime.fromisoformat(str(end_s or start_s).replace("Z", "+00:00"))
        if not end_s: edt = edt.replace(hour=23, minute=59)
        if edt < now: return "종료됨"
        m = int((edt - now).total_seconds() // 60)
        return f"남음 {m//1440}일 { (m//60)%24 }시간" if m > 60 else f"남음 {m}분"
    except: return ""

# =========================================================
# 2) FastAPI 라우팅 (로그인, 회원가입, PWA 파일 매핑)
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

# --- 💡 PWA 필수 파일들을 루트 경로에 강제로 연결 (404 해결 핵심) ---
@app.get("/manifest.json")
async def get_manifest():
    # 파일명이 manifest.webmanifest 인지 manifest.json 인지 확인 후 수정
    p = "static/manifest.webmanifest"
    if not os.path.exists(p): p = "static/manifest.json"
    return FileResponse(p)

@app.get("/sw.js")
async def get_sw():
    return FileResponse("static/sw.js")

@app.get("/icons/{path:path}")
async def get_icons(path: str):
    return FileResponse(f"static/icons/{path}")

@app.get("/healthz")
async def healthz(): return {"status": "ok"}

# --- HTML 템플릿 ---
LOGIN_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/><title>로그인</title><style>body{font-family:Pretendard,sans-serif;background:#FAF9F6;display:flex;justify-content:center;padding-top:50px;margin:0;}.card{background:#fff;border:1px solid #E5E3DD;border-radius:20px;padding:30px;width:90%;max-width:360px;box-shadow:0 10px 25px rgba(0,0,0,0.05);}h1{font-size:24px;margin-bottom:20px;font-weight:800;text-align:center;}input{width:100%;padding:14px;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:15px;box-sizing:border-box;font-size:15px;outline:none;}.btn{width:100%;padding:15px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;font-weight:bold;font-size:16px;width:100%;}.err{color:#ef4444;font-size:13px;margin-bottom:10px;text-align:center;}</style></head><body><div class="card"><h1>로그인</h1><form method="post" action="/login"><input name="email" type="email" placeholder="이메일" required/><input name="password" type="password" placeholder="비밀번호" required/><div class="err">__ERROR_BLOCK__</div><button class="btn">로그인</button></form><div style="text-align:center;margin-top:20px;font-size:14px;color:#888;">계정이 없으신가요? <a href="/signup" style="color:#111;font-weight:bold;text-decoration:none;">회원가입</a></div></div></body></html>"""

@app.get("/login")
async def login_get(err: str = ""): return HTMLResponse(LOGIN_HTML.replace("__ERROR_BLOCK__", err))

@app.post("/login")
async def login_post(email: str = Form(...), password: str = Form(...)):
    try:
        with get_cursor() as cur:
            cur.execute("SELECT id, pw_hash FROM users WHERE email=%s", (email.strip().lower(),))
            row = cur.fetchone()
            if row and pw_verify(password, row[1]):
                token = uuid.uuid4().hex
                cur.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES(%s,%s,%s)", (token, row[0], (now_kst()+timedelta(hours=SESSION_HOURS)).isoformat()))
                resp = RedirectResponse(url="/", status_code=303)
                resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS*3600, httponly=True, samesite="lax", path="/")
                return resp
    except: pass
    return RedirectResponse(url="/login?err=LoginFail", status_code=303)

@app.get("/signup")
async def signup_get(): return HTMLResponse("""<!doctype html><html lang="ko"><body><div style='padding:50px; text-align:center;'>회원가입 화면 (생략됨 - 이전 코드 활용)</div></body></html>""")

@app.post("/signup")
async def signup_post(email: str = Form(...), otp: str = Form(...), password: str = Form(...), name: str = Form(...)):
    # (회원가입 처리 로직...)
    return RedirectResponse(url="/login?err=Success", status_code=303)

@app.get("/")
async def pwa_shell(request: Request):
    if not get_user_id_from_req(request): return RedirectResponse(url="/login", status_code=303)
    return HTMLResponse('<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/><link rel="manifest" href="/manifest.json"/><title>오세요</title><style>html,body{height:100%;margin:0;background:#FAF9F6;overflow:hidden;}iframe{border:0;width:100%;height:100%;vertical-align:bottom;}</style></head><body><iframe src="/app/" title="main"></iframe><script>if("serviceWorker" in navigator){navigator.serviceWorker.register("/sw.js");}</script></body></html>')

# =========================================================
# 3) Gradio 인터페이스
# =========================================================
MAX_CARDS = 60
def refresh_view(req: gr.Request):
    # (60개 카드 로드 로직...)
    return tuple([gr.update(visible=False)] * (MAX_CARDS * 6))

with gr.Blocks(title="오세요") as demo:
    gr.Markdown("# 📍 지금 오세요")
    # (카드 레이아웃 등...)

# =========================================================
# 4) 최종 마운트 및 실행
# =========================================================
app = gr.mount_gradio_app(app, demo, path="/app")

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
