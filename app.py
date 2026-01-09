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
import re
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image

import gradio as gr
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
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

# ---- reCAPTCHA v2(체크박스) ----
RECAPTCHA_SITE_KEY = os.getenv("RECAPTCHA_SITE_KEY", "").strip()
RECAPTCHA_SECRET_KEY = os.getenv("RECAPTCHA_SECRET_KEY", "").strip()

# ---- 이메일 OTP 설정 ----
EMAIL_OTP_TTL_MINUTES = 10
ALLOW_EMAIL_OTP_DEBUG = os.getenv("ALLOW_EMAIL_OTP_DEBUG", "1").strip()

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465").strip() or "465")
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()  # "오세요 <me@gmail.com>" 형태 가능


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
            return os.path.join(d, "oseyo_final_email_v1.db")
        except Exception:
            continue
    return "/tmp/oseyo_final_email_v1.db"

DB_PATH = pick_db_path()
print(f"[DB] Using: {DB_PATH}")

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db_conn() as con:
        # 이벤트
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
        try:
            con.execute("ALTER TABLE events ADD COLUMN user_id TEXT")
        except Exception:
            pass

        # 즐겨찾기
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS favs (
                name TEXT PRIMARY KEY,
                count INTEGER DEFAULT 1,
                updated_at TEXT
            );
            """
        )
        try:
            con.execute("ALTER TABLE favs ADD COLUMN updated_at TEXT")
        except Exception:
            pass

        # 유저 (이메일 인증)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE,
                pw_hash TEXT,
                name TEXT,
                gender TEXT,
                birth TEXT,
                email_verified_at TEXT,
                created_at TEXT
            );
            """
        )
        # 마이그레이션(예전 DB 대비)
        for col_sql in [
            "ALTER TABLE users ADD COLUMN name TEXT",
            "ALTER TABLE users ADD COLUMN gender TEXT",
            "ALTER TABLE users ADD COLUMN birth TEXT",
            "ALTER TABLE users ADD COLUMN email_verified_at TEXT",
        ]:
            try:
                con.execute(col_sql)
            except Exception:
                pass

        # 세션
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT,
                expires_at TEXT
            );
            """
        )

        # 이메일 OTP
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS email_otps (
                email TEXT PRIMARY KEY,
                code_hash TEXT,
                expires_at TEXT,
                created_at TEXT
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
            SELECT u.id, u.username
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token = ?
            """,
            (token,),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "username": row[1]}

def get_current_user(request: gr.Request):
    if not request:
        return None
    token = request.cookies.get(COOKIE_NAME)
    return get_user_by_token(token)

def verify_recaptcha_v2(token: str, remoteip: str | None = None) -> bool:
    """
    Google reCAPTCHA v2 checkbox server-side verification
    - 토큰(token): form의 g-recaptcha-response
    - remoteip: 선택(있으면 같이 보냄)
    """
    # 개발/로컬에서 키 없으면 우회(원하면 False로 바꿔도 됨)
    if not RECAPTCHA_SECRET_KEY:
        return True

    token = (token or "").strip()
    if not token:
        return False

    data = {
        "secret": RECAPTCHA_SECRET_KEY,
        "response": token,
    }
    if remoteip:
        data["remoteip"] = remoteip

    try:
        r = requests.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data=data,
            timeout=10,
        )
        j = r.json()
        return bool(j.get("success"))
    except Exception:
        return False

# =========================================================
# 3) 이메일 OTP 유틸
# =========================================================
def normalize_email(e: str) -> str:
    return (e or "").strip().lower()

def valid_email(e: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e or ""))

def otp_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()

def create_email_otp(email: str) -> str:
    code = f"{random.randint(0, 999999):06d}"
    exp = now_kst() + timedelta(minutes=EMAIL_OTP_TTL_MINUTES)
    with db_conn() as con:
        con.execute(
            """
            INSERT INTO email_otps (email, code_hash, expires_at, created_at)
            VALUES (?,?,?,?)
            ON CONFLICT(email) DO UPDATE SET
                code_hash=excluded.code_hash,
                expires_at=excluded.expires_at,
                created_at=excluded.created_at
            """,
            (email, otp_hash(code), exp.isoformat(), now_kst().isoformat()),
        )
        con.commit()
    return code

def verify_email_otp(email: str, code: str) -> bool:
    email = normalize_email(email)
    code = (code or "").strip()
    if not (valid_email(email) and re.fullmatch(r"\d{6}", code)):
        return False

    now_iso = now_kst().isoformat()
    with db_conn() as con:
        row = con.execute(
            "SELECT code_hash, expires_at FROM email_otps WHERE email=?",
            (email,),
        ).fetchone()
    if not row:
        return False

    code_h, exp = row[0], row[1]
    if (exp or "") < now_iso:
        return False
    return otp_hash(code) == code_h

def smtp_ready() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)

def send_email(to_email: str, subject: str, body: str):
    if not smtp_ready():
        raise RuntimeError("SMTP env vars missing")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM or SMTP_USER
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)
    return True


# =========================================================
# 4) CSS
# =========================================================
CSS = """
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

html, body {
  margin: 0 !important; padding: 0 !important;
  font-family: Pretendard, -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif !important;
  background-color: #ffffff !important;
}
.gradio-container { max-width: 100% !important; padding: 0 !important; margin: 0 !important;}

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
.main-title b { font-weight: 700; }
.logout-link {
    font-size: 13px;
    color: #888;
    text-decoration: none;
    margin-top: 4px;
}

.tabs { border-bottom: 1px solid #eee; margin-top: 10px; }
button.selected {
    color: #111 !important;
    font-weight: 700 !important;
    border-bottom: 2px solid #111 !important;
}

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

.overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 10000; }

.main-modal {
  position: fixed !important;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 92vw;
  max-width: 520px;
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

.event-card { margin-bottom: 24px; cursor: pointer; }
.event-photo {
  width: 100%;
  aspect-ratio: 16/9;
  object-fit: cover;
  border-radius: 16px;
  margin-bottom: 12px;
  background-color: #f0f0f0;
  border: 1px solid #eee;
}
.event-info { padding: 0 4px; }
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

.fav-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.fav-grid button {
  font-size: 13px !important;
  padding: 10px !important;
  border-radius: 10px !important;
  background: #f7f7f7 !important;
  border: 1px solid #eee !important;
  text-align: left !important;
}
.small-muted { color:#777; font-size:12px; margin-top:-6px; }

/* ===== Desktop(웹)에서만 카드 폭 제한: 모바일은 그대로 ===== */
.list-wrap { padding: 0 24px 80px 24px; }

@media (min-width: 900px) {
  .list-wrap { max-width: 560px; margin: 0 auto; }
  .header-row { max-width: 560px; margin: 0 auto; }
  .tabs { max-width: 560px; margin-left: auto; margin-right: auto; }
  #map_iframe { max-width: 560px; margin: 0 auto; display: block; }
}
"""


# =========================================================
# 5) 이벤트/즐겨찾기 로직
# =========================================================
def get_list_html():
    with db_conn() as con:
        rows = con.execute(
            "SELECT title, photo, start, addr FROM events ORDER BY created_at DESC"
        ).fetchall()

    if not rows:
        return "<div style='text-align:center; padding:100px 20px; color:#999;'>등록된 이벤트가 없습니다.<br>오른쪽 아래 버튼을 눌러 시작해보세요.</div>"

    out = "<div class='list-wrap'>"
    for title, photo, start, addr in rows:
        if photo:
            img_html = f"<img class='event-photo' src='data:image/jpeg;base64,{photo}' />"
        else:
            img_html = "<div class='event-photo' style='display:flex;align-items:center;justify-content:center;color:#ccc;'>NO IMAGE</div>"

        try:
            dt = datetime.strptime(start, "%Y-%m-%d %H:%M")
            time_str = dt.strftime("%m월 %d일 %H:%M")
        except Exception:
            time_str = start or ""

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
        return "로그인이 필요합니다."

    title = (title or "").strip()
    if not title:
        return "제목을 입력해 주세요"

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
                user["id"],
            ),
        )
        con.execute(
            """
            INSERT INTO favs (name, count, updated_at) VALUES (?, 1, ?)
            ON CONFLICT(name) DO UPDATE SET count = count + 1, updated_at=excluded.updated_at
            """,
            (title, now_kst().isoformat(timespec="seconds")),
        )
        con.commit()

    return "✅ 이벤트가 생성되었습니다"

def get_my_events(request: gr.Request):
    user = get_current_user(request)
    if not user:
        return []
    with db_conn() as con:
        rows = con.execute(
            "SELECT id, title FROM events WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    return [(f"{r[1]}", r[0]) for r in rows]

def delete_my_event(event_id, request: gr.Request):
    user = get_current_user(request)
    if not user or not event_id:
        return "삭제할 이벤트를 선택해주세요.", gr.update()

    with db_conn() as con:
        con.execute("DELETE FROM events WHERE id = ? AND user_id = ?", (event_id, user["id"]))
        con.commit()

    new_list = get_my_events(request)
    return "✅ 삭제되었습니다.", gr.update(choices=new_list, value=None)

def get_top_favs(limit=10):
    with db_conn() as con:
        rows = con.execute(
            """
            SELECT name, count FROM favs
            WHERE name IS NOT NULL AND TRIM(name) != ''
            ORDER BY count DESC, updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [{"name": r[0], "count": r[1]} for r in rows]

def get_fav_names(limit=50):
    with db_conn() as con:
        rows = con.execute(
            """
            SELECT name FROM favs
            WHERE name IS NOT NULL AND TRIM(name) != ''
            ORDER BY updated_at DESC, count DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [r[0] for r in rows]

def fav_buttons_update(favs):
    updates = []
    for i in range(10):
        if i < len(favs):
            updates.append(gr.update(value=f"⭐ {favs[i]['name']}", visible=True))
        else:
            updates.append(gr.update(value="", visible=False))
    return updates

def add_fav_only(name: str, request: gr.Request):
    user = get_current_user(request)
    favs = get_top_favs(10)
    fav_names = get_fav_names(50)

    if not user:
        return "로그인이 필요합니다.", gr.update(choices=fav_names, value=None), *fav_buttons_update(favs)

    name = (name or "").strip()
    if not name:
        return "활동명을 입력해 주세요.", gr.update(choices=fav_names, value=None), *fav_buttons_update(favs)

    with db_conn() as con:
        con.execute(
            """
            INSERT INTO favs (name, count, updated_at) VALUES (?, 1, ?)
            ON CONFLICT(name) DO UPDATE SET count = count + 1, updated_at=excluded.updated_at
            """,
            (name, now_kst().isoformat(timespec="seconds")),
        )
        con.commit()

    favs = get_top_favs(10)
    fav_names = get_fav_names(50)
    return "✅ 즐겨찾기에 추가되었습니다.", gr.update(choices=fav_names, value=None), *fav_buttons_update(favs)

def delete_fav(name: str, request: gr.Request):
    user = get_current_user(request)
    favs = get_top_favs(10)
    fav_names = get_fav_names(50)

    if not user:
        return "로그인이 필요합니다.", gr.update(choices=fav_names, value=None), *fav_buttons_update(favs)

    name = (name or "").strip()
    if not name:
        return "삭제할 즐겨찾기를 선택해 주세요.", gr.update(choices=fav_names, value=None), *fav_buttons_update(favs)

    with db_conn() as con:
        con.execute("DELETE FROM favs WHERE name = ?", (name,))
        con.commit()

    favs = get_top_favs(10)
    fav_names = get_fav_names(50)
    return "✅ 즐겨찾기를 삭제했습니다.", gr.update(choices=fav_names, value=None), *fav_buttons_update(favs)


# =========================================================
# 6) Gradio UI
# =========================================================
now_dt = now_kst()
later_dt = now_dt + timedelta(hours=2)

def make_map_iframe():
    ts = int(now_kst().timestamp() * 1000)
    return f'<iframe id="map_iframe" src="/map?ts={ts}" style="width:100%;height:70vh;border:none;border-radius:16px;"></iframe>'

def refresh_list_and_map():
    return get_list_html(), make_map_iframe()

with gr.Blocks(css=CSS, title="오세요") as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    gr.HTML("""
    <div class="header-row">
        <div class="main-title">지금, <b>열려 있습니다</b><br><span style="font-size:15px; color:#666; font-weight:400;">원하면 오셔도 됩니다</span></div>
        <a href="/logout" class="logout-link">로그아웃</a>
    </div>
    """)

    with gr.Tabs(elem_classes=["tabs"]):
        with gr.Tab("탐색"):
            explore_html = gr.HTML()
            refresh_btn = gr.Button("🔄 목록 새로고침", variant="secondary", size="sm")
        with gr.Tab("지도"):
            map_html = gr.HTML(value=make_map_iframe())

    with gr.Row(elem_classes=["fab-wrapper"]):
        fab = gr.Button("+")

    overlay = gr.HTML("<div class='overlay'></div>", visible=False)

    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_m:
        gr.HTML("<div class='modal-header'>새 이벤트 만들기</div>")

        with gr.Tabs(elem_classes=["modal-body"]):
            with gr.Tab("작성하기"):
                gr.Markdown("### ⭐ 자주하는 활동")
                gr.Markdown("<div class='small-muted'>버튼을 누르면 이벤트명에 바로 입력됩니다.</div>")

                fav_btns = []
                with gr.Column(elem_classes=["fav-grid"]):
                    for _ in range(10):
                        fav_btns.append(gr.Button("", visible=False))

                with gr.Row():
                    fav_new = gr.Textbox(label="즐겨찾기 추가", placeholder="예: 30분 산책", lines=1)
                    fav_add_btn = gr.Button("추가", variant="secondary")
                fav_msg = gr.Markdown("")

                # --- 즐겨찾기 삭제 UI 추가 ---
                with gr.Row():
                    fav_del_sel = gr.Dropdown(label="즐겨찾기 삭제", choices=[], interactive=True)
                    fav_del_btn = gr.Button("삭제", variant="stop")
                fav_del_msg = gr.Markdown("")

                gr.Markdown("---")

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

    with gr.Column(visible=False, elem_classes=["sub-modal", "main-modal"]) as modal_s:
        gr.HTML("<div class='modal-header'>장소 검색</div>")
        with gr.Column(elem_classes=["modal-body"]):
            q_in = gr.Textbox(label="검색어", placeholder="예: 영일대, 포항시청")
            q_btn = gr.Button("검색", variant="primary")
            q_res = gr.Radio(label="검색 결과", choices=[], interactive=True)
        with gr.Row(elem_classes=["modal-footer"]):
            s_close = gr.Button("취소", elem_classes=["btn-secondary"])
            s_final = gr.Button("확정", elem_classes=["btn-primary"])

    demo.load(fn=get_list_html, inputs=None, outputs=explore_html)
    refresh_btn.click(fn=get_list_html, outputs=explore_html)

    def open_main_modal(request: gr.Request):
        my_events = get_my_events(request)
        favs = get_top_favs(10)
        fav_names = get_fav_names(50)
        return (
            gr.update(visible=True),              # overlay
            gr.update(visible=True),              # modal_m
            gr.update(choices=my_events, value=None),  # my_event_list
            "",                                   # del_msg
            *fav_buttons_update(favs),            # fav_btns(10)
            "",                                   # fav_msg
            gr.update(choices=fav_names, value=None),  # fav_del_sel
            ""                                    # fav_del_msg
        )

    fab.click(
        open_main_modal,
        None,
        [overlay, modal_m, my_event_list, del_msg] + fav_btns + [fav_msg, fav_del_sel, fav_del_msg],
    )

    def close_all():
        return gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    m_close.click(close_all, None, [overlay, modal_m, modal_s])

    def set_title_from_fav(btn_label):
        name = (btn_label or "").replace("⭐", "").strip()
        return gr.update(value=name)

    for b in fav_btns:
        b.click(fn=set_title_from_fav, inputs=b, outputs=t_in)

    # 즐겨찾기 추가 -> (메시지, 삭제드롭다운 갱신, 버튼 10개 갱신)
    fav_add_btn.click(
        fn=add_fav_only,
        inputs=[fav_new],
        outputs=[fav_msg, fav_del_sel] + fav_btns,
    )

    # 즐겨찾기 삭제 -> (메시지, 삭제드롭다운 갱신, 버튼 10개 갱신)
    fav_del_btn.click(
        fn=delete_fav,
        inputs=[fav_del_sel],
        outputs=[fav_del_msg, fav_del_sel] + fav_btns,
    )

    addr_btn.click(lambda: gr.update(visible=True), None, modal_s)
    s_close.click(lambda: gr.update(visible=False), None, modal_s)

    def search_k(q):
        if not q:
            return [], gr.update(choices=[])
        if not KAKAO_REST_API_KEY:
            return [], gr.update(choices=["KAKAO_REST_API_KEY 필요"], value=None)

        headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
        res = requests.get(
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            headers=headers,
            params={"query": q, "size": 5},
            timeout=15,
        )
        cands = []
        for d in res.json().get("documents", []):
            label = f"{d.get('place_name','')} ({d.get('address_name','')})"
            cands.append({"label": label, "name": d.get("place_name",""), "x": d.get("x"), "y": d.get("y")})
        return cands, gr.update(choices=[x["label"] for x in cands], value=None)

    q_btn.click(search_k, q_in, [search_state, q_res])

    def confirm_k(sel, cands):
        item = next((x for x in cands if x["label"] == sel), None)
        if not item:
            return "", {}, gr.update(visible=False)
        return item["label"], item, gr.update(visible=False)

    s_final.click(confirm_k, [q_res, search_state], [addr_v, selected_addr, modal_s])

    # 저장 후: 탐색 목록 + 지도 iframe 자동 갱신 + 모달 닫기 + 즐겨찾기 버튼 갱신
    def save_and_close(title, img, start, end, addr, req: gr.Request):
        _ = save_data(title, img, start, end, addr, req)
        html_list = get_list_html()
        favs = get_top_favs(10)
        return html_list, make_map_iframe(), gr.update(visible=False), gr.update(visible=False), *fav_buttons_update(favs)

    m_save.click(
        save_and_close,
        [t_in, img_in, s_in, e_in, selected_addr],
        [explore_html, map_html, overlay, modal_m] + fav_btns,
    )

    # 이벤트 삭제 후: 탐색+지도 같이 갱신
    del_btn.click(
        delete_my_event,
        [my_event_list],
        [del_msg, my_event_list],
    ).then(refresh_list_and_map, None, [explore_html, map_html])


# =========================================================
# 7) FastAPI + 로그인/회원가입 + 이메일 OTP
# =========================================================
app = FastAPI()

PUBLIC_PATHS = {"/", "/login", "/signup", "/logout", "/health", "/map", "/send_email_otp"}

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

@app.get("/health")
def health():
    return {"ok": True, "db": DB_PATH}

@app.get("/")
def root(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if get_user_by_token(token):
        return RedirectResponse("/app", status_code=303)
    return RedirectResponse("/login", status_code=303)

@app.get("/login")
def login_page():
    html_content = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"/>
  <title>오세요 - 로그인</title>
  <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
    body { font-family: Pretendard, system-ui; background:#fff; margin:0; padding:0;
      display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; }
    .container { width:100%; max-width:360px; padding:20px; text-align:center; }
    h1 { font-size:32px; font-weight:300; margin:0 0 10px 0; color:#333; }
    p.sub { font-size:15px; color:#888; margin-bottom:40px; }
    input { width:100%; padding:14px; margin-bottom:10px; border:1px solid #ddd; border-radius:6px; box-sizing:border-box; font-size:15px; }
    input:focus { outline:none; border-color:#333; }
    .login-btn { width:100%; padding:15px; border-radius:6px; border:none; background:#111; color:white;
      font-weight:700; font-size:16px; cursor:pointer; margin-top:10px; }
    .footer-link { margin-top:20px; font-size:13px; color:#888; }
    .footer-link a { color:#333; text-decoration:underline; }
  </style>
</head>
<body>
  <div class="container">
    <h1>오세요</h1>
    <p class="sub">열려 있는 순간을 나누세요</p>

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

@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    username = normalize_email(username)

    with db_conn() as con:
        row = con.execute("SELECT id, pw_hash FROM users WHERE username=?", (username,)).fetchone()

    if (not row) or (not check_pw(password, row[1])):
        return HTMLResponse("<script>alert('로그인 정보가 올바르지 않습니다.');location.href='/login';</script>")

    token = new_session(row[0])
    resp = RedirectResponse("/app", status_code=303)
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=SESSION_HOURS * 3600,
        samesite="lax",
    )
    return resp


@app.post("/send_email_otp")
async def send_email_otp(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    email = normalize_email(payload.get("email", ""))
    if not valid_email(email):
        return JSONResponse({"ok": False, "message": "이메일 형식을 확인해 주세요."}, status_code=400)

    code = create_email_otp(email)

    subject = "[오세요] 이메일 인증번호 안내"
    body = f"""오세요 이메일 인증번호는 아래와 같습니다.

인증번호: {code}
유효시간: {EMAIL_OTP_TTL_MINUTES}분

본인이 요청하지 않았다면 이 메일을 무시해 주세요.
"""

    sent = False
    err = None
    if smtp_ready():
        try:
            send_email(email, subject, body)
            sent = True
        except Exception as e:
            err = str(e)
            sent = False

    resp = {"ok": True, "message": "인증메일을 전송했습니다."}
    if not sent:
        resp["message"] = "SMTP 설정이 없어 메일 전송을 건너뛰었습니다. (개발모드)"
        resp["smtp_error"] = err

    if ALLOW_EMAIL_OTP_DEBUG == "1":
        resp["debug_code"] = code  # 운영에서는 0 권장

    return JSONResponse(resp)


@app.get("/signup")
def signup_page():
    # site key가 없으면(개발중) 캡차 대신 체크박스 UI만 보여줌
    use_real_captcha = bool(RECAPTCHA_SITE_KEY)

    html_content = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>오세요 - 회원가입</title>
  <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

    body {{
      font-family:Pretendard, system-ui;
      background:#fff; margin:0; padding:0;
      display:flex; justify-content:center; align-items:flex-start;
      min-height:100vh;
    }}

    .wrap {{
      width:100%;
      max-width: 420px;
      padding: 26px 18px 40px 18px;
      box-sizing:border-box;
    }}

    .brand {{
      display:flex; align-items:center; gap:10px;
      margin-bottom: 18px;
    }}
    .logo {{
      width:34px; height:34px; border-radius:8px;
      background:#111; display:inline-block;
    }}
    .brand h1 {{
      font-size:18px; margin:0; font-weight:800;
    }}

    h2 {{
      text-align:center;
      margin: 6px 0 16px 0;
      font-size:16px;
      font-weight:700;
      color:#111;
    }}

    .card {{
      border:1px solid #eee;
      border-radius: 14px;
      padding: 18px;
    }}

    .sns {{
      display:flex; justify-content:center; gap:12px;
      margin: 6px 0 14px 0;
    }}
    .sns a {{
      width:38px; height:38px; border-radius:50%;
      display:flex; align-items:center; justify-content:center;
      text-decoration:none;
      font-weight:800;
      border:1px solid #e8e8e8;
      color:#111;
      user-select:none;
    }}
    .sns .fb {{ background:#eef3ff; }}
    .sns .kk {{ background:#fff4a8; }}
    .sns .nv {{ background:#e7ffe7; }}

    .divider {{
      height:1px; background:#eee; margin: 14px 0;
    }}

    label {{
      display:block;
      font-size:12px;
      color:#111;
      font-weight:700;
      margin: 12px 0 6px 0;
    }}

    .hint {{
      font-size:12px;
      color:#777;
      margin-top:6px;
      line-height:1.35;
    }}

    input, select {{
      width:100%;
      padding:12px;
      border:1px solid #ddd;
      border-radius:8px;
      box-sizing:border-box;
      font-size:14px;
      background:#fff;
    }}
    input:focus, select:focus {{
      outline:none;
      border-color:#111;
    }}

    .row {{
      display:flex;
      gap:8px;
    }}
    .row > * {{ flex:1; }}

    .btn {{
      width:100%;
      padding:13px;
      background:#111;
      color:#fff;
      border:none;
      border-radius:10px;
      cursor:pointer;
      font-weight:800;
      margin-top: 14px;
      font-size:14px;
    }}

    .btn2 {{
      padding:12px;
      background:#f3f3f3;
      color:#111;
      border:1px solid #e6e6e6;
      border-radius:10px;
      cursor:pointer;
      font-weight:800;
      white-space:nowrap;
      font-size:13px;
    }}

    .msg {{
      margin-top:10px;
      font-size:13px;
      color:#444;
    }}
    .err {{ color:#c00; }}
    .ok {{ color:#0a7; }}

    .debug {{
      background:#fff7cc;
      padding:10px;
      border-radius:8px;
      font-size:13px;
      margin-top:10px;
      display:none;
    }}

    .terms {{
      border:1px solid #eee;
      border-radius: 10px;
      padding: 12px;
      margin-top: 12px;
      background: #fafafa;
    }}
    .terms .trow {{
      display:flex;
      align-items:flex-start;
      gap:10px;
      padding: 8px 2px;
      border-top: 1px solid #eee;
    }}
    .terms .trow:first-child {{
      border-top: none;
      padding-top: 2px;
    }}
    .terms input[type="checkbox"] {{
      width:18px;
      height:18px;
      margin-top:2px;
    }}
    .terms .ttext {{
      font-size:12.5px;
      color:#222;
      line-height:1.35;
    }}
    .terms .req {{
      color:#1e6bff;
      font-weight:800;
      margin-left:4px;
      font-size:12px;
    }}
    .terms .opt {{
      color:#999;
      font-weight:800;
      margin-left:4px;
      font-size:12px;
    }}

    .robotFallback {{
      border:1px solid #eee;
      border-radius: 10px;
      padding: 12px;
      margin-top: 12px;
      display:flex;
      align-items:center;
      justify-content:space-between;
      background:#fff;
    }}
    .robotFallback .left {{
      display:flex; align-items:center; gap:10px;
      font-size:13px; color:#111; font-weight:700;
    }}
    .robotFallback .badge {{
      font-size:11px;
      color:#777;
      border:1px solid #eee;
      padding:6px 8px;
      border-radius:8px;
    }}

    .footer {{
      text-align:center;
      margin-top: 12px;
      font-size:13px;
      color:#666;
    }}
    .footer a {{
      color:#111;
      text-decoration:underline;
      font-weight:700;
    }}

    /* 이메일 row에서 @ 표시 */
    .at {{
      display:flex;
      align-items:center;
      justify-content:center;
      width:40px;
      border:1px solid #ddd;
      border-radius:8px;
      font-weight:800;
      color:#666;
      background:#fafafa;
    }}
  </style>

  {"<script src='https://www.google.com/recaptcha/api.js' async defer></script>" if use_real_captcha else ""}
</head>
<body>
  <div class="wrap">
    <div class="brand">
      <span class="logo"></span>
      <h1>오세요</h1>
    </div>

    <h2>회원가입</h2>

    <div class="card">
      <div style="text-align:center; font-size:12px; color:#777; font-weight:700;">
        SNS계정으로 간편하게 회원가입
      </div>
      <div class="sns">
        <a class="fb" href="javascript:void(0)" title="Facebook">f</a>
        <a class="kk" href="javascript:void(0)" title="Kakao">톡</a>
        <a class="nv" href="javascript:void(0)" title="Naver">N</a>
      </div>

      <div class="divider"></div>

      <!-- 이메일: 아이디 / 도메인 완전 분리 -->
      <label>이메일</label>
      <div class="row">
        <input id="emailLocal" placeholder="아이디" autocomplete="email" />
        <div class="at">@</div>
        <select id="emailDomain" aria-label="domain">
          <option value="">도메인 선택</option>
          <option value="gmail.com">gmail.com</option>
          <option value="naver.com">naver.com</option>
          <option value="daum.net">daum.net</option>
          <option value="hanmail.net">hanmail.net</option>
          <option value="outlook.com">outlook.com</option>
          <option value="직접입력">직접입력</option>
        </select>
      </div>
      <input id="emailDomainCustom" placeholder="도메인 직접입력 (예: example.com)" style="display:none; margin-top:8px;" />

      <button class="btn2" type="button" style="width:100%; margin-top:8px;" onclick="sendOtp()">이메일 인증하기</button>

      <input id="otp" placeholder="인증번호 6자리" inputmode="numeric" />
      <div id="otpMsg" class="msg"></div>
      <div id="debugBox" class="debug"></div>

      <form method="post" action="/signup" onsubmit="return beforeSubmit();">
        <!-- 서버로 보내는 값: 기존 로직 유지 -->
        <input id="usernameHidden" name="username" type="hidden" />
        <input id="otpHidden" name="otp" type="hidden" />

        <label>비밀번호</label>
        <div class="hint">영문, 숫자를 포함한 8자 이상의 비밀번호를 입력해주세요.</div>
        <input id="pw" name="password" type="password" placeholder="비밀번호" required />

        <label>비밀번호 확인</label>
        <input id="pw2" type="password" placeholder="비밀번호 확인" required />
        <div id="pwMsg" class="msg"></div>

        <label>이름</label>
        <input name="name" placeholder="이름" required />

        <div class="row">
          <div>
            <label>성별</label>
            <select name="gender" required>
              <option value="">성별 선택</option>
              <option value="F">여성</option>
              <option value="M">남성</option>
              <option value="N">선택안함</option>
            </select>
          </div>
          <div>
            <label>생년월일</label>
            <input name="birth" type="date" required />
          </div>
        </div>

        <label>약관동의</label>
        <div class="terms">
          <div class="trow">
            <input id="t_all" type="checkbox" />
            <div class="ttext"><b>전체동의</b> <span style="color:#777;font-weight:600;">(선택항목에 대한 동의 포함)</span></div>
          </div>

          <div class="trow">
            <input id="t_age" type="checkbox" />
            <div class="ttext">만 14세 이상입니다<span class="req">(필수)</span></div>
          </div>

          <div class="trow">
            <input id="t_terms" type="checkbox" />
            <div class="ttext">이용약관<span class="req">(필수)</span></div>
          </div>

          <div class="trow">
            <input id="t_priv" type="checkbox" />
            <div class="ttext">개인정보 처리방침 동의<span class="req">(필수)</span></div>
          </div>

          <div class="trow">
            <input id="t_mkt" type="checkbox" />
            <div class="ttext">이벤트/프로모션 알림 동의<span class="opt">(선택)</span></div>
          </div>
        </div>

        <!-- ✅ 진짜 reCAPTCHA v2 -->
        {"<div style='margin-top:12px; display:flex; justify-content:center;'><div class='g-recaptcha' data-sitekey='" + RECAPTCHA_SITE_KEY + "'></div></div>" if use_real_captcha else ""}

        <!-- (개발중: 사이트키 없으면 UI 체크박스 대체) -->
        {"""
        <div class="robotFallback">
          <div class="left">
            <input id="robotFallback" type="checkbox" />
            <div>로봇이 아닙니다.</div>
          </div>
          <div class="badge">reCAPTCHA 미설정</div>
        </div>
        """ if not use_real_captcha else ""}

        <button class="btn" type="submit">회원가입하기</button>

        <div class="footer">
          이미 아이디가 있으신가요? <a href="/login">로그인</a>
        </div>
      </form>
    </div>
  </div>

<script>
  // 도메인 직접입력 토글
  document.getElementById("emailDomain").addEventListener("change", () => {{
    const v = document.getElementById("emailDomain").value;
    const custom = document.getElementById("emailDomainCustom");
    if (v === "직접입력") {{
      custom.style.display = "block";
    }} else {{
      custom.style.display = "none";
      custom.value = "";
    }}
  }});

  function buildEmailStrict() {{
    const local = (document.getElementById("emailLocal").value || "").trim();
    const domainSel = (document.getElementById("emailDomain").value || "").trim();
    const domainCustom = (document.getElementById("emailDomainCustom").value || "").trim();

    // 아이디 입력란에 @ 넣는 것 금지
    if (!local) return {{ ok:false, msg:"이메일 아이디를 입력해 주세요." }};
    if (local.includes("@")) return {{ ok:false, msg:"아이디 칸에는 @ 없이 입력해 주세요." }};

    let domain = domainSel;
    if (!domain) return {{ ok:false, msg:"도메인을 선택해 주세요." }};
    if (domainSel === "직접입력") {{
      if (!domainCustom) return {{ ok:false, msg:"도메인 직접입력을 입력해 주세요." }};
      domain = domainCustom;
    }}

    return {{ ok:true, email: local + "@" + domain }};
  }}

  // 전체동의 로직
  const all = document.getElementById("t_all");
  const items = ["t_age","t_terms","t_priv","t_mkt"].map(id => document.getElementById(id));
  all.addEventListener("change", () => {{
    items.forEach(ch => ch.checked = all.checked);
  }});
  items.forEach(ch => {{
    ch.addEventListener("change", () => {{
      all.checked = items.every(x => x.checked);
    }});
  }});

  async function sendOtp() {{
    const msgEl = document.getElementById("otpMsg");
    const dbg = document.getElementById("debugBox");
    msgEl.textContent = "";
    dbg.style.display = "none";
    dbg.textContent = "";

    const built = buildEmailStrict();
    if (!built.ok) {{
      msgEl.innerHTML = '<span class="err">' + built.msg + '</span>';
      return;
    }}

    try {{
      const r = await fetch("/send_email_otp", {{
        method: "POST",
        headers: {{"Content-Type":"application/json"}},
        body: JSON.stringify({{email: built.email}})
      }});
      const data = await r.json();
      if (!r.ok || !data.ok) {{
        msgEl.innerHTML = '<span class="err">' + (data.message || "전송 실패") + '</span>';
        return;
      }}
      msgEl.innerHTML = '<span class="ok">' + (data.message || "전송 완료") + '</span>';

      if (data.debug_code) {{
        dbg.style.display = "block";
        dbg.textContent = "개발모드 인증번호: " + data.debug_code + " (운영에서는 표시되지 않게 설정해야 함)";
      }}
    }} catch(e) {{
      msgEl.innerHTML = '<span class="err">요청 실패: 네트워크 오류</span>';
    }}
  }}

  function beforeSubmit() {{
    const built = buildEmailStrict();
    if (!built.ok) {{
      alert(built.msg);
      return false;
    }}

    const otp = (document.getElementById("otp").value || "").trim();
    document.getElementById("usernameHidden").value = built.email;
    document.getElementById("otpHidden").value = otp;

    // 비번 확인(프론트)
    const pw = document.getElementById("pw").value || "";
    const pw2 = document.getElementById("pw2").value || "";
    const pwMsg = document.getElementById("pwMsg");
    pwMsg.textContent = "";
    if (pw !== pw2) {{
      pwMsg.innerHTML = '<span class="err">비밀번호가 일치하지 않습니다.</span>';
      return false;
    }}

    // 약관 필수(프론트)
    const t_age = document.getElementById("t_age").checked;
    const t_terms = document.getElementById("t_terms").checked;
    const t_priv = document.getElementById("t_priv").checked;
    if (!(t_age && t_terms && t_priv)) {{
      alert("필수 약관에 동의해 주세요.");
      return false;
    }}

    // ✅ reCAPTCHA: 프론트에서 먼저 체크(실제 검증은 서버가 함)
    {"if (!grecaptcha || !grecaptcha.getResponse || grecaptcha.getResponse().length === 0) { alert('reCAPTCHA를 완료해 주세요.'); return false; }" if use_real_captcha else "if (!document.getElementById('robotFallback').checked) { alert('로봇이 아님을 확인해 주세요.'); return false; }"}

    return true;
  }}
</script>
</body>
</html>
    """
    return HTMLResponse(html_content)

@app.post("/signup")
def signup(
    request: Request,
    username: str = Form(...),
    otp: str = Form(...),
    password: str = Form(...),
    name: str = Form(...),
    gender: str = Form(...),
    birth: str = Form(...),
    g_recaptcha_response: str = Form("", alias="g-recaptcha-response"),
):
    email = normalize_email(username)

    # ✅ reCAPTCHA 서버 검증 (키 없으면 verify_recaptcha_v2()가 True 반환하도록 해둠)
    remoteip = None
    try:
        remoteip = request.client.host
    except Exception:
        remoteip = None

    if not verify_recaptcha_v2(g_recaptcha_response, remoteip=remoteip):
        return HTMLResponse("<script>alert('reCAPTCHA 인증에 실패했습니다. 다시 시도해 주세요.');history.back();</script>")

    if not verify_email_otp(email, otp):
        return HTMLResponse("<script>alert('이메일 인증번호가 올바르지 않거나 만료되었습니다.');history.back();</script>")

    uid = uuid.uuid4().hex
    try:
        with db_conn() as con:
            con.execute(
                """
                INSERT INTO users (id, username, pw_hash, name, gender, birth, email_verified_at, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    uid,
                    email,
                    make_pw_hash(password),
                    (name or "").strip(),
                    (gender or "").strip(),
                    (birth or "").strip(),
                    now_kst().isoformat(timespec="seconds"),
                    now_kst().isoformat(timespec="seconds"),
                ),
            )
            con.commit()
    except Exception:
        return HTMLResponse("<script>alert('이미 존재하는 이메일이거나 가입 정보가 올바르지 않습니다.');history.back();</script>")

    token = new_session(uid)
    resp = RedirectResponse("/app", status_code=303)
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        max_age=SESSION_HOURS * 3600,
        samesite="lax",
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp


# =========================================================
# 8) Map
# =========================================================
@app.get("/map")
def map_h():
    # ts 파라미터는 캐시 무효화 용도(사용만 하고 무시해도 됨)
    with db_conn() as con:
        rows = con.execute("SELECT title, photo, lat, lng, addr, start FROM events").fetchall()

    data = []
    for r in rows:
        data.append({"title": r[0], "photo": r[1], "lat": r[2], "lng": r[3], "addr": r[4], "start": r[5]})

    return HTMLResponse(f"""
<!doctype html>
<html>
<head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <style>
      body{{margin:0;}}
      .iw-img{{width:100%;height:100px;object-fit:cover;border-radius:6px;margin-top:6px;}}
      .iw-title{{font-weight:700;}}
      .iw-meta{{font-size:12px;margin-top:4px;color:#666;}}
    </style>
</head>
<body>
    <div id="m" style="width:100%;height:100vh;"></div>
    <script src="//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}"></script>
    <script>
        function esc(s) {{
          return String(s||"")
            .replaceAll("&","&amp;")
            .replaceAll("<","&lt;")
            .replaceAll(">","&gt;")
            .replaceAll('"',"&quot;")
            .replaceAll("'","&#039;");
        }}

        const map = new kakao.maps.Map(document.getElementById('m'), {{
          center: new kakao.maps.LatLng(36.019, 129.343),
          level: 7
        }});

        const data = {json.dumps(data)};
        let openIw = null;

        data.forEach(d => {{
            if (!d.lat || !d.lng) return;

            const marker = new kakao.maps.Marker({{
              position: new kakao.maps.LatLng(d.lat, d.lng),
              map: map
            }});

            const title = esc(d.title);
            const addr = esc(d.addr);
            const start = esc(d.start);

            const img = d.photo ? `<img class="iw-img" src="data:image/jpeg;base64,${{d.photo}}">` : "";
            const content = `
              <div style="padding:10px;width:220px;">
                <div class="iw-title">${{title}}</div>
                <div class="iw-meta">⏰ ${{start}}</div>
                <div class="iw-meta">📍 ${{addr}}</div>
                ${{img}}
              </div>
            `;

            const iw = new kakao.maps.InfoWindow({{
                content: content,
                removable: true
            }});

            kakao.maps.event.addListener(marker, 'click', () => {{
                if (openIw) openIw.close();
                iw.open(map, marker);
                openIw = iw;
            }});
        }});
    </script>
</body>
</html>
    """)


# =========================================================
# 9) Gradio 마운트
# =========================================================
app = gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


