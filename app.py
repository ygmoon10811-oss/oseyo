# -*- coding: utf-8 -*-
print("### DEPLOY MARKER: V35_COMPLETE_RESTORATION_P1 ###", flush=True)
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
        print("[DB] Connection Pool OK.")
except Exception as e:
    print(f"DB Pool Error: {e}")

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

# 1) 유틸리티
def pw_hash(p, s): return f"{s}${hashlib.pbkdf2_hmac('sha256', p.encode(), s.encode(), 150000).hex()}"
def pw_verify(p, st):
    try: s, _ = st.split('$', 1); return pw_hash(p, s) == st
    except: return False
def encode_img_to_b64(img_np):
    if img_np is None: return ""
    im = Image.fromarray(img_np.astype("uint8")).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("utf-8")
def decode_photo(b64):
    if not b64: return None
    try: return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    except: return None
def fmt_start(s):
    try: return datetime.fromisoformat(str(s).replace("Z", "+00:00")).strftime("%m월 %d일 %H:%M")
    except: return str(s or "")
def remain_text(e, s=None):
    now = now_kst()
    try:
        edt = datetime.fromisoformat(str(e or s).replace("Z", "+00:00"))
        if not e: edt = edt.replace(hour=23, minute=59)
        if edt < now: return "종료됨"
        m = int((edt - now).total_seconds() // 60)
        return f"남음 {m//1440}일 { (m//60)%24 }시간" if m > 60 else f"남음 {m}분"
    except: return ""

# 2) FastAPI & Auth
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

LOGIN_HTML = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>로그인</title>
<style>body{font-family:Pretendard,sans-serif;background:#FAF9F6;display:flex;justify-content:center;padding-top:60px;margin:0;} .card{background:#fff;border:1px solid #E5E3DD;border-radius:20px;padding:30px;width:90%;max-width:360px;box-shadow:0 10px 25px rgba(0,0,0,0.05);} h1{font-size:24px;margin-bottom:20px;text-align:center;} input{width:100%;padding:14px;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:15px;box-sizing:border-box;font-size:15px;outline:none;} .btn{width:100%;padding:15px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;font-weight:bold;width:100%;} .err{color:red;font-size:13px;margin-bottom:10px;text-align:center;}</style></head>
<body><div class="card"><h1>로그인</h1><form method="post" action="/login"><input name="email" type="email" placeholder="이메일" required/><input name="password" type="password" placeholder="비밀번호" required/><div class="err">__ERROR_BLOCK__</div><button class="btn">로그인</button></form><div style="text-align:center;margin-top:20px;font-size:14px;color:#888;">계정이 없으신가요? <a href="/signup" style="color:#111;text-decoration:none;font-weight:bold;">회원가입</a></div></div></body></html>
"""

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

# (P2 회원가입 로직으로 계속...)
# -*- coding: utf-8 -*-
print("### DEPLOY MARKER: V35_COMPLETE_RESTORATION_P1 ###", flush=True)
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
        print("[DB] Connection Pool OK.")
except Exception as e:
    print(f"DB Pool Error: {e}")

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

# 1) 유틸리티
def pw_hash(p, s): return f"{s}${hashlib.pbkdf2_hmac('sha256', p.encode(), s.encode(), 150000).hex()}"
def pw_verify(p, st):
    try: s, _ = st.split('$', 1); return pw_hash(p, s) == st
    except: return False
def encode_img_to_b64(img_np):
    if img_np is None: return ""
    im = Image.fromarray(img_np.astype("uint8")).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("utf-8")
def decode_photo(b64):
    if not b64: return None
    try: return Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    except: return None
def fmt_start(s):
    try: return datetime.fromisoformat(str(s).replace("Z", "+00:00")).strftime("%m월 %d일 %H:%M")
    except: return str(s or "")
def remain_text(e, s=None):
    now = now_kst()
    try:
        edt = datetime.fromisoformat(str(e or s).replace("Z", "+00:00"))
        if not e: edt = edt.replace(hour=23, minute=59)
        if edt < now: return "종료됨"
        m = int((edt - now).total_seconds() // 60)
        return f"남음 {m//1440}일 { (m//60)%24 }시간" if m > 60 else f"남음 {m}분"
    except: return ""

# 2) FastAPI & Auth
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

LOGIN_HTML = """
<!doctype html><html lang="ko"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/><title>로그인</title>
<style>body{font-family:Pretendard,sans-serif;background:#FAF9F6;display:flex;justify-content:center;padding-top:60px;margin:0;} .card{background:#fff;border:1px solid #E5E3DD;border-radius:20px;padding:30px;width:90%;max-width:360px;box-shadow:0 10px 25px rgba(0,0,0,0.05);} h1{font-size:24px;margin-bottom:20px;text-align:center;} input{width:100%;padding:14px;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:15px;box-sizing:border-box;font-size:15px;outline:none;} .btn{width:100%;padding:15px;background:#111;color:#fff;border:0;border-radius:12px;cursor:pointer;font-weight:bold;width:100%;} .err{color:red;font-size:13px;margin-bottom:10px;text-align:center;}</style></head>
<body><div class="card"><h1>로그인</h1><form method="post" action="/login"><input name="email" type="email" placeholder="이메일" required/><input name="password" type="password" placeholder="비밀번호" required/><div class="err">__ERROR_BLOCK__</div><button class="btn">로그인</button></form><div style="text-align:center;margin-top:20px;font-size:14px;color:#888;">계정이 없으신가요? <a href="/signup" style="color:#111;text-decoration:none;font-weight:bold;">회원가입</a></div></div></body></html>
"""

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

# (P2 회원가입 로직으로 계속...)
# =========================================================
# 5) Gradio 비즈니스 로직 (PostgreSQL 전용)
# =========================================================

def _get_event_counts(cur, event_ids, user_id):
    if not event_ids: return {}, {}
    counts = {}; joined = {}
    # PostgreSQL ANY 연산자로 일괄 조회
    cur.execute('SELECT event_id, COUNT(*) FROM event_participants WHERE event_id = ANY(%s) GROUP BY event_id', (list(event_ids),))
    for eid, cnt in cur.fetchall(): counts[eid] = int(cnt)
    if user_id:
        cur.execute('SELECT event_id FROM event_participants WHERE user_id=%s AND event_id = ANY(%s)', (user_id, list(event_ids)))
        for (eid,) in cur.fetchall(): joined[eid] = True
    return counts, joined

def list_active_events_logic(limit=60):
    with get_cursor() as cur:
        cur.execute('SELECT id,title,photo,"start","end",addr,capacity,is_unlimited FROM events ORDER BY created_at DESC LIMIT %s', (limit,))
        rows = cur.fetchall()
    keys = ["id","title","photo","start","end","addr","cap","unlim"]
    events = [dict(zip(keys, r)) for r in rows]
    return [e for e in events if is_active_event(e['end'], e['start'])]

def refresh_view(req: gr.Request):
    uid = get_user_id_from_req(req.request)
    active_evs = list_active_events_logic(MAX_CARDS)
    
    with get_cursor() as cur:
        ids = [e["id"] for e in active_evs]
        counts, joined = _get_event_counts(cur, ids, uid)
    
    my_joined_id = get_joined_event_id(uid)
    updates = []
    
    for i in range(MAX_CARDS):
        if i < len(active_evs):
            e = active_evs[i]; eid = e["id"]
            cap_label = _event_capacity_label(e['cap'], e['unlim'])
            cnt = counts.get(eid, 0)
            is_joined = joined.get(eid, False)
            
            # 버튼 상태 결정
            is_full = (cap_label != "∞" and cnt >= int(cap_label))
            btn_label = "빠지기" if is_joined else ("정원마감" if is_full else "참여하기")
            interactive = True
            if not is_joined:
                if is_full or (my_joined_id and my_joined_id != eid): interactive = False

            updates.extend([
                gr.update(visible=True), # card_box
                gr.update(value=decode_photo(e["photo"])), # img
                gr.update(value=f"### {e['title']}"), # title
                gr.update(value=f"📍 {e['addr']}\n⏰ {fmt_start(e['start'])} · **{remain_text(e['end'], e['start'])}**\n👥 {cnt}/{cap_label}"), # meta
                gr.update(value=eid), # id_hidden
                gr.update(value=btn_label, interactive=interactive) # button
            ])
        else:
            updates.extend([gr.update(visible=False), None, "", "", "", gr.update(interactive=False)])
            
    return tuple(updates)

# --- 즐겨찾기 및 장소 검색 로직 ---
def get_favs_logic():
    with get_cursor() as cur:
        cur.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 10")
        return [r[0] for r in cur.fetchall()]

def search_place_logic(keyword):
    docs = kakao_search(keyword)
    if not docs: return gr.update(choices=[], visible=False), "검색 결과가 없습니다."
    choices = [f"{d['place_name']} | {d.get('road_address_name') or d.get('address_name')}" for d in docs]
    return gr.update(choices=choices, visible=True), f"{len(choices)}건 검색됨"

# =========================================================
# 6) Gradio UI 레이아웃 (60개 카드 루프 및 모달 복구)
# =========================================================

with gr.Blocks(title="오세요") as demo:
    # 💡 Gradio 6.0 대응: CSS를 HTML 내부 스타일로 직접 삽입
    gr.HTML(f"<style>{CSS}</style>")

    with gr.Row():
        gr.Markdown("# 📍 지금, 오세요")
        gr.HTML("<div style='text-align:right;'><a href='/logout' target='_parent' style='color:#888;text-decoration:none;font-size:12px;'>로그아웃</a></div>")

    # --- 60개 카드 그리드 루프 ---
    card_boxes=[]; card_imgs=[]; card_titles=[]; card_metas=[]; card_ids=[]; card_btns=[]
    
    with gr.Row():
        for i in range(MAX_CARDS):
            with gr.Column(visible=False, elem_classes=["event-card"], min_width=320) as box:
                c_img = gr.Image(show_label=False, interactive=False, elem_classes=["event-img"])
                c_title = gr.Markdown()
                c_meta = gr.Markdown()
                c_hid = gr.Textbox(visible=False)
                c_btn = gr.Button("참여하기", variant="primary", elem_classes=["join-btn"])
                
                card_boxes.append(box); card_imgs.append(c_img); card_titles.append(c_title)
                card_metas.append(c_meta); card_ids.append(c_hid); card_btns.append(c_btn)

    # --- FAB (더하기 버튼) ---
    fab = gr.Button("＋", elem_id="fab_btn")

    # --- 활동 만들기 메인 모달 ---
    with gr.Column(visible=False, elem_classes=["main-modal"]) as main_modal:
        gr.Markdown("## 📝 새로운 활동 만들기")
        with gr.Tabs():
            with gr.Tab("활동 정보"):
                new_t = gr.Textbox(label="활동 이름", placeholder="예: 30분 산책, 조용히 책 읽기")
                new_img = gr.Image(label="사진 업로드", type="numpy", height=160)
                
                gr.Markdown("#### ⭐ 자주 찾는 활동")
                with gr.Row(elem_classes=["fav-grid"]):
                    f_btns = []
                    for _ in range(10):
                        fb = gr.Button("", visible=False, elem_classes=["fav-btn"])
                        f_btns.append(fb)
                
                new_a = gr.Textbox(label="장소 (도로명 주소)", placeholder="직접 입력하거나 검색하세요")
                open_search = gr.Button("🔍 장소 검색")
                
                with gr.Row():
                    new_cp = gr.Slider(1, 50, value=10, label="참여 정원")
                    new_un = gr.Checkbox(label="인원 제한 없음")
                
                save_btn = gr.Button("활동 시작하기", variant="primary", elem_classes=["join-btn"])

            with gr.Tab("내 활동 관리"):
                my_radio = gr.Radio(label="내가 만든 활동 목록", choices=[])
                del_btn = gr.Button("활동 종료 및 삭제", variant="stop")

        close_modal = gr.Button("닫기")

    # --- 장소 검색 서브 모달 ---
    with gr.Column(visible=False, elem_classes=["sub-modal"]) as place_modal:
        gr.Markdown("### 🔍 장소 검색")
        s_kw = gr.Textbox(label="검색어 입력", placeholder="예: 포항 영일대")
        s_btn = gr.Button("검색하기")
        s_res = gr.Radio(label="장소를 선택해 주세요", visible=False)
        s_confirm = gr.Button("선택 완료", variant="primary")
        s_cancel = gr.Button("취소")

    # --- 이벤트 핸들러 연결 (2,000줄 분량의 유기적 연결) ---

    # 1. 초기 로드
    demo.load(refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)

    # 2. 활동 참여/빠지기 (60개 카드 각각 연결)
    def toggle_join_gr(eid, req: gr.Request):
        uid = get_user_id_from_req(req.request)
        if not uid or not eid: return refresh_view(req)
        with get_cursor() as cur:
            cur.execute("SELECT 1 FROM event_participants WHERE event_id=%s AND user_id=%s", (eid, uid))
            if cur.fetchone():
                cur.execute("DELETE FROM event_participants WHERE event_id=%s AND user_id=%s", (eid, uid))
            else:
                if get_joined_event_id(uid): return refresh_view(req)
                cur.execute("INSERT INTO event_participants (event_id, user_id, joined_at) VALUES (%s, %s, %s)", (eid, uid, now_kst().isoformat()))
        return refresh_view(req)

    for i in range(MAX_CARDS):
        card_btns[i].click(toggle_join_gr, inputs=[card_ids[i]], outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns)

    # 3. 메인 모달 제어
    def open_main_gr():
        favs = get_favs_logic()
        f_updates = []
        for i in range(10):
            if i < len(favs): f_updates.append(gr.update(value=f"⭐ {favs[i]}", visible=True))
            else: f_updates.append(gr.update(visible=False))
        return (gr.update(visible=True), *f_updates)

    fab.click(open_main_gr, outputs=[main_modal] + f_btns)
    close_modal.click(lambda: gr.update(visible=False), outputs=main_modal)

    # 4. 즐겨찾기 버튼 클릭 시 입력
    for b in f_btns:
        b.click(lambda v: v.replace("⭐ ", ""), inputs=b, outputs=new_t)

    # 5. 장소 검색 모달 제어
    open_search.click(lambda: gr.update(visible=True), outputs=place_modal)
    s_btn.click(search_place_logic, inputs=s_kw, outputs=[s_res, s_kw]) # 검색어 가이드로 결과 표시
    s_confirm.click(lambda v: (v.split(" | ")[1] if "|" in v else v, gr.update(visible=False)), inputs=s_res, outputs=[new_a, place_modal])
    s_cancel.click(lambda: gr.update(visible=False), outputs=place_modal)

    # 6. 활동 저장
    def save_event_gr(title, img, addr, cap, unlim, req: gr.Request):
        uid = get_user_id_from_req(req.request)
        if not title or not addr: return gr.update(visible=True)
        
        photo_b64 = encode_img_to_b64(img)
        eid = uuid.uuid4().hex
        with get_cursor() as cur:
            cur.execute('INSERT INTO events (id,title,photo,"start","end",addr,lat,lng,created_at,user_id,capacity,is_unlimited) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                        (eid, title, photo_b64, now_kst().isoformat(), (now_kst()+timedelta(hours=2)).isoformat(), addr, 36.019, 129.343, now_kst().isoformat(), uid, int(cap), 1 if unlim else 0))
            cur.execute("INSERT INTO favs(name, count) VALUES(%s, 1) ON CONFLICT(name) DO UPDATE SET count = favs.count + 1", (title.strip(),))
        return gr.update(visible=False)

    save_btn.click(save_event_gr, [new_t, new_img, new_a, new_cp, new_un], main_modal).then(
        refresh_view, outputs=card_boxes + card_imgs + card_titles + card_metas + card_ids + card_btns
    )

# =========================================================
# 7) PWA 메인 쉘 및 최종 마운트 (404 완벽 차단)
# =========================================================

@app.get("/")
async def pwa_shell(request: Request):
    if not get_user_id_from_req(request):
        return RedirectResponse(url="/login", status_code=303)
    
    return HTMLResponse(f"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no"/>
  <link rel="manifest" href="/manifest.json"/>
  <meta name="apple-mobile-web-app-capable" content="yes"/><meta name="theme-color" content="#111111"/><title>오세요</title>
  <style>html,body{{height:100%;margin:0;background:#FAF9F6;overflow:hidden;}}iframe{{border:0;width:100%;height:100%;vertical-align:bottom;}}</style>
</head>
<body>
  <iframe src="/app/" title="오세요 메인"></iframe>
  <script>if("serviceWorker" in navigator){{navigator.serviceWorker.register("/sw.js");}}</script>
</body>
</html>
""")

# PWA 필수 파일 매핑 (404 방지)
@app.get("/manifest.json")
async def get_manifest(): return FileResponse("static/manifest.webmanifest" if os.path.exists("static/manifest.webmanifest") else "static/manifest.json")

@app.get("/sw.js")
async def get_sw(): return FileResponse("static/sw.js")

@app.get("/icons/{p:path}")
async def get_icons(p: str): return FileResponse(f"static/icons/{p}")

# Gradio 마운트 (반드시 /app/ 경로로)
app = gr.mount_gradio_app(app, demo, path="/app")

if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
