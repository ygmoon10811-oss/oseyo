# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json, html, hashlib, random
from datetime import datetime, timedelta, timezone
import requests
from PIL import Image
import gradio as gr
from fastapi import FastAPI, Request, Form, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import uvicorn

# 1. 환경 및 DB 초기화
KST = timezone(timedelta(hours=9))
def now_kst(): return datetime.now(KST)

DB_PATH = "oseyo_v7.db"
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def db_conn(): return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db_conn() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, title TEXT, photo TEXT, start TEXT, end TEXT, 
            addr TEXT, lat REAL, lng REAL, created_at TEXT, user_id TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, username TEXT UNIQUE, pw_hash TEXT, created_at TEXT, 
            real_name TEXT, gender TEXT, birthdate TEXT, phone TEXT)""")
        con.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT, expires_at TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS favs (name TEXT PRIMARY KEY, count INTEGER DEFAULT 1)")
        con.commit()
init_db()

# 2. 보안 유틸리티
def make_pw_hash(pw):
    salt = uuid.uuid4().hex
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000)
    return f"{salt}${base64.b64encode(dk).decode()}"

def check_pw(pw, stored):
    try:
        salt, b64 = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000)
        return base64.b64encode(dk).decode() == b64
    except: return False

def get_user_by_token(token):
    if not token: return None
    with db_conn() as con:
        row = con.execute("SELECT u.id, u.username, u.real_name FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.expires_at > ?", (token, now_kst().isoformat())).fetchone()
    return {"id": row[0], "username": row[1], "real_name": row[2]} if row else None

# 3. Gradio 기능 함수
def get_list_html():
    with db_conn() as con:
        rows = con.execute("SELECT title, photo, start, addr FROM events ORDER BY created_at DESC").fetchall()
    if not rows: return "<div style='text-align:center;padding:100px;color:#aaa;'>등록된 이벤트가 없습니다.</div>"
    out = "<div style='padding:10px 20px 80px 20px;'>"
    for r in rows:
        img_tag = f"<img src='data:image/jpeg;base64,{r[1]}' style='width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:12px;'>" if r[1] else ""
        out += f"<div style='margin-bottom:30px;'>{img_tag}<div style='margin-top:10px;'><b style='font-size:18px;'>{html.escape(r[0])}</b><p style='color:#666;font-size:14px;margin:5px 0;'>📅 {r[2]}<br>📍 {r[3]}</p></div></div>"
    return out + "</div>"

def get_fav_tags():
    with db_conn() as con:
        rows = con.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 5").fetchall()
    tags = [[r[0]] for r in rows if r[0]]
    return gr.update(visible=len(tags)>0, samples=tags)

def save_event(title, img, start, end, addr_obj, req: gr.Request):
    user = get_user_by_token(req.cookies.get("oseyo_session"))
    if not user or not title: return "저장 실패"
    pic = ""
    if img is not None:
        im = Image.fromarray(img).convert("RGB")
        im.thumbnail((800,800)); buf = io.BytesIO(); im.save(buf, "JPEG", quality=85); pic = base64.b64encode(buf.getvalue()).decode()
    with db_conn() as con:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex[:8], title, pic, start, end, addr_obj.get('name',''), addr_obj.get('y',0), addr_obj.get('x',0), now_kst().isoformat(), user['id']))
        con.execute("INSERT INTO favs (name, count) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET count = count + 1", (title,))
        con.commit()
    return "등록 완료"

def get_my_events(req: gr.Request):
    user = get_user_by_token(req.cookies.get("oseyo_session"))
    if not user: return gr.update(choices=[])
    with db_conn() as con:
        rows = con.execute("SELECT id, title FROM events WHERE user_id=? ORDER BY created_at DESC", (user['id'],)).fetchall()
    return gr.update(choices=[(r[1], r[0]) for r in rows])

def delete_event(eid, req: gr.Request):
    user = get_user_by_token(req.cookies.get("oseyo_session"))
    if not user or not eid: return "실패"
    with db_conn() as con:
        con.execute("DELETE FROM events WHERE id=? AND user_id=?", (eid, user['id']))
        con.commit()
    return "삭제 완료"

# 4. UI 구성
CSS = """
.fab { position:fixed !important; right:24px; bottom:30px; z-index:900; border-radius:50%; width:56px; height:56px; background:#222; color:white; font-size:28px; border:none; box-shadow:0 4px 12px rgba(0,0,0,0.3); }
.modal-box { position:fixed !important; top:50%; left:50%; transform:translate(-50%, -50%); width:90%; max-width:400px; background:white; border-radius:20px; z-index:1000; padding:20px; box-shadow:0 10px 40px rgba(0,0,0,0.2); max-height:85vh; overflow-y:auto; }
.overlay { position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:999; }
"""

with gr.Blocks(css=CSS) as demo:
    search_state = gr.State([]); addr_state = gr.State({})

    gr.HTML("<div style='padding:20px;display:flex;justify-content:space-between;align-items:center;'><div><h2 style='margin:0;'>지금, <b>열려 있습니다</b></h2></div><a href='/logout' style='color:#888;text-decoration:none;'>로그아웃</a></div>")
    
    with gr.Tabs():
        with gr.Tab("탐색"):
            list_view = gr.HTML()
            ref_btn = gr.Button("새로고침", size="sm")
        with gr.Tab("지도"):
            gr.HTML('<iframe src="/map" style="width:100%;height:60vh;border:none;"></iframe>')

    fab = gr.Button("+", elem_classes="fab")
    overlay = gr.HTML("<div class='overlay'></div>", visible=False)
    
    with gr.Column(visible=False, elem_classes="modal-box") as modal:
        with gr.Tabs():
            with gr.Tab("글쓰기"):
                fav_ds = gr.Dataset(components=[gr.Textbox(visible=False)], label="자주 하는 활동", samples=[])
                t_in = gr.Textbox(label="제목", placeholder="예: 조용히 책 읽기")
                img_in = gr.Image(label="사진", type="numpy")
                with gr.Row():
                    s_in = gr.Textbox(label="시작", value=lambda: now_kst().strftime("%Y-%m-%d %H:%M"))
                    e_in = gr.Textbox(label="종료", value=lambda: (now_kst()+timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"))
                addr_in = gr.Textbox(label="장소", interactive=False)
                search_btn = gr.Button("장소 검색")
                with gr.Row():
                    c_btn = gr.Button("취소"); ok_btn = gr.Button("등록", variant="primary")
            with gr.Tab("관리"):
                my_drop = gr.Dropdown(label="삭제할 글 선택")
                del_btn = gr.Button("삭제하기", variant="stop")
                manage_close = gr.Button("닫기")

    with gr.Column(visible=False, elem_classes="modal-box") as s_modal:
        q_in = gr.Textbox(label="어디로 갈까요?"); q_res = gr.Radio(label="검색 결과")
        with gr.Row():
            s_cancel = gr.Button("닫기"); s_ok = gr.Button("선택")

    # 인터랙션 바인딩
    demo.load(get_list_html, None, list_view)
    ref_btn.click(get_list_html, None, list_view)
    
    # FAB 클릭 시 모달 열기 + 즐겨찾기 갱신 + 관리 탭 드롭다운 갱신
    fab.click(lambda req: (gr.update(visible=True), gr.update(visible=True), get_fav_tags(), get_my_events(req)), 
              None, [overlay, modal, fav_ds, my_drop])
    
    c_btn.click(lambda: (gr.update(visible=False), gr.update(visible=False)), None, [overlay, modal])
    manage_close.click(lambda: (gr.update(visible=False), gr.update(visible=False)), None, [overlay, modal])
    fav_ds.click(lambda x: x[0], fav_ds, t_in)
    
    search_btn.click(lambda: gr.update(visible=True), None, s_modal)
    def do_search(q):
        r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", 
                         headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}, params={"query":q}).json()
        items = [{"label":f"{d['place_name']} ({d['address_name']})", "name":d['place_name'], "x":d['x'], "y":d['y']} for d in r.get("documents", [])]
        return items, gr.update(choices=[x['label'] for x in items])
    q_in.submit(do_search, q_in, [search_state, q_res])
    s_ok.click(lambda sel, cands: (sel, next(x for x in cands if x['label']==sel), gr.update(visible=False)), [q_res, search_state], [addr_in, addr_state, s_modal])
    s_cancel.click(lambda: gr.update(visible=False), None, s_modal)
    
    ok_btn.click(save_event, [t_in, img_in, s_in, e_in, addr_state], None).then(get_list_html, None, list_view).then(lambda: (gr.update(visible=False), gr.update(visible=False)), None, [overlay, modal])
    del_btn.click(delete_event, [my_drop], None).then(get_list_html, None, list_view).then(lambda req: get_my_events(req), None, my_drop)

# 5. FastAPI 서버
app = FastAPI()

@app.get("/map")
def map_v():
    with db_conn() as con:
        rows = con.execute("SELECT title, lat, lng, addr FROM events").fetchall()
    evs = json.dumps([{"t":r[0], "lat":r[1], "lng":r[2], "addr":r[3]} for r in rows])
    tmpl = """
    <div id="m" style="width:100%;height:100vh;"></div>
    <script src="//dapi.kakao.com/v2/maps/sdk.js?appkey=JS_KEY"></script>
    <script>
      const map = new kakao.maps.Map(document.getElementById('m'), {center:new kakao.maps.LatLng(36.5, 127.5), level:12});
      const data = JSON_DATA;
      data.forEach(d => {
        if(!d.lat) return;
        const m = new kakao.maps.Marker({position: new kakao.maps.LatLng(d.lat, d.lng), map: map});
        kakao.maps.event.addListener(m, 'click', () => {
          new kakao.maps.InfoWindow({content: `<div style="padding:10px;"><b>${d.t}</b><br><small>${d.addr}</small></div>`, removable:true}).open(map, m);
        });
      });
    </script>
    """.replace("JS_KEY", KAKAO_JAVASCRIPT_KEY).replace("JSON_DATA", evs)
    return HTMLResponse(tmpl)

@app.get("/signup")
def signup_page():
    return HTMLResponse("""
    <style>body{font-family:'Pretendard', sans-serif; padding:40px; max-width:400px; margin:auto;}</style>
    <h2>회원가입</h2>
    <form method="post">
      <p>아이디(이메일)<br><input name="username" style="width:100%" required></p>
      <p>비밀번호<br><input name="password" type="password" style="width:100%" required></p>
      <p>이름<br><input name="real_name" style="width:100%" required></p>
      <p>성별 <select name="gender"><option value="M">남</option><option value="F">여</option></select> / 생일 <input name="birthdate" type="date"></p>
      <p>휴대폰 번호<br><input name="phone" id="ph" placeholder="01012345678" style="width:60%"> <button type="button" onclick="alert('테스트 인증번호: 123456')">인증</button></p>
      <p>인증번호<br><input id="vcode" placeholder="123456" style="width:60%"> <button type="button" onclick="if(document.getElementById('vcode').value=='123456'){alert('인증성공');document.getElementById('sub').disabled=false;}">확인</button></p>
      <button id="sub" disabled style="width:100%; padding:10px; background:#222; color:white;">가입하기</button>
    </form>
    """)

@app.post("/signup")
def signup_do(username:str=Form(...), password:str=Form(...), real_name:str=Form(...), gender:str=Form("M"), birthdate:str=Form(""), phone:str=Form("")):
    with db_conn() as con:
        con.execute("INSERT INTO users (id,username,pw_hash,created_at,real_name,gender,birthdate,phone) VALUES (?,?,?,?,?,?,?,?)", 
                    (uuid.uuid4().hex, username, make_pw_hash(password), now_kst().isoformat(), real_name, gender, birthdate, phone))
        con.commit()
    return RedirectResponse("/login", 303)

@app.get("/login")
def login_page(): return HTMLResponse("<div style='padding:50px; max-width:300px; margin:auto;'><form method='post'><h3>로그인</h3><input name='username' placeholder='이메일' style='width:100%'><br><br><input name='password' type='password' style='width:100%'><br><br><button style='width:100%'>로그인</button></form><br><a href='/signup'>회원가입</a></div>")

@app.post("/login")
def login_do(username:str=Form(...), password:str=Form(...)):
    with db_conn() as con: row = con.execute("SELECT id, pw_hash FROM users WHERE username=?", (username,)).fetchone()
    if row and check_pw(password, row[1]):
        tk = uuid.uuid4().hex
        with db_conn() as con: con.execute("INSERT INTO sessions VALUES (?,?,?)", (tk, row[0], (now_kst()+timedelta(days=7)).isoformat()))
        r = RedirectResponse("/app", 303); r.set_cookie("oseyo_session", tk, httponly=True); return r
    return "실패"

@app.get("/logout")
def logout(): r = RedirectResponse("/login", 303); r.delete_cookie("oseyo_session"); return r

@app.middleware("http")
async def auth_guard(r: Request, call):
    if r.url.path.startswith("/app") and not get_user_by_token(r.cookies.get("oseyo_session")): return RedirectResponse("/login", 303)
    return await call(r)

app = gr.mount_gradio_app(app, demo, path="/app")
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json, html, hashlib, random
from datetime import datetime, timedelta, timezone
import requests
from PIL import Image
import gradio as gr
from fastapi import FastAPI, Request, Form, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import uvicorn

# 1. 환경 설정 및 DB 초기화
KST = timezone(timedelta(hours=9))
def now_kst(): return datetime.now(KST)

DB_PATH = "oseyo_complete.db"
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()
SMS_CODES = {}

def db_conn(): return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db_conn() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, title TEXT, photo TEXT, start TEXT, end TEXT, 
            addr TEXT, lat REAL, lng REAL, created_at TEXT, user_id TEXT)""")
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, username TEXT UNIQUE, pw_hash TEXT, created_at TEXT, 
            real_name TEXT, gender TEXT, birthdate TEXT, phone TEXT)""")
        con.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT, expires_at TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS favs (name TEXT PRIMARY KEY, count INTEGER DEFAULT 1)")
        con.commit()
init_db()

# 2. 보안 유틸리티
def make_pw_hash(pw):
    salt = uuid.uuid4().hex
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000)
    return f"{salt}${base64.b64encode(dk).decode()}"

def check_pw(pw, stored):
    try:
        salt, b64 = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000)
        return base64.b64encode(dk).decode() == b64
    except: return False

def get_user_by_token(token):
    if not token: return None
    with db_conn() as con:
        row = con.execute("""SELECT u.id, u.username, u.real_name FROM sessions s 
                             JOIN users u ON u.id = s.user_id 
                             WHERE s.token = ? AND s.expires_at > ?""", 
                          (token, now_kst().isoformat())).fetchone()
    return {"id": row[0], "username": row[1], "real_name": row[2]} if row else None

# 3. Gradio 비즈니스 로직
def get_list_html():
    with db_conn() as con:
        rows = con.execute("SELECT title, photo, start, addr FROM events ORDER BY created_at DESC").fetchall()
    if not rows: return "<div style='text-align:center;padding:100px;color:#aaa;'>등록된 이벤트가 없습니다.</div>"
    
    out = "<div style='padding:20px;'>"
    for r in rows:
        img_tag = f"<img src='data:image/jpeg;base64,{r[1]}' style='width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:12px;'>" if r[1] else ""
        out += f"""<div style='margin-bottom:25px;'>
            {img_tag}
            <div style='margin-top:10px;'>
                <b style='font-size:18px;'>{html.escape(r[0])}</b>
                <p style='color:#666;font-size:14px;margin:5px 0;'>📅 {r[2]}<br>📍 {r[3]}</p>
            </div>
        </div>"""
    return out + "</div>"

def get_fav_tags():
    with db_conn() as con:
        rows = con.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 5").fetchall()
    tags = [[r[0]] for r in rows if r[0]]
    return gr.update(visible=len(tags)>0, samples=tags)

def save_event(title, img, start, end, addr_obj, req: gr.Request):
    user = get_user_by_token(req.cookies.get("oseyo_session"))
    if not user: return "로그인 필요"
    pic = ""
    if img is not None:
        im = Image.fromarray(img).convert("RGB")
        im.thumbnail((800,800))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        pic = base64.b64encode(buf.getvalue()).decode()
    
    with db_conn() as con:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)", 
                    (uuid.uuid4().hex[:8], title, pic, start, end, addr_obj.get('name',''), addr_obj.get('y',0), addr_obj.get('x',0), now_kst().isoformat(), user['id']))
        con.execute("INSERT INTO favs (name, count) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET count = count + 1", (title,))
        con.commit()
    return "등록 완료"

def get_my_events(req: gr.Request):
    user = get_user_by_token(req.cookies.get("oseyo_session"))
    if not user: return []
    with db_conn() as con:
        rows = con.execute("SELECT id, title FROM events WHERE user_id=? ORDER BY created_at DESC", (user['id'],)).fetchall()
    return [(r[1], r[0]) for r in rows]

def delete_event(eid, req: gr.Request):
    user = get_user_by_token(req.cookies.get("oseyo_session"))
    if not user or not eid: return "삭제 실패"
    with db_conn() as con:
        con.execute("DELETE FROM events WHERE id=? AND user_id=?", (eid, user['id']))
        con.commit()
    return "삭제 완료"

# 4. UI (Gradio)
CSS = """
.fab { position:fixed !important; right:24px; bottom:30px; z-index:999; border-radius:50%; width:56px; height:56px; background:#222; color:white; font-size:28px; border:none; box-shadow:0 4px 12px rgba(0,0,0,0.3); }
.modal-box { position:fixed !important; top:50%; left:50%; transform:translate(-50%, -50%); width:90%; max-width:400px; background:white; border-radius:20px; z-index:1000; padding:20px; box-shadow:0 10px 40px rgba(0,0,0,0.2); max-height:85vh; overflow-y:auto; }
.overlay { position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:999; }
"""

with gr.Blocks(css=CSS) as demo:
    search_state = gr.State([])
    addr_state = gr.State({})

    gr.HTML("<div style='padding:20px;'><h2>지금, <b>열려 있습니다</b></h2><a href='/logout' style='color:#888;'>로그아웃</a></div>")
    
    with gr.Tabs():
        with gr.Tab("탐색"):
            list_view = gr.HTML()
            ref_btn = gr.Button("새로고침", size="sm")
        with gr.Tab("지도"):
            gr.HTML('<iframe src="/map" style="width:100%;height:60vh;border:none;"></iframe>')

    fab = gr.Button("+", elem_classes="fab")
    overlay = gr.HTML("<div class='overlay'></div>", visible=False)
    
    with gr.Column(visible=False, elem_classes="modal-box") as modal:
        with gr.Tabs():
            with gr.Tab("글쓰기"):
                fav_ds = gr.Dataset(components=[gr.Textbox(visible=False)], label="자주 하는 활동", samples=[])
                t_in = gr.Textbox(label="제목", placeholder="예: 산책하기")
                img_in = gr.Image(label="사진", type="numpy")
                with gr.Row():
                    s_in = gr.Textbox(label="시작", value=lambda: now_kst().strftime("%Y-%m-%d %H:%M"))
                    e_in = gr.Textbox(label="종료", value=lambda: (now_kst()+timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"))
                addr_in = gr.Textbox(label="장소", interactive=False)
                search_btn = gr.Button("장소 검색")
                with gr.Row():
                    c_btn = gr.Button("취소")
                    ok_btn = gr.Button("등록", variant="primary")
            with gr.Tab("관리"):
                my_drop = gr.Dropdown(label="내 글 선택")
                del_btn = gr.Button("삭제하기", variant="stop")
                manage_close = gr.Button("닫기")

    # 검색 보조 모달
    with gr.Column(visible=False, elem_classes="modal-box") as s_modal:
        q_in = gr.Textbox(label="키워드 검색")
        q_res = gr.Radio(label="결과 선택")
        with gr.Row():
            s_cancel = gr.Button("닫기")
            s_ok = gr.Button("선택")

    # 이벤트 바인딩
    demo.load(get_list_html, None, list_view)
    ref_btn.click(get_list_html, None, list_view)
    
    fab.click(lambda: (gr.update(visible=True), gr.update(visible=True), get_fav_tags()), None, [overlay, modal, fav_ds])
    c_btn.click(lambda: (gr.update(visible=False), gr.update(visible=False)), None, [overlay, modal])
    fav_ds.click(lambda x: x[0], fav_ds, t_in)
    
    search_btn.click(lambda: gr.update(visible=True), None, s_modal)
    def do_search(q):
        r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", 
                         headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}, params={"query":q}).json()
        items = [{"label":f"{d['place_name']} ({d['address_name']})", "name":d['place_name'], "x":d['x'], "y":d['y']} for d in r.get("documents", [])]
        return items, gr.update(choices=[x['label'] for x in items])
    q_in.submit(do_search, q_in, [search_state, q_res])
    s_ok.click(lambda sel, cands: (sel, next(x for x in cands if x['label']==sel), gr.update(visible=False)), [q_res, search_state], [addr_in, addr_state, s_modal])
    
    ok_btn.click(save_event, [t_in, img_in, s_in, e_in, addr_state], None).then(get_list_html, None, list_view).then(lambda: (gr.update(visible=False), gr.update(visible=False)), None, [overlay, modal])
    
    modal.change(lambda req: gr.update(choices=get_my_events(req)), None, my_drop)
    del_btn.click(delete_event, [my_drop], None).then(get_list_html, None, list_view)
    manage_close.click(lambda: (gr.update(visible=False), gr.update(visible=False)), None, [overlay, modal])

# 5. FastAPI 서버 로직
app = FastAPI()

@app.get("/map")
def map_view():
    with db_conn() as con:
        rows = con.execute("SELECT title, photo, lat, lng, addr FROM events").fetchall()
    evs = json.dumps([{"t":r[0], "p":r[1], "lat":r[2], "lng":r[3], "addr":r[4]} for r in rows])
    
    # f-string 충돌 방지용 템플릿 처리
    tmpl = """
    <div id="map" style="width:100%;height:100vh;"></div>
    <script src="//dapi.kakao.com/v2/maps/sdk.js?appkey=JS_KEY"></script>
    <script>
      const map = new kakao.maps.Map(document.getElementById('map'), {center:new kakao.maps.LatLng(36.5, 127.5), level:12});
      const data = JSON_DATA;
      data.forEach(d => {
        if(!d.lat) return;
        const m = new kakao.maps.Marker({position: new kakao.maps.LatLng(d.lat, d.lng), map: map});
        kakao.maps.event.addListener(m, 'click', () => {
          new kakao.maps.InfoWindow({content: `<div style="padding:10px;"><b>${d.t}</b><br><small>${d.addr}</small></div>`, removable:true}).open(map, m);
        });
      });
    </script>
    """.replace("JS_KEY", KAKAO_JAVASCRIPT_KEY).replace("JSON_DATA", evs)
    return HTMLResponse(tmpl)

@app.get("/signup")
def signup_p():
    return HTMLResponse("""
    <style>body{font-family:sans-serif; padding:40px;}</style>
    <h2>회원가입</h2>
    <form method="post">
      <input name="username" placeholder="이메일" required><br><br>
      <input name="password" type="password" placeholder="비밀번호" required><br><br>
      <input name="real_name" placeholder="이름" required><br><br>
      <select name="gender"><option value="M">남</option><option value="F">여</option></select><br><br>
      <input name="birthdate" type="date"><br><br>
      <input name="phone" id="ph" placeholder="01012345678"> 
      <button type="button" onclick="alert('인증번호 123456이 발송되었습니다.')">인증요청</button><br><br>
      <input placeholder="인증번호 123456" id="code"><br><br>
      <button>가입하기</button>
    </form>
    """)

@app.post("/signup")
def signup_do(username:str=Form(...), password:str=Form(...), real_name:str=Form(...), gender:str=Form("M"), birthdate:str=Form(""), phone:str=Form("")):
    with db_conn() as con:
        con.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, username, make_pw_hash(password), now_kst().isoformat(), real_name, gender, birthdate, phone))
        con.commit()
    return RedirectResponse("/login", 303)

@app.get("/login")
def login_p(): return HTMLResponse("<form method='post' style='padding:50px;'><input name='username' placeholder='이메일'><br><br><input name='password' type='password'><br><br><button>로그인</button><br><br><a href='/signup'>회원가입</a></form>")

@app.post("/login")
def login_do(username:str=Form(...), password:str=Form(...)):
    with db_conn() as con:
        row = con.execute("SELECT id, pw_hash FROM users WHERE username=?", (username,)).fetchone()
    if row and check_pw(password, row[1]):
        tk = uuid.uuid4().hex
        with db_conn() as con: con.execute("INSERT INTO sessions VALUES (?,?,?)", (tk, row[0], (now_kst()+timedelta(days=7)).isoformat()))
        r = RedirectResponse("/app", 303); r.set_cookie("oseyo_session", tk, httponly=True); return r
    return "로그인 실패"

@app.get("/logout")
def logout(): r = RedirectResponse("/login", 303); r.delete_cookie("oseyo_session"); return r

@app.middleware("http")
async def auth_check(r: Request, call):
    if r.url.path.startswith("/app") and not get_user_by_token(r.cookies.get("oseyo_session")): return RedirectResponse("/login", 303)
    return await call(r)

app = gr.mount_gradio_app(app, demo, path="/app")
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

