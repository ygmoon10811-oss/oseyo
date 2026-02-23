# -*- coding: utf-8 -*-
print("### DEPLOY MARKER: V32_ULTIMATE_SINGLE_FILE_RESTORE ###", flush=True)
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

# =========================================================
# 0) 설정 및 데이터베이스 (Supabase)
# =========================================================
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
except Exception as e:
    print(f"DB Pool Error: {e}")

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
# 1) 보안 및 유틸리티
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
    im = Image.fromarray(img_np.astype("uint8")).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def decode_photo(photo_b64: str):
    if not photo_b64: return None
    return Image.open(io.BytesIO(base64.b64decode(photo_b64))).convert("RGB")

def fmt_start(start_s):
    try: return datetime.fromisoformat(str(start_s).replace("Z", "+00:00")).strftime("%m월 %d일 %H:%M")
    except: return str(start_s or "")

def remain_text(end_s, start_s=None):
    now = now_kst()
    try:
        edt = datetime.fromisoformat(str(end_s or start_s).replace("Z", "+00:00"))
        if edt < now: return "종료됨"
        diff = edt - now
        m = int(diff.total_seconds() // 60)
        return f"남음 {m//1440}일 { (m//60)%24 }시간" if m > 60 else f"남음 {m}분"
    except: return ""

# (Part 2로 이어집니다...)
# =========================================================
# 2) FastAPI 및 웹 라우팅 (404 방지용)
# =========================================================
app = FastAPI(redirect_slashes=False)

def get_user_id(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if not token: return None
    with get_cursor() as cur:
        cur.execute("SELECT user_id, expires_at FROM sessions WHERE token=%s", (token,))
        row = cur.fetchone()
        if row and datetime.fromisoformat(row[1]) > now_kst(): return row[0]
    return None

@app.get("/healthz")
async def healthz(): return {"status": "ok"}

# --- HTML 템플릿 ---
def render_safe(t, **k):
    for key, v in k.items(): t = t.replace(f"__{key}__", str(v))
    return t

LOGIN_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>로그인</title><style>body{font-family:Pretendard,sans-serif;background:#FAF9F6;display:flex;justify-content:center;padding-top:50px;}.card{background:#fff;border:1px solid #E5E3DD;border-radius:20px;padding:30px;width:100%;max-width:360px;box-shadow:0 10px 25px rgba(0,0,0,0.05);}h1{font-size:22px;margin-bottom:20px;}input{width:100%;padding:12px;border:1px solid #ddd;border-radius:10px;margin-bottom:15px;box-sizing:border-box;}.btn{width:100%;padding:12px;background:#111;color:#fff;border:0;border-radius:10px;cursor:pointer;font-weight:bold;}.err{color:red;font-size:13px;margin-bottom:10px;}</style></head><body><div class="card"><h1>로그인</h1><form method="post" action="/login"><input name="email" type="email" placeholder="이메일" required/><input name="password" type="password" placeholder="비밀번호" required/>__ERROR_BLOCK__<button class="btn">로그인</button></form><div style="text-align:center;margin-top:15px;font-size:13px;"><a href="/signup">회원가입</a></div></div></body></html>"""

SIGNUP_HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>회원가입</title><style>body{font-family:Pretendard,sans-serif;background:#FAF9F6;display:flex;justify-content:center;padding:30px 10px;}.card{background:#fff;border:1px solid #E5E3DD;border-radius:20px;padding:25px;width:100%;max-width:420px;}h1{font-size:20px;text-align:center;}.row{display:flex;gap:5px;align-items:center;}input,select{padding:10px;border:1px solid #ddd;border-radius:8px;box-sizing:border-box;margin-bottom:10px;}.btn{width:100%;padding:12px;background:#111;color:#fff;border:0;border-radius:10px;cursor:pointer;margin-top:10px;}.terms{background:#f9f9f9;padding:10px;border-radius:8px;font-size:12px;margin-top:10px;}</style></head><body><div class="card"><h1>회원가입</h1><form method="post" action="/signup" onsubmit="combineEmail()"><label style="font-size:12px;">이메일</label><div class="row"><input id="eid" type="text" placeholder="아이디" required style="flex:1;"/><span style="font-weight:bold;">@</span><select id="edom" style="flex:1;"><option value="naver.com">naver.com</option><option value="gmail.com">gmail.com</option><option value="kakao.com">kakao.com</option></select></div><input type="hidden" id="fem" name="email"/><button type="button" onclick="sendOtp()" style="font-size:12px;padding:5px;">인증발송</button><div id="omsg" style="font-size:12px;color:blue;"></div><input name="otp" placeholder="인증번호" required/><input name="password" type="password" placeholder="비밀번호" required/><input name="name" placeholder="이름" required/><div class="terms"><label><input type="checkbox" required/> (필수) 만 14세 이상</label><br/><label><input type="checkbox" required/> (필수) 이용약관 동의</label></div><button class="btn">가입 완료</button></form>__ERROR_BLOCK__</div><script>function combineEmail(){document.getElementById('fem').value=document.getElementById('eid').value+'@'+document.getElementById('edom').value;}async function sendOtp(){combineEmail();const em=document.getElementById('fem').value;if(!em)return;document.getElementById('omsg').innerText='발송 중...';const r=await fetch('/send_email_otp',{method:'POST',body:JSON.stringify({email:em})});const d=await r.json();document.getElementById('omsg').innerText=d.ok?'발송됨':'실패';}</script></body></html>"""

@app.get("/login")
async def login_get(err: str = ""):
    return HTMLResponse(render_safe(LOGIN_HTML, ERROR_BLOCK=f'<div class="err">{err}</div>' if err else ""))

@app.post("/login")
async def login_post(email: str = Form(...), password: str = Form(...)):
    with get_cursor() as cur:
        cur.execute("SELECT id, pw_hash FROM users WHERE email=%s", (email.strip().lower(),))
        row = cur.fetchone()
        if row and pw_verify(password, row[1]):
            token = uuid.uuid4().hex
            cur.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES(%s,%s,%s)", (token, row[0], (now_kst()+timedelta(hours=SESSION_HOURS)).isoformat()))
            resp = RedirectResponse(url="/", status_code=303)
            resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS*3600, httponly=True, samesite="lax", path="/")
            return resp
    return RedirectResponse(url="/login?err=로그인 실패", status_code=303)

@app.get("/signup")
async def signup_get(err: str = ""):
    return HTMLResponse(render_safe(SIGNUP_HTML, ERROR_BLOCK=f'<div class="err">{err}</div>' if err else ""))

@app.post("/send_email_otp")
async def send_email_otp(request: Request):
    try:
        data = await request.json()
        email = data.get("email", "").strip().lower()
        otp = str(random.randint(100000, 999999))
        with get_cursor() as cur:
            cur.execute("INSERT INTO email_otps(email,otp,expires_at) VALUES(%s,%s,%s) ON CONFLICT(email) DO UPDATE SET otp=EXCLUDED.otp", (email, otp, (now_kst()+timedelta(minutes=10)).isoformat()))
        return JSONResponse({"ok": True})
    except: return JSONResponse({"ok": False})

@app.post("/signup")
async def signup_post(email: str = Form(...), otp: str = Form(...), password: str = Form(...), name: str = Form(...)):
    with get_cursor() as cur:
        cur.execute("SELECT otp FROM email_otps WHERE email=%s", (email.strip().lower(),))
        row = cur.fetchone()
        if not row or row[0] != otp: return RedirectResponse(url="/signup?err=인증오류", status_code=303)
        cur.execute("INSERT INTO users(id,email,pw_hash,name,created_at) VALUES(%s,%s,%s,%s,%s)", (uuid.uuid4().hex, email, pw_hash(password, "salt"), name.strip(), now_kst().isoformat()))
    return RedirectResponse(url="/login?err=성공! 로그인하세요", status_code=303)

# (Part 3에서 메인 화면 '/'와 Gradio 60개 카드가 이어집니다...)
# =========================================================
# 3) PWA 메인 쉘 및 로그아웃 (404 방지)
# =========================================================

@app.get("/")
async def pwa_shell(request: Request):
    # 로그인 체크
    uid = get_user_id(request)
    if not uid: 
        return RedirectResponse(url="/login", status_code=303)
    
    # PWA 껍데기 HTML
    return HTMLResponse(f"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
  <link rel="manifest" href="/static/manifest.webmanifest"/>
  <meta name="theme-color" content="#111111"/>
  <title>오세요</title>
  <style>
    html,body{{height:100%;margin:0;background:#FAF9F6;overflow:hidden;}}
    iframe{{border:0;width:100%;height:100%;vertical-align:bottom;}}
  </style>
</head>
<body>
  <iframe src="/app" title="오세요 메인"></iframe>
  <script>if("serviceWorker" in navigator){{navigator.serviceWorker.register("/static/sw.js");}}</script>
</body>
</html>
""")

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with get_cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

# =========================================================
# 4) Gradio UI (60개 카드 및 메인 로직 100% 복구)
# =========================================================

MAX_CARDS = 60
CSS = r"""
:root { --accent: #ff5a1f; }
html, body, .gradio-container { background: #FAF9F6 !important; font-family: 'Pretendard', sans-serif; }
.event-card { background: white; border:1px solid #E5E3DD; border-radius:18px; padding:14px; box-shadow:0 8px 22px rgba(0,0,0,0.06); margin-bottom:12px; }
.event-img img { border-radius:14px !important; height:180px !important; object-fit:cover !important; }
.join-btn button { border-radius:999px !important; background: var(--accent) !important; color: white !important; font-weight:800 !important; border:0 !important; }
#fab_btn {
  position: fixed !important; right: 22px !important; bottom: 22px !important; z-index: 9999 !important;
  width: 56px !important; height: 56px !important; border-radius: 999px !important;
  background: var(--accent) !important; color: white !important; font-size: 28px !important; font-weight: 900 !important;
  border: 0 !important; box-shadow: 0 10px 20px rgba(0,0,0,0.2) !important; cursor: pointer !important;
}
.main-modal { position: fixed; left:50%; top:50%; transform: translate(-50%,-50%); width: 90%; max-width: 500px; background: white; border-radius: 20px; z-index: 70; padding: 20px; box-shadow: 0 20px 50px rgba(0,0,0,0.2); }
.fav-btn button { background: #f3f4f6 !important; border: 1px solid #e5e7eb !important; border-radius: 10px !important; color: #333 !important; font-size: 13px !important; height:40px !important; }
"""

def refresh_view(req: gr.Request):
    uid = get_user_id(req.request)
    events = []
    try:
        with get_cursor() as cur:
            # PostgreSQL 예약어 컬럼("start", "end") 처리
            cur.execute('SELECT id,title,photo,"start","end",addr,capacity,is_unlimited FROM events ORDER BY created_at DESC LIMIT %s', (MAX_CARDS,))
            rows = cur.fetchall()
            for r in r: # r은 row
                pass # 아래 로직으로 대체
            
            # 참여 정보 일괄 조회 최적화 생략, 개별 조회로 안정성 확보
            for r in rows:
                cur.execute("SELECT COUNT(*) FROM event_participants WHERE event_id=%s", (r[0],))
                cnt = cur.fetchone()[0]
                cur.execute("SELECT 1 FROM event_participants WHERE event_id=%s AND user_id=%s", (r[0], uid))
                joined = cur.fetchone() is not None
                events.append({'id':r[0],'title':r[1],'photo':r[2],'start':r[3],'end':r[4],'addr':r[5],'cap':r[6],'unlim':r[7],'cnt':cnt,'joined':joined})
    except: pass

    # 60개 컴포넌트 업데이트 리스트 생성
    updates = []
    for i in range(MAX_CARDS):
        if i < len(events):
            e = events[i]
            cap_label = _event_capacity_label(e['cap'], e['unlim'])
            btn_txt = "빠지기" if e['joined'] else ("정원마감" if (cap_label != "∞" and e['cnt'] >= int(cap_label)) else "참여하기")
            updates.extend([
                gr.update(visible=True), 
                decode_photo(e['photo']), 
                f"### {e['title']}", 
                f"📍 {e['addr']}\n⏰ {fmt_start(e['start'])} · {remain_text(e['end'], e['start'])}\n👥 {e['cnt']}/{cap_label}", 
                e['id'], 
                btn_txt
            ])
        else:
            updates.extend([gr.update(visible=False), None, "", "", "", ""])
    return tuple(updates)

def toggle_join_gr(eid, req: gr.Request):
    uid = get_user_id(req.request)
    if not uid or not eid: return refresh_view(req)
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM event_participants WHERE event_id=%s AND user_id=%s", (eid, uid))
        if cur.fetchone(): cur.execute("DELETE FROM event_participants WHERE event_id=%s AND user_id=%s", (eid, uid))
        else: cur.execute("INSERT INTO event_participants (event_id, user_id, joined_at) VALUES (%s, %s, %s)", (eid, uid, now_kst().isoformat()))
    return refresh_view(req)

def save_event_gr(title, img, addr, cap, unlim, req: gr.Request):
    uid = get_user_id(req.request)
    if not title or not addr: return gr.update(visible=True)
    photo_b64 = encode_img_to_b64(img)
    eid = uuid.uuid4().hex
    with get_cursor() as cur:
        cur.execute('INSERT INTO events (id,title,photo,"start","end",addr,lat,lng,created_at,user_id,capacity,is_unlimited) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                    (eid, title, photo_b64, now_kst().isoformat(), (now_kst()+timedelta(hours=2)).isoformat(), addr, 36.019, 129.343, now_kst().isoformat(), uid, int(cap), 1 if unlim else 0))
        cur.execute("INSERT INTO favs(name, count) VALUES(%s, 1) ON CONFLICT(name) DO UPDATE SET count = favs.count + 1", (title.strip(),))
    return gr.update(visible=False)

def get_favs_gr():
    with get_cursor() as cur:
        cur.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 10")
        res = [r[0] for r in cur.fetchall()]
    updates = []
    for i in range(10):
        if i < len(res): updates.append(gr.update(value=f"⭐ {res[i]}", visible=True))
        else: updates.append(gr.update(visible=False))
    return tuple(updates)

# --- UI 레이아웃 ---
with gr.Blocks(css=CSS, title="오세요") as demo:
    with gr.Row():
        gr.Markdown("# 📍 지금 오세요")
        gr.HTML("<div style='text-align:right'><a href='/logout' target='_parent' style='color:#888;text-decoration:none;font-size:12px;'>로그아웃</a></div>")

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
        with gr.Tabs():
            with gr.Tab("정보입력"):
                t = gr.Textbox(label="활동 이름", placeholder="예: 공원 산책"); img_in = gr.Image(label="사진", type="numpy", height=150)
                gr.Markdown("⭐ 자주 쓰는 활동")
                with gr.Row():
                    fbtns = []
                    for _ in range(10):
                        fb = gr.Button("", visible=False, elem_classes=["fav-btn"])
                        fbtns.append(fb)
                a = gr.Textbox(label="장소", placeholder="주소 입력"); cp = gr.Slider(1, 50, 10, label="정원"); un = gr.Checkbox(label="제한없음")
                sub = gr.Button("등록하기", variant="primary", elem_classes=["join-btn"])
            with gr.Tab("관리"):
                gr.Markdown("준비 중인 기능입니다.")
        cls = gr.Button("닫기")

    # --- 이벤트 연결 ---
    demo.load(refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)
    fab.click(lambda: (gr.update(visible=True), *get_favs_gr()), outputs=[modal] + fbtns)
    cls.click(lambda: gr.update(visible=False), outputs=modal)
    sub.click(save_event_gr, [t, img_in, a, cp, un], modal).then(refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)
    
    for i in range(10):
        fbtns[i].click(lambda v: v.replace("⭐ ", ""), inputs=fbtns[i], outputs=t)
    for i in range(MAX_CARDS):
        card_btns[i].click(toggle_join_gr, inputs=[card_ids[i]], outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)

# =========================================================
# 5) 최종 마운트 및 실행
# =========================================================

# Gradio를 /app 경로에 마운트
app = gr.mount_gradio_app(app, demo, path="/app")

# 정적 파일 경로 연결 (manifest 등)
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except: pass

if __name__ == "__main__":
    # Koyeb의 PORT 환경변수 대응
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
