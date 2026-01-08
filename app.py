# -*- coding: utf-8 -*-
import os
import uuid
import base64
import io
import sqlite3
import json
import html
import hashlib
import random
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image

import gradio as gr
from fastapi import FastAPI, Request, Form, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import uvicorn


# =========================================================
# 0) 기본 설정
# =========================================================
KST = timezone(timedelta(hours=9))

def now_kst():
    return datetime.now(KST)

COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 24 * 7  # 7일 로그인 유지

# 카카오 키 (없으면 지도 등 일부 기능 제한, 앱은 안 터짐)
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

# SMS 인증번호 임시 저장소 (메모리)
SMS_CODES = {}


# =========================================================
# 1) 환경/DB (마이그레이션 로직 강화)
# =========================================================
def pick_db_path():
    candidates = ["./data", ".", "/tmp"]
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, "oseyo_final_v4.db") # 버전 업
        except:
            continue
    return "oseyo_final_v4.db"

DB_PATH = pick_db_path()
print(f"[DB] Using: {DB_PATH}")

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db_conn() as con:
        # 1. 이벤트
        con.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                title TEXT,
                photo TEXT,
                start TEXT,
                end TEXT,
                addr TEXT,
                lat REAL,
                lng REAL,
                created_at TEXT,
                user_id TEXT
            );
        """)
        
        # 2. 사용자 (컬럼 대거 추가)
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE,
                pw_hash TEXT,
                created_at TEXT,
                real_name TEXT,
                gender TEXT,
                birthdate TEXT,
                phone TEXT
            );
        """)
        
        # 3. 세션
        con.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT,
                expires_at TEXT
            );
        """)

        # 4. 즐겨찾기(통계용)
        con.execute("""
            CREATE TABLE IF NOT EXISTS favs (
                name TEXT PRIMARY KEY,
                count INTEGER DEFAULT 1
            );
        """)
        
        # 마이그레이션: 기존 DB 사용자를 위해 컬럼이 없으면 추가 (try-catch)
        # 4.0 버전 업데이트: users 테이블에 개인정보 컬럼 추가
        try:
            con.execute("ALTER TABLE users ADD COLUMN real_name TEXT")
            con.execute("ALTER TABLE users ADD COLUMN gender TEXT")
            con.execute("ALTER TABLE users ADD COLUMN birthdate TEXT")
            con.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        except:
            pass # 이미 있으면 패스

        con.commit()

init_db()


# =========================================================
# 2) 보안/세션 유틸
# =========================================================
def make_pw_hash(pw: str) -> str:
    salt = uuid.uuid4().hex
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 120000)
    return f"{salt}${base64.b64encode(dk).decode('utf-8')}"

def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, b64 = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 120000)
        return base64.b64encode(dk).decode("utf-8") == b64
    except:
        return False

def cleanup_sessions():
    try:
        now_iso = now_kst().isoformat()
        with db_conn() as con:
            con.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso,))
            con.commit()
    except:
        pass

def new_session(user_id: str) -> str:
    cleanup_sessions()
    token = uuid.uuid4().hex
    exp = now_kst() + timedelta(hours=SESSION_HOURS)
    with db_conn() as con:
        con.execute("INSERT INTO sessions VALUES (?,?,?)", (token, user_id, exp.isoformat()))
        con.commit()
    return token

def get_user_by_token(token: str):
    if not token: return None
    cleanup_sessions()
    with db_conn() as con:
        row = con.execute(
            "SELECT u.id, u.username, u.real_name FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,)
        ).fetchone()
    if not row: return None
    return {"id": row[0], "username": row[1], "real_name": row[2]}

def get_current_user(request: gr.Request):
    if not request: return None
    token = request.cookies.get(COOKIE_NAME)
    return get_user_by_token(token)


# =========================================================
# 3) CSS (Pretendard + 모달 스타일)
# =========================================================
CSS = """
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

html, body {
  margin: 0 !important; padding: 0 !important;
  font-family: Pretendard, sans-serif !important;
  background-color: #ffffff !important;
}
.gradio-container { max-width: 100% !important; padding: 0 !important; margin: 0 !important;}

/* 헤더 */
.header-row {
    padding: 20px 24px 10px 24px;
    display: flex; justify-content: space-between; align-items: flex-start;
}
.main-title { font-size: 26px; font-weight: 300; color: #111; line-height: 1.3; }
.main-title b { font-weight: 700; }
.logout-btn {
    font-size: 13px; color: #999; text-decoration: none;
    background: #f5f5f5; padding: 6px 10px; border-radius: 14px;
}

/* 탭 */
.tabs { border-bottom: 1px solid #eee; margin-top: 10px; }
.tabs button.selected { color: #000 !important; font-weight: 800 !important; border-bottom: 2px solid #000 !important; }

/* FAB 버튼 */
.fab-wrapper {
  position: fixed !important; right: 24px !important; bottom: 30px !important; z-index: 9000 !important;
}
.fab-wrapper button {
  width: 56px !important; height: 56px !important; border-radius: 50% !important;
  background: #222 !important; color: white !important; font-size: 30px !important;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3) !important; border: none !important;
}

/* 모달 */
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.5); z-index: 9998; }
.main-modal {
  position: fixed !important; top: 50%; left: 50%; transform: translate(-50%, -50%);
  width: 90vw; max-width: 420px; max-height: 85vh; background: white; z-index: 9999;
  border-radius: 20px; box-shadow: 0 20px 60px rgba(0,0,0,0.2);
  display: flex; flex-direction: column; overflow: hidden;
}
.modal-header { padding: 18px; border-bottom: 1px solid #f0f0f0; font-weight: 700; text-align: center; }
.modal-body { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 12px; }
.modal-footer { padding: 14px 20px; border-top: 1px solid #f0f0f0; background: #fff; display: flex; gap: 8px; }

.btn-primary { background: #222 !important; color: white !important; }
.btn-secondary { background: #f0f0f0 !important; color: #555 !important; }
.btn-danger { background: #fff0f0 !important; color: #d32f2f !important; }

/* 리스트 스타일 */
.event-card {
  display: block; margin-bottom: 30px; cursor: pointer;
}
.event-photo {
  width: 100%; aspect-ratio: 16/9; object-fit: cover; border-radius: 12px;
  margin-bottom: 12px; background-color: #f7f7f7; border: 1px solid #eaeaea;
}
.event-title { font-size: 18px; font-weight: 700; color: #222; margin-bottom: 4px; }
.event-meta { font-size: 14px; color: #777; display: flex; align-items: center; gap: 6px; }

/* 즐겨찾기 칩 스타일 */
.fav-chip-container { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.fav-chip {
    padding: 6px 12px; border-radius: 20px; background: #f0f0f0; 
    color: #333; font-size: 13px; font-weight: 600; cursor: pointer; border: 1px solid #ddd;
}
.fav-chip:hover { background: #e0e0e0; }
"""


# =========================================================
# 4) 로직 함수
# =========================================================

# (A) 탐색 탭 리스트 HTML 생성
def get_list_html():
    try:
        with db_conn() as con:
            # 최신순 정렬
            rows = con.execute(
                "SELECT title, photo, start, addr FROM events ORDER BY created_at DESC"
            ).fetchall()
    except Exception:
        return "DB Error"

    if not rows:
        return "<div style='text-align:center; padding:100px 20px; color:#aaa; font-size:14px;'>등록된 이벤트가 없습니다.<br>오른쪽 아래 + 버튼을 눌러보세요.</div>"

    out = "<div style='padding:10px 24px 80px 24px;'>"
    for title, photo, start, addr in rows:
        img_html = ""
        if photo:
            img_html = f"<img class='event-photo' src='data:image/jpeg;base64,{photo}' />"
        else:
            img_html = "<div class='event-photo' style='display:flex;align-items:center;justify-content:center;color:#ccc;'>이미지 없음</div>"
        
        # 날짜 포맷팅
        try:
            dt = datetime.strptime(start, "%Y-%m-%d %H:%M")
            time_str = dt.strftime("%m월 %d일 %H:%M")
        except:
            time_str = start

        out += f"""
        <div class='event-card'>
          {img_html}
          <div class='event-info'>
            <div class='event-title'>{html.escape(title or "")}</div>
            <div class='event-meta'>📅 {html.escape(time_str)}</div>
            <div class='event-meta'>📍 {html.escape(addr or "장소 미정")}</div>
          </div>
        </div>
        """
    return out + "</div>"

# (B) 자주 사용하는 제목(즐겨찾기) 가져오기
def get_fav_tags():
    try:
        with db_conn() as con:
            # 많이 사용된 상위 5개 제목
            rows = con.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 5").fetchall()
        
        if not rows:
            return gr.update(visible=False), []
        
        # HTML로 칩 만들기 (Gradio HTML 컴포넌트에 넣기엔 클릭 이벤트가 복잡하므로, Gradio Dataset 컴포넌트 활용이 나음.
        # 하지만 여기선 디자인 요구사항에 맞춰 단순 버튼들로 대체하거나 Dataset 사용)
        tags = [r[0] for r in rows if r[0]]
        return gr.update(visible=True, samples=tags), tags # samples for Dataset
    except:
        return gr.update(visible=False), []

# (C) 글 저장
def save_data(title, img, start, end, addr_obj, request: gr.Request):
    user = get_current_user(request)
    if not user:
        return "로그인이 필요합니다."

    title = (title or "").strip()
    if not title:
        return "제목을 입력해 주세요"

    addr_obj = addr_obj or {}
    
    # 이미지 처리
    pic_b64 = ""
    if img is not None:
        try:
            im = Image.fromarray(img).convert("RGB")
            im.thumbnail((800, 800))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            pic_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        except:
            pass

    with db_conn() as con:
        con.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex[:8], title, pic_b64, start, end,
                addr_obj.get("name", ""), addr_obj.get("y", 0), addr_obj.get("x", 0),
                now_kst().isoformat(), user['id']
            ),
        )
        # 즐겨찾기 카운트 증가
        con.execute(
            "INSERT INTO favs (name, count) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET count = count + 1",
            (title,),
        )
        con.commit()

    return "✅ 등록되었습니다"

# (D) 내 글 삭제 관련
def get_my_events(request: gr.Request):
    user = get_current_user(request)
    if not user: return []
    with db_conn() as con:
        rows = con.execute("SELECT id, title FROM events WHERE user_id=? ORDER BY created_at DESC", (user['id'],)).fetchall()
    return [(f"{r[1]}", r[0]) for r in rows]

def delete_my_event(eid, request: gr.Request):
    user = get_current_user(request)
    if not user or not eid: return "삭제 실패", gr.update()
    with db_conn() as con:
        con.execute("DELETE FROM events WHERE id=? AND user_id=?", (eid, user['id']))
        con.commit()
    return "✅ 삭제 완료", gr.update(choices=get_my_events(request), value=None)


# =========================================================
# 5) Gradio UI
# =========================================================
now_dt = now_kst()

with gr.Blocks(css=CSS, title="오세요") as demo:
    # 상태 변수
    search_state = gr.State([])
    selected_addr = gr.State({})

    gr.HTML("""
    <div class="header-row">
        <div class="main-title">지금, <b>열려 있습니다</b><br>
        <span style="font-size:15px; color:#888;">편하면 오셔도 됩니다</span></div>
        <a href="/logout" class="logout-btn">로그아웃</a>
    </div>
    """)

    with gr.Tabs(elem_classes=["tabs"]):
        with gr.Tab("탐색"):
            # 앱 로딩 시 바로 데이터가 보이도록 함
            explore_html = gr.HTML() 
            refresh_btn = gr.Button("새로고침", variant="secondary", size="sm")
        with gr.Tab("지도"):
            gr.HTML('<iframe src="/map" style="width:100%;height:65vh;border:none;border-radius:12px;"></iframe>')

    # FAB
    with gr.Row(elem_classes=["fab-wrapper"]):
        fab = gr.Button("+")

    overlay = gr.HTML("<div class='overlay'></div>", visible=False)

    # --- 메인 모달 ---
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_m:
        gr.HTML("<div class='modal-header'>이벤트 만들기</div>")
        
        with gr.Tabs(elem_classes=["modal-body"]):
            with gr.Tab("글쓰기"):
                # 즐겨찾기(자주 쓴 제목) 섹션
                gr.Markdown("###### 자주 하는 활동", elem_id="fav-label")
                # Dataset 컴포넌트를 사용하여 클릭 시 텍스트박스에 입력되게 함
                fav_dataset = gr.Dataset(
                    label="",
                    components=[gr.Textbox(visible=False)], 
                    headers=None,
                    samples=[],
                    visible=False
                )

                t_in = gr.Textbox(label="제목", placeholder="예: 산책해요", lines=1)
                
                # 즐겨찾기 클릭 이벤트
                def fill_title(data):
                    return data[0] # 선택한 샘플의 첫번째 요소(제목) 반환
                fav_dataset.click(fill_title, inputs=fav_dataset, outputs=t_in)

                with gr.Accordion("사진 (선택)", open=False):
                    img_in = gr.Image(label="사진", type="numpy", height=150)

                with gr.Row():
                    s_in = gr.Textbox(label="시작", value=now_dt.strftime("%Y-%m-%d %H:%M"))
                    e_in = gr.Textbox(label="종료", value=(now_dt+timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"))

                addr_v = gr.Textbox(label="장소", interactive=False, placeholder="검색 필요")
                addr_btn = gr.Button("🔍 장소 검색", size="sm")

                with gr.Row(elem_classes=["modal-footer"]):
                    m_close = gr.Button("취소", elem_classes=["btn-secondary"])
                    m_save = gr.Button("등록", elem_classes=["btn-primary"])

            with gr.Tab("관리"):
                my_list = gr.Dropdown(label="내 글 선택", interactive=True)
                del_btn = gr.Button("삭제하기", elem_classes=["btn-danger"])
                del_msg = gr.Markdown("")
                with gr.Row(elem_classes=["modal-footer"]):
                    del_close = gr.Button("닫기", elem_classes=["btn-secondary"])

    # --- 검색 모달 ---
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_s:
        gr.HTML("<div class='modal-header'>장소 검색</div>")
        with gr.Column(elem_classes=["modal-body"]):
            q_in = gr.Textbox(label="검색어")
            q_btn = gr.Button("검색", elem_classes=["btn-primary"])
            q_res = gr.Radio(label="결과", interactive=True)
        with gr.Row(elem_classes=["modal-footer"]):
            s_close = gr.Button("취소", elem_classes=["btn-secondary"])
            s_final = gr.Button("선택", elem_classes=["btn-primary"])

    # --- 이벤트 연결 ---

    # 1. 앱 시작 시 데이터 로드 (새로고침 문제 해결)
    demo.load(get_list_html, None, explore_html)
    
    refresh_btn.click(get_list_html, None, explore_html)

    # 2. 글쓰기 모달 열 때 즐겨찾기 갱신 + 내 글 목록 갱신
    def open_modal_logic(req: gr.Request):
        ds_upd, tags = get_fav_tags()
        my_ev = get_my_events(req)
        return (
            gr.update(visible=True), gr.update(visible=True), # overlay, modal
            ds_upd, # fav dataset update
            gr.update(choices=my_ev, value=None), "" # delete dropdown
        )

    fab.click(open_modal_logic, None, [overlay, modal_m, fav_dataset, my_list, del_msg])
    
    # 닫기
    def close_all(): return [gr.update(visible=False)]*3
    m_close.click(close_all, None, [overlay, modal_m, modal_s])
    del_close.click(close_all, None, [overlay, modal_m, modal_s])

    # 장소 검색
    addr_btn.click(lambda: gr.update(visible=True), None, modal_s)
    s_close.click(lambda: gr.update(visible=False), None, modal_s)

    def search_k(q):
        if not KAKAO_REST_API_KEY: return [], gr.update(choices=["API 키 없음"])
        try:
            h = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
            r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", headers=h, params={"query":q})
            items = []
            for d in r.json().get("documents", []):
                items.append({"label": f"{d['place_name']} ({d['address_name']})", "name": d['place_name'], "x": d['x'], "y": d['y']})
            return items, gr.update(choices=[x['label'] for x in items], value=None)
        except: return [], gr.update(choices=["에러 발생"])
    
    q_btn.click(search_k, q_in, [search_state, q_res])
    
    def confirm_k(sel, cands):
        found = next((x for x in cands if x['label']==sel), None)
        if not found: return "", {}, gr.update(visible=False)
        return found['label'], found, gr.update(visible=False)

    s_final.click(confirm_k, [q_res, search_state], [addr_v, selected_addr, modal_s])

    # 저장
    m_save.click(
        save_data, 
        [t_in, img_in, s_in, e_in, selected_addr],
        [explore_html] # 결과 메시지 대신 바로 리스트 갱신 시도 (메시지 팝업은 없지만 리스트가 바뀜)
    ).then(
        get_list_html, None, explore_html
    ).then(
        close_all, None, [overlay, modal_m, modal_s]
    )

    # 삭제
    del_btn.click(delete_my_event, [my_list], [del_msg, my_list]).then(get_list_html, None, explore_html)


# =========================================================
# 6) FastAPI + 인증/회원가입
# =========================================================
app = FastAPI()

# SMS 모의 전송 API
@app.post("/send-code")
async def send_sms_code(item: dict = Body(...)):
    phone = item.get("phone")
    if not phone: return JSONResponse({"success": False, "msg": "번호 오류"})
    
    # 6자리 랜덤 생성 (실제론 여기서 SMS API 호출)
    code = str(random.randint(100000, 999999))
    # 테스트 편의를 위해 무조건 123456도 허용하거나, 콘솔에 출력
    print(f"=============================")
    print(f"[SMS 발송] {phone} : {code}")
    print(f"=============================")
    
    SMS_CODES[phone] = code
    # 테스트용: 알림창에 띄우기 위해 응답에 포함 (실제 서비스에선 절대 금지)
    return JSONResponse({"success": True, "debug_code": code})

@app.post("/verify-code")
async def verify_sms_code(item: dict = Body(...)):
    phone = item.get("phone")
    code = item.get("code")
    
    # 123456은 마스터 키 (테스트용)
    if code == "123456":
        return JSONResponse({"success": True})

    stored = SMS_CODES.get(phone)
    if stored and stored == code:
        return JSONResponse({"success": True})
    
    return JSONResponse({"success": False})


# 미들웨어/라우트
PUBLIC = {"/", "/login", "/signup", "/logout", "/health", "/map", "/send-code", "/verify-code"}

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path in PUBLIC:
        return await call_next(request)
    if path.startswith("/app"):
        if not get_user_by_token(request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/login", status_code=303)
    return await call_next(request)

@app.get("/")
def root(r: Request):
    if get_user_by_token(r.cookies.get(COOKIE_NAME)): return RedirectResponse("/app", status_code=303)
    return RedirectResponse("/login", status_code=303)

@app.get("/login")
def login_page():
    return HTMLResponse("""
    <!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
    <style>
      @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
      body{font-family:Pretendard;display:flex;justify-content:center;align-items:center;height:100vh;margin:0;background:#f9f9f9;}
      .box{width:320px;padding:30px;background:white;border-radius:16px;box-shadow:0 4px 20px rgba(0,0,0,0.05);text-align:center;}
      h1{font-weight:300;margin:0 0 30px 0;} h1 b{font-weight:700;}
      input{width:100%;padding:14px;margin-bottom:10px;border:1px solid #ddd;border-radius:8px;box-sizing:border-box;}
      button{width:100%;padding:14px;background:#222;color:white;border:none;border-radius:8px;font-weight:700;cursor:pointer;}
      a{font-size:13px;color:#888;text-decoration:none;display:inline-block;margin-top:20px;}
    </style></head><body>
      <div class="box">
        <h1><b>오세요</b></h1>
        <form method="post" action="/login">
          <input name="username" placeholder="아이디 (이메일)" required/>
          <input name="password" type="password" placeholder="비밀번호" required/>
          <button type="submit">로그인</button>
        </form>
        <a href="/signup">회원가입</a>
      </div>
    </body></html>
    """)

@app.post("/login")
def login_proc(username:str=Form(...), password:str=Form(...)):
    try:
        with db_conn() as con:
            row = con.execute("SELECT id, pw_hash FROM users WHERE username=?", (username,)).fetchone()
    except: row=None
    
    if row and check_pw(password, row[1]):
        resp = RedirectResponse("/app", status_code=303)
        resp.set_cookie(COOKIE_NAME, new_session(row[0]), httponly=True)
        return resp
    return HTMLResponse("<script>alert('정보가 일치하지 않습니다');history.back();</script>")

# --- 회원가입 페이지 (JS 로직 포함) ---
@app.get("/signup")
def signup_page():
    return HTMLResponse("""
    <!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
    <style>
      @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
      body{font-family:Pretendard;background:#fff;margin:0;padding:20px;display:flex;justify-content:center;}
      .container{width:100%;max-width:360px;}
      h2{font-weight:700;margin-bottom:20px;}
      .field{margin-bottom:16px;}
      label{display:block;font-size:13px;color:#666;margin-bottom:6px;font-weight:600;}
      input, select{width:100%;padding:12px;border:1px solid #ddd;border-radius:8px;box-sizing:border-box;font-size:14px;}
      .row{display:flex;gap:8px;}
      .btn-sm{width:80px;background:#eee;color:#333;border:none;border-radius:8px;font-size:13px;cursor:pointer;font-weight:600;}
      .submit-btn{width:100%;padding:16px;background:#ccc;color:white;border:none;border-radius:8px;font-weight:700;font-size:16px;cursor:not-allowed;margin-top:20px;}
      .submit-btn.active{background:#222;cursor:pointer;}
    </style></head><body>
    
    <div class="container">
      <h2>회원가입</h2>
      <form id="frm" method="post" action="/signup">
        
        <div class="field">
          <label>아이디 (이메일)</label>
          <input name="username" type="email" required placeholder="user@example.com"/>
        </div>
        <div class="field">
          <label>비밀번호</label>
          <input name="password" type="password" required placeholder="8자 이상 권장"/>
        </div>
        
        <div class="field">
          <label>이름 (실명)</label>
          <input name="real_name" required placeholder="홍길동"/>
        </div>
        
        <div class="field">
            <label>성별</label>
            <select name="gender">
                <option value="M">남성</option>
                <option value="F">여성</option>
                <option value="N">선택안함</option>
            </select>
        </div>

        <div class="field">
            <label>생년월일</label>
            <input name="birthdate" type="date" required value="2000-01-01"/>
        </div>

        <div class="field">
          <label>휴대폰 번호</label>
          <div class="row">
            <input id="ph" name="phone" type="tel" placeholder="01012345678" />
            <button type="button" class="btn-sm" onclick="sendCode()">인증요청</button>
          </div>
        </div>
        
        <div class="field" id="code-box" style="display:none;">
          <label>인증번호</label>
          <div class="row">
            <input id="cd" type="text" placeholder="인증번호 6자리" />
            <button type="button" class="btn-sm" onclick="verifyCode()">확인</button>
          </div>
          <p id="msg" style="font-size:12px;color:red;margin-top:4px;"></p>
        </div>

        <input type="hidden" name="verified" id="verified" value="false">

        <button type="submit" id="sbtn" class="submit-btn" disabled>가입 완료</button>
      </form>
    </div>

    <script>
      let isVerified = false;

      function sendCode(){
        const ph = document.getElementById('ph').value;
        if(ph.length < 10){ alert('올바른 번호를 입력해주세요'); return; }
        
        fetch('/send-code', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({phone: ph})
        })
        .then(r=>r.json())
        .then(d=>{
            if(d.success){
                alert('인증번호가 발송되었습니다. (테스트용: ' + d.debug_code + ')');
                document.getElementById('code-box').style.display = 'block';
            } else {
                alert('발송 실패');
            }
        });
      }

      function verifyCode(){
        const ph = document.getElementById('ph').value;
        const cd = document.getElementById('cd').value;
        
        fetch('/verify-code', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({phone: ph, code: cd})
        })
        .then(r=>r.json())
        .then(d=>{
            if(d.success){
                document.getElementById('msg').style.color='green';
                document.getElementById('msg').innerText = '인증되었습니다.';
                isVerified = true;
                document.getElementById('verified').value = 'true';
                document.getElementById('ph').readOnly = true;
                
                // 가입 버튼 활성화
                const btn = document.getElementById('sbtn');
                btn.disabled = false;
                btn.classList.add('active');
            } else {
                document.getElementById('msg').innerText = '번호가 틀렸습니다.';
            }
        });
      }
      
      document.getElementById('frm').onsubmit = function(e){
        if(!isVerified){
            e.preventDefault();
            alert('휴대폰 인증을 완료해주세요.');
        }
      }
    </script>
    </body></html>
    """)

@app.post("/signup")
def signup_proc(
    username:str=Form(...), password:str=Form(...),
    real_name:str=Form(...), gender:str=Form("N"), birthdate:str=Form(""), phone:str=Form("")
):
    try:
        with db_conn() as con:
            # users 테이블에 정보 저장
            con.execute(
                "INSERT INTO users (id, username, pw_hash, created_at, real_name, gender, birthdate, phone) VALUES (?,?,?,?,?,?,?,?)", 
                (uuid.uuid4().hex, username, make_pw_hash(password), now_kst().isoformat(), real_name, gender, birthdate, phone)
            )
            con.commit()
    except Exception as e:
        print(e)
        return HTMLResponse("<script>alert('가입 중 오류가 발생했습니다(아이디 중복 등).');history.back();</script>")
    
    return HTMLResponse("<script>alert('가입되었습니다! 로그인해주세요.');location.href='/login';</script>")

@app.get("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(COOKIE_NAME)
    return r

# --- 지도 iframe ---
@app.get("/map")
def map_view():
    try:
        with db_conn() as con:
            rows = con.execute("SELECT title, photo, lat, lng, addr FROM events").fetchall()
    except: rows=[]
    data = [{"title":r[0],"photo":r[1],"lat":r[2],"lng":r[3],"addr":r[4]} for r in rows]
    
    if not KAKAO_JAVASCRIPT_KEY: return "지도 API 키 설정 필요"

    return HTMLResponse(f"""
    <!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
    <style>body{{margin:0;}}.iw{{padding:10px;width:200px;}}.iw img{{width:100%;height:100px;object-fit:cover;}}</style>
    </head><body><div id="m" style="width:100%;height:100vh;"></div>
    <script src="//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}"></script>
    <script>
      const map = new kakao.maps.Map(document.getElementById('m'), {{center:new kakao.maps.LatLng(36.5, 127.5), level:13}});
      const data = {json.dumps(data)};
      if(data.length>0 && data[0].lat) map.setCenter(new kakao.maps.LatLng(data[0].lat, data[0].lng));
      if(data.length>0) map.setLevel(7);

      data.forEach(d=>{
        if(!d.lat) return;
        const mk = new kakao.maps.Marker({{position:new kakao.maps.LatLng(d.lat, d.lng), map:map}});
        const c = `<div class="iw"><b>${{d.title}}</b><br>${{d.photo?`<img src="data:image/jpeg;base64,${{d.photo}}">`:''}}<br><small>${{d.addr}}</small></div>`;
        const iw = new kakao.maps.InfoWindow({{content:c, removable:true}});
        kakao.maps.event.addListener(mk, 'click', ()=>iw.open(map, mk));
      });
    </script></body></html>
    """)

app = gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
