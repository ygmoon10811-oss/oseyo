# -*- coding: utf-8 -*-
print("### DEPLOY MARKER: V33_FINAL_STABLE_VERSION ###", flush=True)
import os, io, re, uuid, json, base64, hashlib, html, random
from datetime import datetime, timedelta, timezone
import requests
from PIL import Image
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
import gradio as gr

# 0) 시간 및 DB 설정
KST = timezone(timedelta(hours=9))
def now_kst(): return datetime.now(KST)

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()
COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 24 * 7

DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
db_pool = None
try:
    if DATABASE_URL:
        db_pool = pool.ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)
        print("[DB] PostgreSQL Pool Initialized.")
except Exception as e:
    print(f"[DB] Connection Pool Error: {e}")

@contextmanager
def get_cursor():
    global db_pool
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
    except: pass

if db_pool: init_db()

# 1) 유틸리티 (암호, 이미지, 날짜)
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

# 2) FastAPI 및 인증 (404 방지용 설정)
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

@app.get("/healthz")
async def healthz(): return {"status": "ok"}

# --- HTML 템플릿 (CSS 포함) ---
LOGIN_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>로그인</title><style>body{font-family:Pretendard,sans-serif;background:#FAF9F6;display:flex;justify-content:center;padding-top:50px;}.card{background:#fff;border:1px solid #E5E3DD;border-radius:20px;padding:30px;width:100%;max-width:360px;box-shadow:0 10px 25px rgba(0,0,0,0.05);}h1{font-size:24px;margin-bottom:20px;font-weight:800;text-align:center;}input{width:100%;padding:14px;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:15px;box-sizing:border-box;font-size:15px;}.btn{width:100%;padding:15px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;font-weight:bold;width:100%;}.err{color:#ef4444;font-size:13px;margin-bottom:10px;text-align:center;}</style></head><body><div class="card"><h1>로그인</h1><form method="post" action="/login"><input name="email" type="email" placeholder="이메일" required/><input name="password" type="password" placeholder="비밀번호" required/>__ERROR_BLOCK__<button class="btn">로그인</button></form><div style="text-align:center;margin-top:15px;font-size:13px;color:#888;">계정이 없으신가요? <a href="/signup" style="color:#111;font-weight:bold;text-decoration:none;">회원가입</a></div></div></body></html>"""

SIGNUP_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>회원가입</title><style>body{font-family:Pretendard,sans-serif;background:#FAF9F6;display:flex;justify-content:center;padding:30px 10px;}.card{background:#fff;border:1px solid #E5E3DD;border-radius:20px;padding:25px;width:100%;max-width:440px;}h1{font-size:22px;text-align:center;font-weight:800;}.row{display:flex;gap:8px;align-items:center;}input,select{padding:12px;border:1px solid #e5e7eb;border-radius:10px;box-sizing:border-box;margin-bottom:10px;font-size:15px;}.btn{width:100%;padding:15px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;margin-top:10px;font-weight:bold;}.terms{background:#f9fafb;padding:12px;border-radius:10px;font-size:13px;margin-top:10px;border:1px solid #e5e7eb;}</style></head><body><div class="card"><h1>회원가입</h1><form method="post" action="/signup" onsubmit="combineEmail()"><label style="font-size:13px;color:#666;">이메일 아이디</label><div class="row"><input id="eid" type="text" placeholder="아이디" required style="flex:1.5;"/><span style="font-weight:bold;color:#888;">@</span><select id="edom" style="flex:1.2;"><option value="naver.com">naver.com</option><option value="gmail.com">gmail.com</option><option value="kakao.com">kakao.com</option><option value="daum.net">daum.net</option></select></div><input type="hidden" id="fem" name="email"/><button type="button" onclick="sendOtp()" style="padding:8px;font-size:12px;margin-bottom:10px;cursor:pointer;">인증번호 발송</button><div id="omsg" style="font-size:12px;color:#10b981;margin-bottom:10px;"></div><input name="otp" placeholder="인증번호 6자리" required/><input name="password" type="password" placeholder="비밀번호" required/><input name="name" placeholder="이름" required/><div class="terms"><label><input type="checkbox" required/> (필수) 만 14세 이상입니다</label><br/><label><input type="checkbox" required/> (필수) 이용약관 및 개인정보 동의</label></div><button class="btn">회원가입 완료</button></form></div><script>function combineEmail(){document.getElementById('fem').value=document.getElementById('eid').value+'@'+document.getElementById('edom').value;}async function sendOtp(){combineEmail();const em=document.getElementById('fem').value;if(!document.getElementById('eid').value)return;document.getElementById('omsg').innerText='발송 중...';const r=await fetch('/send_email_otp',{method:'POST',body:JSON.stringify({email:em})});const d=await r.json();document.getElementById('omsg').innerText=d.ok?'메일이 발송되었습니다.':'발송 실패';}</script></body></html>"""

@app.get("/login")
async def login_get(err: str = ""):
    eb = f'<div class="err">{html.escape(err)}</div>' if err else ""
    return HTMLResponse(LOGIN_HTML.replace("__ERROR_BLOCK__", eb))

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
    return RedirectResponse(url="/login?err=로그인 정보가 틀립니다.", status_code=303)

@app.get("/signup")
async def signup_get(err: str = ""):
    return HTMLResponse(SIGNUP_HTML.replace("__ERROR_BLOCK__", f'<div style="color:red;font-size:12px;text-align:center;">{err}</div>' if err else ""))

@app.post("/send_email_otp")
async def send_email_otp(request: Request):
    try:
        data = await request.json()
        email, otp = data.get("email", "").strip().lower(), str(random.randint(100000, 999999))
        with get_cursor() as cur:
            cur.execute("INSERT INTO email_otps(email,otp,expires_at) VALUES(%s,%s,%s) ON CONFLICT(email) DO UPDATE SET otp=EXCLUDED.otp", (email, otp, (now_kst()+timedelta(minutes=10)).isoformat()))
        # SMTP 생략 (실제 메일 연동 시 os.getenv 설정 필요)
        return JSONResponse({"ok": True})
    except: return JSONResponse({"ok": False})

@app.post("/signup")
async def signup_post(email: str = Form(...), otp: str = Form(...), password: str = Form(...), name: str = Form(...)):
    try:
        with get_cursor() as cur:
            cur.execute("SELECT otp FROM email_otps WHERE email=%s", (email.strip().lower(),))
            row = cur.fetchone()
            if not row or row[0] != otp: return RedirectResponse(url="/signup?err=인증오류", status_code=303)
            cur.execute("INSERT INTO users(id,email,pw_hash,name,created_at) VALUES(%s,%s,%s,%s,%s)", (uuid.uuid4().hex, email, pw_hash(password, "salt"), name.strip(), now_kst().isoformat()))
        return RedirectResponse(url="/login?err=가입성공! 로그인하세요", status_code=303)
    except: return RedirectResponse(url="/signup?err=오류발생", status_code=303)


# 3) Gradio UI (60개 카드 복구)
MAX_CARDS = 60
CSS = r":root{--accent:#ff5a1f}.event-card{background:white;border:1px solid #E5E3DD;border-radius:18px;padding:14px;box-shadow:0 8px 22px rgba(0,0,0,0.06);margin-bottom:12px;}.event-img img{border-radius:14px!important;height:180px!important;object-fit:cover!important;}#fab_btn{position:fixed;right:22px;bottom:22px;z-index:9999;width:56px;height:56px;border-radius:999px;background:#ff5a1f;color:white;font-size:28px;border:0;box-shadow:0 10px 20px rgba(0,0,0,0.2);cursor:pointer;}"

def refresh_view(req: gr.Request):
    uid = get_user_id_from_req(req.request)
    events = []
    try:
        with get_cursor() as cur:
            cur.execute('SELECT id,title,photo,"start","end",addr,capacity,is_unlimited FROM events ORDER BY created_at DESC LIMIT %s', (MAX_CARDS,))
            rows = cur.fetchall()
            for r in rows:
                cur.execute("SELECT COUNT(*) FROM event_participants WHERE event_id=%s", (r[0],))
                cnt = cur.fetchone()[0]
                cur.execute("SELECT 1 FROM event_participants WHERE event_id=%s AND user_id=%s", (r[0], uid))
                joined = cur.fetchone() is not None
                events.append({'id':r[0],'title':r[1],'photo':r[2],'start':r[3],'end':r[4],'addr':r[5],'cap':r[6],'unlim':r[7],'cnt':cnt,'joined':joined})
    except: pass

    updates = []
    for i in range(MAX_CARDS):
        if i < len(events):
            e = events[i]
            # _event_capacity_label 직접 구현
            cap_lab = "∞" if e['unlim'] == 1 else str(e['cap'] or "∞")
            btn_txt = "빠지기" if e['joined'] else ("마감" if (cap_lab != "∞" and e['cnt'] >= int(cap_lab)) else "참여하기")
            updates.extend([gr.update(visible=True), decode_photo(e['photo']), f"### {e['title']}", f"📍 {e['addr']}\n⏰ {fmt_start(e['start'])} · {remain_text(e['end'], e['start'])}\n👥 {e['cnt']}/{cap_lab}", e['id'], btn_txt])
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

with gr.Blocks(css=CSS, title="오세요") as demo:
    with gr.Row():
        gr.Markdown("# 📍 지금 오세요")
        gr.HTML("<div style='text-align:right'><a href='/logout' target='_parent' style='color:#888;text-decoration:none;font-size:12px;'>로그아웃</a></div>")
    card_boxes=[]; card_imgs=[]; card_titles=[]; card_metas=[]; card_ids=[]; card_btns=[]
    with gr.Row():
        for i in range(MAX_CARDS):
            with gr.Column(visible=False, elem_classes=["event-card"], min_width=300) as box:
                img=gr.Image(show_label=False, interactive=False, elem_classes=["event-img"]); title=gr.Markdown(); meta=gr.Markdown(); hid=gr.Textbox(visible=False); btn=gr.Button("참여하기", elem_classes=["join-btn"])
                card_boxes.append(box); card_imgs.append(img); card_titles.append(title); card_metas.append(meta); card_ids.append(hid); card_btns.append(btn)
    fab = gr.Button("＋", elem_id="fab_btn")
    demo.load(refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)
    for i in range(MAX_CARDS):
        card_btns[i].click(toggle_join_gr, inputs=[card_ids[i]], outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)

# 4) 최종 마운트 및 실행
@app.get("/")
async def pwa_shell(request: Request):
    if not get_user_id_from_req(request): return RedirectResponse(url="/login", status_code=303)
    return HTMLResponse(f'<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/><link rel="manifest" href="/static/manifest.webmanifest"/><title>오세요</title><style>html,body{{height:100%;margin:0;background:#FAF9F6;overflow:hidden;}}iframe{{border:0;width:100%;height:100%;vertical-align:bottom;}}</style></head><body><iframe src="/app" title="오세요"></iframe><script>if("serviceWorker" in navigator){{navigator.serviceWorker.register("/static/sw.js");}}</script></body></html>')

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with get_cursor() as cur: cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

app = gr.mount_gradio_app(app, demo, path="/app")
try: app.mount("/static", StaticFiles(directory="static"), name="static")
except: pass

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
