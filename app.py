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

# ---- 휴대폰 OTP 설정 ----
OTP_TTL_MINUTES = 5
ALLOW_OTP_DEBUG = os.getenv("ALLOW_OTP_DEBUG", "1").strip()  # 1이면(개발용) 화면에 debug_code를 표시
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "").strip().lower()  # "twilio" 등

# Twilio 옵션(선택)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM = os.getenv("TWILIO_FROM", "").strip()


# =========================================================
# 1) 환경/DB
# =========================================================
def pick_db_path():
    # Render 같은 환경에서 디스크 마운트가 있으면 /var/data, 아니면 /tmp
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
        # 마이그레이션(예전 DB 대비)
        for col_sql in [
            "ALTER TABLE events ADD COLUMN user_id TEXT",
        ]:
            try:
                con.execute(col_sql)
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
        # favs 마이그레이션
        try:
            con.execute("ALTER TABLE favs ADD COLUMN updated_at TEXT")
        except Exception:
            pass

        # 유저 (회원가입 정보 확장)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE,
                pw_hash TEXT,
                name TEXT,
                gender TEXT,
                birth TEXT,
                phone TEXT,
                phone_verified_at TEXT,
                created_at TEXT
            );
            """
        )
        # users 마이그레이션
        for col_sql in [
            "ALTER TABLE users ADD COLUMN name TEXT",
            "ALTER TABLE users ADD COLUMN gender TEXT",
            "ALTER TABLE users ADD COLUMN birth TEXT",
            "ALTER TABLE users ADD COLUMN phone TEXT",
            "ALTER TABLE users ADD COLUMN phone_verified_at TEXT",
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

        # 휴대폰 OTP
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS phone_otps (
                phone TEXT PRIMARY KEY,
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


# =========================================================
# 3) OTP(휴대폰 인증) 유틸
# =========================================================
def normalize_phone(p: str) -> str:
    p = (p or "").strip()
    p = re.sub(r"[^0-9]", "", p)
    return p

def valid_phone(p: str) -> bool:
    # 한국 휴대폰 기준 대략 체크(10~11자리)
    return bool(re.fullmatch(r"\d{10,11}", p or ""))

def otp_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()

def create_otp(phone: str) -> str:
    code = f"{random.randint(0, 999999):06d}"
    exp = now_kst() + timedelta(minutes=OTP_TTL_MINUTES)
    with db_conn() as con:
        con.execute(
            """
            INSERT INTO phone_otps (phone, code_hash, expires_at, created_at)
            VALUES (?,?,?,?)
            ON CONFLICT(phone) DO UPDATE SET
                code_hash=excluded.code_hash,
                expires_at=excluded.expires_at,
                created_at=excluded.created_at
            """,
            (phone, otp_hash(code), exp.isoformat(), now_kst().isoformat()),
        )
        con.commit()
    return code

def verify_otp(phone: str, code: str) -> bool:
    phone = normalize_phone(phone)
    code = (code or "").strip()
    if not (valid_phone(phone) and re.fullmatch(r"\d{6}", code)):
        return False

    now_iso = now_kst().isoformat()
    with db_conn() as con:
        row = con.execute(
            "SELECT code_hash, expires_at FROM phone_otps WHERE phone=?",
            (phone,),
        ).fetchone()
    if not row:
        return False

    code_h, exp = row[0], row[1]
    if (exp or "") < now_iso:
        return False
    return otp_hash(code) == code_h

def send_sms_twilio(to_phone: str, message: str):
    # Twilio는 국가번호 포함 필요할 수 있음(+82...). 여기선 최소 구현만 제공
    # 운영에서는 전화번호 포맷을 국제표준으로 맞추는 것을 권장함.
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM):
        raise RuntimeError("Twilio env vars missing")
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    data = {
        "From": TWILIO_FROM,
        "To": to_phone,
        "Body": message,
    }
    r = requests.post(url, data=data, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=15)
    r.raise_for_status()
    return True


# =========================================================
# 4) CSS (요청하신 디자인 반영)
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
.main-title b { font-weight: 700; }
.logout-link {
    font-size: 13px;
    color: #888;
    text-decoration: none;
    margin-top: 4px;
}

/* 탭 스타일 조정 */
.tabs { border-bottom: 1px solid #eee; margin-top: 10px; }
button.selected {
    color: #111 !important;
    font-weight: 700 !important;
    border-bottom: 2px solid #111 !important;
}

/* FAB 버튼 - 오른쪽 하단 고정 */
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

/* 메인 모달 */
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

/* 이벤트 카드 */
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

/* 즐겨찾기 */
.fav-title { font-weight: 700; font-size: 14px; margin-top: 6px; }
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

    out = "<div style='padding:0 24px 80px 24px;'>"
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
        # 즐겨찾기 자동 증가
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

def add_fav_only(name: str, request: gr.Request):
    user = get_current_user(request)
    if not user:
        return "로그인이 필요합니다.", *([gr.update()] * 10)

    name = (name or "").strip()
    if not name:
        favs = get_top_favs(10)
        return "활동명을 입력해 주세요.", *fav_buttons_update(favs)

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
    return "✅ 즐겨찾기에 추가되었습니다.", *fav_buttons_update(favs)

def fav_buttons_update(favs):
    # 10개 버튼 업데이트(라벨/보임)
    updates = []
    for i in range(10):
        if i < len(favs):
            label = f"⭐ {favs[i]['name']}"
            updates.append(gr.update(value=label, visible=True))
        else:
            updates.append(gr.update(value="", visible=False))
    return updates


# =========================================================
# 6) Gradio UI
# =========================================================
now_dt = now_kst()
later_dt = now_dt + timedelta(hours=2)

with gr.Blocks(css=CSS, title="오세요") as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    gr.HTML("""
    <div class="header-row">
        <div class="main-title">지금, <b>열려 있습니다</b><br><span style="font-size:15px; color:#666; font-weight:400;">편하면 오셔도 됩니다</span></div>
        <a href="/logout" class="logout-link">로그아웃</a>
    </div>
    """)

    with gr.Tabs(elem_classes=["tabs"]):
        with gr.Tab("탐색"):
            # ✅ 중요: 초기값을 서버 시작 시점에 고정하지 않도록 비워두고,
            # demo.load에서 매번 DB를 읽어 채움.
            explore_html = gr.HTML()
            refresh_btn = gr.Button("🔄 목록 새로고침", variant="secondary", size="sm")

        with gr.Tab("지도"):
            gr.HTML('<iframe id="map_iframe" src="/map" style="width:100%;height:70vh;border:none;border-radius:16px;"></iframe>')

    # FAB
    with gr.Row(elem_classes=["fab-wrapper"]):
        fab = gr.Button("+")

    overlay = gr.HTML("<div class='overlay'></div>", visible=False)

    # ------------------- 메인 모달 -------------------
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_m:
        gr.HTML("<div class='modal-header'>새 이벤트 만들기</div>")

        with gr.Tabs(elem_classes=["modal-body"]):
            with gr.Tab("작성하기"):
                # 즐겨찾기(자주하는 활동)
                gr.Markdown("### ⭐ 자주하는 활동")
                gr.Markdown("<div class='small-muted'>버튼을 누르면 이벤트명에 바로 입력됩니다.</div>")

                fav_btns = []
                with gr.Row():
                    # 10개 고정 버튼(2열 그리드는 CSS로)
                    pass
                fav_wrap = gr.HTML("<div class='fav-grid'>", visible=True)
                # 버튼은 실제로 Row/Column에 넣으면 grid가 깨져서, 그냥 Column에 넣고 CSS class로 감싼 느낌을 재현
                # Gradio 구조상 완전한 div wrapping이 어려워서, 버튼 자체 스타일은 동일하게 맞춤.
                with gr.Column():
                    for _ in range(10):
                        b = gr.Button("", visible=False)
                        fav_btns.append(b)
                gr.HTML("</div>")

                with gr.Row():
                    fav_new = gr.Textbox(label="즐겨찾기 추가", placeholder="예: 30분 산책", lines=1)
                    fav_add_btn = gr.Button("추가", variant="secondary")
                fav_msg = gr.Markdown("")

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

    # ------------------- 장소 검색 서브 모달 -------------------
    with gr.Column(visible=False, elem_classes=["sub-modal", "main-modal"]) as modal_s:
        gr.HTML("<div class='modal-header'>장소 검색</div>")
        with gr.Column(elem_classes=["modal-body"]):
            q_in = gr.Textbox(label="검색어", placeholder="예: 영일대, 포항시청")
            q_btn = gr.Button("검색", variant="primary")
            q_res = gr.Radio(label="검색 결과", choices=[], interactive=True)
        with gr.Row(elem_classes=["modal-footer"]):
            s_close = gr.Button("취소", elem_classes=["btn-secondary"])
            s_final = gr.Button("확정", elem_classes=["btn-primary"])

    # ------- 이벤트 핸들러 -------

    # ✅ 페이지 로드시 항상 DB에서 목록을 다시 렌더(새로고침 문제 해결)
    demo.load(fn=get_list_html, inputs=None, outputs=explore_html)

    refresh_btn.click(fn=get_list_html, outputs=explore_html)

    # 모달 열기/닫기
    def open_main_modal(request: gr.Request):
        my_events = get_my_events(request)
        favs = get_top_favs(10)
        return (
            gr.update(visible=True),
            gr.update(visible=True),
            gr.update(choices=my_events, value=None),
            "",
            *fav_buttons_update(favs),
            ""
        )

    # outputs: overlay, modal_m, my_event_list, del_msg, fav_btns(10), fav_msg
    fab.click(
        open_main_modal,
        None,
        [overlay, modal_m, my_event_list, del_msg] + fav_btns + [fav_msg],
    )

    def close_all():
        return gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    m_close.click(close_all, None, [overlay, modal_m, modal_s])

    # 즐겨찾기 버튼 클릭 -> 이벤트명 채우기
    def set_title_from_fav(btn_label):
        # "⭐ name" 에서 name만 추출
        name = (btn_label or "").replace("⭐", "").strip()
        return gr.update(value=name)

    for b in fav_btns:
        b.click(fn=set_title_from_fav, inputs=b, outputs=t_in)

    # 즐겨찾기 추가
    fav_add_btn.click(
        fn=add_fav_only,
        inputs=[fav_new],
        outputs=[fav_msg] + fav_btns,
    )

    # 장소 검색 모달
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

    # 저장
    def save_and_close(title, img, start, end, addr, req: gr.Request):
        msg = save_data(title, img, start, end, addr, req)
        html_list = get_list_html()
        # 저장 후 즐겨찾기도 갱신
        favs = get_top_favs(10)
        # 메시지는 일단 console/리턴하지 않고, 리스트 갱신 + 모달 닫기만
        return (
            html_list,
            gr.update(visible=False),
            gr.update(visible=False),
            *fav_buttons_update(favs),
        )

    m_save.click(
        save_and_close,
        [t_in, img_in, s_in, e_in, selected_addr],
        [explore_html, overlay, modal_m] + fav_btns,
    )

    # 삭제
    del_btn.click(
        delete_my_event,
        [my_event_list],
        [del_msg, my_event_list],
    ).then(
        get_list_html, None, explore_html
    )


# =========================================================
# 7) FastAPI + 로그인/회원가입/OTP
# =========================================================
app = FastAPI()

PUBLIC_PATHS = {"/", "/login", "/signup", "/logout", "/health", "/map", "/send_otp"}

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
      background: #111; color: white; font-weight: 700; font-size: 16px; cursor: pointer; margin-top: 10px;
    }}

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

@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
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


# ---------- OTP 발송 ----------
@app.post("/send_otp")
async def send_otp(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    phone = normalize_phone(payload.get("phone", ""))
    if not valid_phone(phone):
        return JSONResponse({"ok": False, "message": "휴대폰 번호를 정확히 입력해 주세요(숫자만 10~11자리)."}, status_code=400)

    code = create_otp(phone)

    # 메시지
    msg = f"[오세요] 인증번호는 {code} 입니다. (유효시간 {OTP_TTL_MINUTES}분)"

    sent = False
    err = None
    if SMS_PROVIDER == "twilio":
        try:
            # Twilio는 보통 +82... 필요. 사용자가 010...으로 넣으면 운영에선 변환 로직을 추가하는 게 좋음.
            # 여기서는 입력 그대로 보냄(테스트용).
            send_sms_twilio(phone, msg)
            sent = True
        except Exception as e:
            err = str(e)
            sent = False

    # SMS 설정이 없으면 개발모드로 동작(코드 표시)
    resp = {"ok": True, "message": "인증번호를 전송했습니다."}
    if not sent and SMS_PROVIDER:
        resp["message"] = "SMS 전송 설정이 올바르지 않아 전송에 실패했습니다. (개발모드로 진행)"
        resp["provider_error"] = err

    if ALLOW_OTP_DEBUG == "1":
        resp["debug_code"] = code  # ✅ 개발용: 화면에 코드 표시(운영에서는 0 권장)

    return JSONResponse(resp)


@app.get("/signup")
def signup_page():
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
      font-family: Pretendard, system-ui;
      background: #fff; margin: 0; padding: 0;
      display: flex; justify-content: center; align-items: center;
      min-height: 100vh;
    }}
    .wrap {{
      width: 100%; max-width: 380px; padding: 20px;
    }}
    h2 {{ margin: 0 0 12px 0; font-size: 22px; }}
    .muted {{ color: #777; font-size: 13px; margin-bottom: 18px; }}
    input, select {{
      width: 100%; padding: 12px; margin: 8px 0;
      border: 1px solid #ddd; border-radius: 8px; box-sizing: border-box;
      font-size: 14px;
    }}
    input:focus, select:focus {{ outline: none; border-color: #111; }}
    .row {{ display: flex; gap: 8px; }}
    .row > * {{ flex: 1; }}
    .btn {{
      width: 100%; padding: 13px; background: #111; color: #fff;
      border: none; border-radius: 8px; cursor: pointer; font-weight: 700; margin-top: 10px;
    }}
    .btn2 {{
      padding: 12px; background: #f0f0f0; color: #111;
      border: none; border-radius: 8px; cursor: pointer; font-weight: 700;
      white-space: nowrap;
    }}
    .msg {{ margin-top: 10px; font-size: 13px; color: #444; }}
    .err {{ color: #c00; }}
    .ok {{ color: #0a7; }}
    a {{ color: #333; }}
    .debug {{
      background: #fff7cc; padding: 10px; border-radius: 8px; font-size: 13px; margin-top: 10px;
      display:none;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h2>회원가입</h2>
    <div class="muted">정보를 입력하고 휴대폰 인증을 완료해 주세요.</div>

    <div class="row">
      <input id="phone" name="phone" placeholder="휴대폰 번호(숫자만)" />
      <button class="btn2" type="button" onclick="sendOtp()">인증번호 받기</button>
    </div>
    <input id="otp" name="otp" placeholder="인증번호 6자리" />

    <div id="otpMsg" class="msg"></div>
    <div id="debugBox" class="debug"></div>

    <form method="post" action="/signup" onsubmit="return beforeSubmit();">
      <input name="username" placeholder="이메일(아이디)" required />
      <input name="password" type="password" placeholder="비밀번호" required />
      <input name="name" placeholder="이름" required />

      <div class="row">
        <select name="gender" required>
          <option value="">성별 선택</option>
          <option value="F">여성</option>
          <option value="M">남성</option>
          <option value="N">선택안함</option>
        </select>
        <input name="birth" type="date" required />
      </div>

      <!-- phone/otp를 폼에 같이 실어 보냄 -->
      <input type="hidden" id="phoneHidden" name="phone" />
      <input type="hidden" id="otpHidden" name="otp" />

      <button class="btn" type="submit">가입완료</button>
      <p style="margin-top:12px;font-size:13px;color:#666;">
        이미 계정이 있나요? <a href="/login">로그인</a>
      </p>
    </form>
  </div>

<script>
  async function sendOtp() {{
    const phone = document.getElementById("phone").value.trim();
    const msgEl = document.getElementById("otpMsg");
    const dbg = document.getElementById("debugBox");
    msgEl.textContent = "";
    dbg.style.display = "none";
    dbg.textContent = "";

    if (!phone) {{
      msgEl.innerHTML = '<span class="err">휴대폰 번호를 입력해 주세요.</span>';
      return;
    }}

    try {{
      const r = await fetch("/send_otp", {{
        method: "POST",
        headers: {{"Content-Type":"application/json"}},
        body: JSON.stringify({{phone}})
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
    // 폼 제출 전에 hidden에 phone/otp를 복사
    document.getElementById("phoneHidden").value = document.getElementById("phone").value.trim();
    document.getElementById("otpHidden").value = document.getElementById("otp").value.trim();
    return true;
  }}
</script>
</body>
</html>
    """
    return HTMLResponse(html_content)

@app.post("/signup")
def signup(
    username: str = Form(...),
    password: str = Form(...),
    name: str = Form(...),
    gender: str = Form(...),
    birth: str = Form(...),
    phone: str = Form(...),
    otp: str = Form(...),
):
    phone_n = normalize_phone(phone)

    # 휴대폰 인증 필수
    if not verify_otp(phone_n, otp):
        return HTMLResponse("<script>alert('휴대폰 인증번호가 올바르지 않거나 만료되었습니다.');history.back();</script>")

    uid = uuid.uuid4().hex
    try:
        with db_conn() as con:
            con.execute(
                """
                INSERT INTO users (id, username, pw_hash, name, gender, birth, phone, phone_verified_at, created_at)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    uid,
                    username,
                    make_pw_hash(password),
                    name.strip(),
                    (gender or "").strip(),
                    (birth or "").strip(),
                    phone_n,
                    now_kst().isoformat(timespec="seconds"),
                    now_kst().isoformat(timespec="seconds"),
                ),
            )
            con.commit()
    except Exception:
        return HTMLResponse("<script>alert('이미 존재하는 아이디이거나 가입 정보가 올바르지 않습니다.');history.back();</script>")

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
# 8) Map (카카오맵)
# =========================================================
@app.get("/map")
def map_h():
    with db_conn() as con:
        rows = con.execute("SELECT title, photo, lat, lng, addr, start FROM events").fetchall()

    data = []
    for r in rows:
        data.append(
            {"title": r[0], "photo": r[1], "lat": r[2], "lng": r[3], "addr": r[4], "start": r[5]}
        )

    # InfoWindow 하나만 열리도록(openInfowindow 전역 관리)
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
