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
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn


# =========================================================
# 0) 기본 설정
# =========================================================
KST = timezone(timedelta(hours=9))

def now_kst():
    return datetime.now(KST)

COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 24 * 7  # 7일

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

# 휴대폰 인증 임시 저장소 (실제 운영시에는 Redis 등 사용 권장)
phone_verification_codes = {}


# =========================================================
# 1) 환경/DB
# =========================================================
def pick_db_path():
    candidates = ["/var/data", "/tmp"]
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            test = os.path.join(d, ".writetest")
            with open(test, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test)
            return os.path.join(d, "oseyo_final_v3.db")
        except Exception:
            continue
    return "/tmp/oseyo_final_v3.db"


DB_PATH = pick_db_path()
print(f"[DB] Using: {DB_PATH}")


def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    with db_conn() as con:
        # 이벤트 테이블
        con.execute(
            """
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
            """
        )
        
        # 즐겨찾기 테이블
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS favs (
                name TEXT PRIMARY KEY,
                count INTEGER DEFAULT 1
            );
            """
        )

        # 사용자 테이블 (확장)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE,
                pw_hash TEXT,
                name TEXT,
                gender TEXT,
                birth_date TEXT,
                phone TEXT,
                created_at TEXT
            );
            """
        )
        
        # 기존 users 테이블에 새 컬럼 추가 (마이그레이션)
        for col in ["name", "gender", "birth_date", "phone"]:
            try:
                con.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            except Exception:
                pass
        
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT,
                expires_at TEXT
            );
            """
        )

        con.commit()

init_db()


# =========================================================
# 2) 비밀번호/세션 유틸
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
    except Exception:
        return False

def cleanup_sessions():
    now_iso = now_kst().isoformat()
    with db_conn() as con:
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso,))
        con.commit()

def new_session(user_id: str) -> str:
    cleanup_sessions()
    token = uuid.uuid4().hex
    exp = now_kst() + timedelta(hours=SESSION_HOURS)
    with db_conn() as con:
        con.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, exp.isoformat()),
        )
        con.commit()
    return token

def get_user_by_token(token: str):
    if not token:
        return None
    cleanup_sessions()
    with db_conn() as con:
        row = con.execute(
            """
            SELECT u.id, u.username, u.name
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ?
            """,
            (token,),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1], "name": row[2]}

def get_current_user(request: gr.Request):
    if not request:
        return None
    token = request.cookies.get(COOKIE_NAME)
    return get_user_by_token(token)


# =========================================================
# 2.5) 휴대폰 인증
# =========================================================
def send_verification_code(phone: str) -> str:
    """인증번호 전송 (실제로는 SMS API 사용, 여기서는 시뮬레이션)"""
    code = str(random.randint(100000, 999999))
    phone_verification_codes[phone] = {
        "code": code,
        "expires_at": now_kst() + timedelta(minutes=3)
    }
    print(f"[SMS] {phone} -> 인증번호: {code}")  # 실제로는 SMS 발송
    return code

def verify_phone_code(phone: str, code: str) -> bool:
    """인증번호 확인"""
    if phone not in phone_verification_codes:
        return False
    
    data = phone_verification_codes[phone]
    if now_kst() > data["expires_at"]:
        del phone_verification_codes[phone]
        return False
    
    if data["code"] == code:
        del phone_verification_codes[phone]
        return True
    
    return False


# =========================================================
# 3) CSS
# =========================================================
CSS = """
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

html, body {
  margin: 0 !important; padding: 0 !important;
  font-family: Pretendard, -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif !important;
  background-color: #ffffff !important;
}
.gradio-container { max-width: 100% !important; padding: 0 !important; margin: 0 !important;}

/* 상단 헤더 영역 */
.header-row {
    padding: 20px 24px 10px 24px;
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
}
.main-title {
    font-size: 26px;
    font-weight: 300;
    color: #111;
    line-height: 1.3;
}
.main-title b {
    font-weight: 700;
}
.logout-link {
    font-size: 13px;
    color: #888;
    text-decoration: none;
    margin-top: 4px;
}

/* 탭 스타일 */
.tabs {
    border-bottom: 1px solid #eee;
    margin-top: 10px;
}
button.selected {
    color: #111 !important;
    font-weight: 700 !important;
    border-bottom: 2px solid #111 !important;
}

/* FAB 버튼 */
.fab-wrapper {
  position: fixed !important;
  right: 24px !important;
  bottom: 30px !important;
  z-index: 9999 !important;
  width: auto !important;
  height: auto !important;
}
.fab-wrapper button {
  width: 60px !important;
  height: 60px !important;
  border-radius: 50% !important;
  background: #111 !important;
  color: white !important;
  font-size: 32px !important;
  border: none !important;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3) !important;
  cursor: pointer !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  line-height: 1 !important;
}

/* 오버레이 */
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 10000; }

/* 모달 */
.main-modal {
  position: fixed !important;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 92vw;
  max-width: 500px;
  height: 86vh;
  background: white;
  z-index: 10001;
  border-radius: 24px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.2);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.modal-header {
  padding: 20px;
  border-bottom: 1px solid #f0f0f0;
  font-weight: 700;
  font-size: 18px;
  text-align: center;
}
.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.modal-footer {
  padding: 16px 20px;
  border-top: 1px solid #f0f0f0;
  background: #fff;
  display: flex;
  gap: 10px;
}
.modal-footer button {
  flex: 1;
  padding: 14px;
  border-radius: 12px;
  font-weight: 700;
  border: none;
}
.btn-primary { background: #111 !important; color: white !important; }
.btn-secondary { background: #f0f0f0 !important; color: #333 !important; }

/* 이벤트 카드 */
.event-card {
  margin-bottom: 24px;
  cursor: pointer;
}
.event-photo {
  width: 100%;
  aspect-ratio: 16/9;
  object-fit: cover;
  border-radius: 16px;
  margin-bottom: 12px;
  background-color: #f0f0f0;
  border: 1px solid #eee;
}
.event-info {
  padding: 0 4px;
}
.event-title {
  font-size: 18px;
  font-weight: 700;
  color: #111;
  margin-bottom: 6px;
  line-height: 1.4;
}
.event-meta {
  font-size: 14px;
  color: #666;
  margin-bottom: 2px;
  display: flex;
  align-items: center;
  gap: 4px;
}

/* 즐겨찾기 그리드 */
.fav-section {
  padding: 20px 24px;
  background: #f9f9f9;
  border-bottom: 1px solid #eee;
}
.fav-title {
  font-size: 14px;
  font-weight: 700;
  color: #666;
  margin-bottom: 12px;
}
.fav-grid { 
  display: grid; 
  grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)); 
  gap: 8px; 
}
.fav-grid button { 
  font-size: 13px; 
  padding: 10px 12px; 
  border-radius: 20px; 
  background: white; 
  border: 1px solid #ddd;
  cursor: pointer;
  transition: all 0.2s;
}
.fav-grid button:hover {
  background: #f0f0f0;
  border-color: #999;
}
"""


# =========================================================
# 4) 즐겨찾기 로직
# =========================================================
def get_fav_html():
    """즐겨찾기 섹션 HTML 생성"""
    with db_conn() as con:
        rows = con.execute(
            "SELECT name, count FROM favs ORDER BY count DESC LIMIT 10"
        ).fetchall()
    
    if not rows:
        return ""
    
    buttons_html = "".join([
        f"<button onclick='fillEventTitle(\"{html.escape(r[0])}\")'>{html.escape(r[0])} ({r[1]})</button>"
        for r in rows
    ])
    
    return f"""
    <div class='fav-section'>
        <div class='fav-title'>⭐ 자주 만드는 활동</div>
        <div class='fav-grid'>{buttons_html}</div>
    </div>
    <script>
    function fillEventTitle(title) {{
        // Gradio의 textbox에 값을 채우는 방법
        const titleInput = document.querySelector('input[placeholder*="예:"]');
        if (titleInput) {{
            titleInput.value = title;
            titleInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
        }}
    }}
    </script>
    """


# =========================================================
# 5) 이벤트 로직
# =========================================================
def get_list_html():
    with db_conn() as con:
        rows = con.execute(
            "SELECT title, photo, start, addr FROM events ORDER BY created_at DESC"
        ).fetchall()

    if not rows:
        return "<div style='text-align:center; padding:100px 20px; color:#999;'>등록된 이벤트가 없습니다.<br>오른쪽 아래 버튼을 눌러 시작해보세요.</div>"

    out = "<div style='padding:0 24px 80px 24px;'>"
    for title, photo, start, addr in rows:
        if photo:
            img_html = f"<img class='event-photo' src='data:image/jpeg;base64,{photo}' />"
        else:
            img_html = "<div class='event-photo' style='display:flex;align-items:center;justify-content:center;color:#ccc;'>NO IMAGE</div>"
        
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
            <div class='event-meta'>⏰ {html.escape(time_str)}</div>
            <div class='event-meta'>📍 {html.escape(addr or "장소 미정")}</div>
          </div>
        </div>
        """
    return out + "</div>"


def save_data(title, img, start, end, addr_obj, request: gr.Request):
    user = get_current_user(request)
    if not user:
        return "로그인이 필요합니다.", gr.update(), gr.update()

    title = (title or "").strip()
    if not title:
        return "제목을 입력해 주세요", gr.update(), gr.update()

    if addr_obj is None:
        addr_obj = {}

    pic_b64 = ""
    if img is not None:
        try:
            im = Image.fromarray(img).convert("RGB")
            im.thumbnail((800, 800))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            pic_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception:
            pic_b64 = ""

    addr_name = (addr_obj.get("name") or "").strip()
    lat = addr_obj.get("y") or 0
    lng = addr_obj.get("x") or 0
    try:
        lat = float(lat)
        lng = float(lng)
    except Exception:
        lat, lng = 0.0, 0.0

    with db_conn() as con:
        con.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex[:8],
                title,
                pic_b64,
                start or "",
                end or "",
                addr_name,
                lat,
                lng,
                now_kst().isoformat(timespec="seconds"),
                user['id']
            ),
        )
        # 즐겨찾기 자동 추가
        con.execute(
            "INSERT INTO favs (name, count) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET count = count + 1",
            (title,),
        )
        con.commit()

    return "✅ 이벤트가 생성되었습니다", get_list_html(), get_fav_html()


def get_my_events(request: gr.Request):
    user = get_current_user(request)
    if not user:
        return []
    
    with db_conn() as con:
        rows = con.execute(
            "SELECT id, title FROM events WHERE user_id = ? ORDER BY created_at DESC", 
            (user['id'],)
        ).fetchall()
    
    return [(f"{r[1]}", r[0]) for r in rows]


def delete_my_event(event_id, request: gr.Request):
    user = get_current_user(request)
    if not user or not event_id:
        return "삭제할 이벤트를 선택해주세요.", gr.update(), gr.update()
    
    with db_conn() as con:
        con.execute("DELETE FROM events WHERE id = ? AND user_id = ?", (event_id, user['id']))
        con.commit()
    
    new_list = get_my_events(request)
    return "✅ 삭제되었습니다.", gr.update(choices=new_list, value=None), get_list_html()


# =========================================================
# 6) Gradio UI
# =========================================================
now_dt = now_kst()
later_dt = now_dt + timedelta(hours=2)

with gr.Blocks(css=CSS, title="오세요") as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    # 헤더
    gr.HTML("""
    <div class="header-row">
        <div class="main-title">지금, <b>열려 있습니다</b><br><span style="font-size:15px; color:#666; font-weight:400;">편하면 오셔도 됩니다</span></div>
        <a href="/logout" class="logout-link">로그아웃</a>
    </div>
    """)

    with gr.Tabs(elem_classes=["tabs"]):
        with gr.Tab("탐색"):
            fav_html = gr.HTML(get_fav_html())  # 즐겨찾기 섹션
            explore_html = gr.HTML(get_list_html())
            refresh_btn = gr.Button("🔄 목록 새로고침", variant="secondary", size="sm")
        with gr.Tab("지도"):
            gr.HTML('<iframe src="/map" style="width:100%;height:70vh;border:none;border-radius:16px;"></iframe>')

    # FAB
    with gr.Row(elem_classes=["fab-wrapper"]):
        fab = gr.Button("+")

    overlay = gr.HTML("<div class='overlay'></div>", visible=False)

    # 메인 모달
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_m:
        gr.HTML("<div class='modal-header'>새 이벤트 만들기</div>")

        with gr.Tabs(elem_classes=["modal-body"]):
            with gr.Tab("작성하기"):
                with gr.Column(elem_classes=["modal-body-content"]):
                    t_in = gr.Textbox(label="이벤트명", placeholder="예: 30분 산책, 조용히 책 읽기", lines=1)
                    
                    with gr.Accordion("사진 추가 (선택)", open=False):
                        img_in = gr.Image(label="사진", type="numpy", height=200)

                    with gr.Row():
                        s_in = gr.Textbox(label="시작", value=now_dt.strftime("%Y-%m-%d %H:%M"))
                        e_in = gr.Textbox(label="종료", value=later_dt.strftime("%Y-%m-%d %H:%M"))

                    addr_v = gr.Textbox(label="장소", interactive=False, placeholder="장소를 검색해주세요")
                    addr_btn = gr.Button("🔍 장소 검색", size="sm")

            with gr.Tab("🗑 내 글 관리"):
                gr.Markdown("### 내가 만든 이벤트")
                my_event_list = gr.Dropdown(label="삭제할 이벤트를 선택하세요", choices=[], interactive=True)
                del_btn = gr.Button("선택한 이벤트 삭제", variant="stop")
                del_msg = gr.Markdown("")

        with gr.Row(elem_classes=["modal-footer"]):
            m_close = gr.Button("닫기", elem_classes=["btn-secondary"])
            m_save = gr.Button("등록하기", elem_classes=["btn-primary"])

    # 장소 검색 모달
    with gr.Column(visible=False, elem_classes=["sub-modal", "main-modal"]) as modal_s:
        gr.HTML("<div class='modal-header'>장소 검색</div>")
        with gr.Column(elem_classes=["modal-body"]):
            q_in = gr.Textbox(label="검색어", placeholder="예: 영일대, 포항시청")
            q_btn = gr.Button("검색", variant="primary")
            q_res = gr.Radio(label="검색 결과", choices=[], interactive=True)
        with gr.Row(elem_classes=["modal-footer"]):
            s_close = gr.Button("취소", elem_classes=["btn-secondary"])
            s_final = gr.Button("확정", elem_classes=["btn-primary"])

    # 이벤트 핸들러
    refresh_btn.click(fn=lambda: (get_list_html(), get_fav_html()), outputs=[explore_html, fav_html])

    def open_main_modal(request: gr.Request):
        my_events = get_my_events(request)
        return gr.update(visible=True), gr.update(visible=True), gr.update(choices=my_events, value=None), ""

    fab.click(open_main_modal, None, [overlay, modal_m, my_event_list, del_msg])
    
    def close_all():
        return gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    m_close.click(close_all, None, [overlay, modal_m, modal_s])

    addr_btn.click(lambda: gr.update(visible=True), None, modal_s)
    s_close.click(lambda: gr.update(visible=False), None, modal_s)

    def search_k(q):
        if not q: return [], gr.update(choices=[])
        if not KAKAO_REST_API_KEY: return [], gr.update(choices=["API KEY 필요"])
        
        headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
        res = requests.get(
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            headers=headers, params={"query": q, "size": 5}
        )
        cands = []
        for d in res.json().get("documents", []):
            label = f"{d['place_name']} ({d['address_name']})"
            cands.append({"label": label, "name": d['place_name'], "x": d['x'], "y": d['y']})
        return cands, gr.update(choices=[x["label"] for x in cands], value=None)

    q_btn.click(search_k, q_in, [search_state, q_res])

    def confirm_k(sel, cands):
        item = next((x for x in cands if x["label"] == sel), None)
        if not item: return "", {}, gr.update(visible=False)
        return item["label"], item, gr.update(visible=False)

    s_final.click(confirm_k, [q_res, search_state], [addr_v, selected_addr, modal_s])

    def save_and_close(title, img, start, end, addr, req: gr.Request):
        msg, html_list, fav = save_data(title, img, start, end, addr, req)
        return html_list, fav, gr.update(visible=False), gr.update(visible=False)

    m_save.click(
        save_and_close,
        [t_in, img_in, s_in, e_in, selected_addr],
        [explore_html, fav_html, overlay, modal_m]
    )

    del_btn.click(
        delete_my_event,
        [my_event_list],
        [del_msg, my_event_list, explore_html]
    ).then(
        get_fav_html, None, fav_html
    )


# =========================================================
# 7) FastAPI + 로그인
# =========================================================
app = FastAPI()

PUBLIC_PATHS = {"/", "/login", "/signup", "/logout", "/health", "/map", "/verify-phone", "/check-verification"}

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path or "/"
    if path.startswith("/static") or path.startswith("/assets") or path in PUBLIC_PATHS:
        return await call_next(request)
    
    if path.startswith("/app"):
        token = request.cookies.get(COOKIE_NAME)
        if not get_user_by_token(token):
            return RedirectResponse("/login", status_code=303)

    return await call_next(request)

@app.get("/")
def root(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if get_user_by_token(token):
        return RedirectResponse("/app", status_code=303)
    return RedirectResponse("/login", status_code=303)

@app.get("/login")
def login_page():
    html_content = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"/>
  <title>오세요 - 로그인</title>
  <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
    body {{
      font-family: Pretendard, system-ui;
      background: #fff; margin: 0; padding: 0;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      height: 100vh;
    }}
    .container {{
      width: 100%; max-width: 360px; padding: 20px; text-align: center;
    }}
    h1 {{ font-size: 32px; font-weight: 300; margin: 0 0 10px 0; color: #333; }}
    p.sub {{ font-size: 15px; color: #888; margin-bottom: 40px; }}
    
    .social-btn {{
      display: block; width: 100%; padding: 14px 0; margin-bottom: 10px;
      border-radius: 6px; border: none; font-size: 15px; font-weight: 700; cursor: pointer; text-decoration: none;
      box-sizing: border-box;
    }}
    .naver {{ background: #03C75A; color: white; }}
    .kakao {{ background: #FEE500; color: #000; }}
    
    .divider {{
      margin: 30px 0; position: relative; text-align: center; font-size: 12px; color: #ccc;
    }}
    .divider::before, .divider::after {{
      content: ""; position: absolute; top: 50%; width: 40%; height: 1px; background: #eee;
    }}
    .divider::before {{ left: 0; }}
    .divider::after {{ right: 0; }}
    
    input {{
      width: 100%; padding: 14px; margin-bottom: 10px;
      border: 1px solid #ddd; border-radius: 6px; box-sizing: border-box; font-size: 15px;
    }}
    input:focus {{ outline: none; border-color: #333; }}
    
    .login-btn {{
      width: 100%; padding: 15px; border-radius: 6px; border: none;
      background: #999; color: white; font-weight: 700; font-size: 16px; cursor: pointer; margin-top: 10px;
    }}
    .login-btn:hover {{ background: #777; }}
    
    .footer-link {{ margin-top: 20px; font-size: 13px; color: #888; }}
    .footer-link a {{ color: #333; text-decoration: underline; }}
  </style>
</head>
<body>
  <div class="container">
    <h1>오세요</h1>
    <p class="sub">열려 있는 순간을 나누세요</p>
    
    <button class="social-btn naver" onclick="document.getElementById('uid').focus()">네이버로 시작하기</button>
    <button class="social-btn kakao" onclick="document.getElementById('uid').focus()">카카오로 시작하기</button>
    
    <div class="divider">또는</div>
    
    <form method="post" action="/login">
      <input id="uid" name="username" placeholder="이메일" required />
      <input name="password" type="password" placeholder="비밀번호" required />
      <button type="submit" class="login-btn">로그인</button>
    </form>
    
    <div class="footer-link">
      계정이 없으신가요? <a href="/signup">가입하기</a>
    </div>
  </div>
</body>
</html>
    """
    return HTMLResponse(html_content)
