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
            return os.path.join(d, "oseyo_final_email_v2.db")
        except Exception:
            continue
    return "/tmp/oseyo_final_email_v2.db"

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
                user_id TEXT,
                capacity INTEGER DEFAULT 10
            );
            """
        )
        # 마이그레이션(예전 DB 대비)
        for col_sql in [
            "ALTER TABLE events ADD COLUMN user_id TEXT",
            "ALTER TABLE events ADD COLUMN capacity INTEGER DEFAULT 10",
        ]:
            try:
                con.execute(col_sql)
            except Exception:
                pass

        # 즐겨찾기(활동명 Top10)
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
        for col_sql in [
            "ALTER TABLE users ADD COLUMN name TEXT",
            "ALTER TABLE users ADD COLUMN gender TEXT",
            "ALTER TABLE users ADD COLUMN birth TEXT",
            "ALTER TABLE users bati ADD COLUMN email_verified_at TEXT",  # typo safeguard (won't run)
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

        # 이벤트 참여(1인 1이벤트 참여 제한은 로직으로 강제)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS event_participants (
                event_id TEXT,
                user_id TEXT,
                joined_at TEXT,
                PRIMARY KEY (event_id, user_id)
            );
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_ep_user ON event_participants(user_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_ep_event ON event_participants(event_id);")

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

def get_current_user_fastapi(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    return get_user_by_token(token)


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
# 4) 날짜/표시 유틸
# =========================================================
def parse_dt(s: str):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=KST)
        except Exception:
            continue
    return None

def fmt_start(s: str) -> str:
    dt = parse_dt(s)
    if not dt:
        return (s or "").strip()
    return dt.strftime("%m월 %d일 %H:%M")

def fmt_remaining(end_s: str) -> str:
    dt = parse_dt(end_s)
    if not dt:
        return ""
    diff = int((dt - now_kst()).total_seconds())
    if diff <= 0:
        return "종료"
    days = diff // 86400
    diff %= 86400
    hours = diff // 3600
    diff %= 3600
    mins = diff // 60

    parts = []
    if days > 0:
        parts.append(f"{days}일")
    if hours > 0 and len(parts) < 2:
        parts.append(f"{hours}시간")
    if mins > 0 and len(parts) < 2:
        parts.append(f"{mins}분")
    if not parts:
        parts = ["1분"]
    return "· " + " ".join(parts) + " 남음"


# =========================================================
# 5) 참여 로직 (1인 1이벤트 제한)
# =========================================================
def ensure_single_participation(user_id: str):
    """혹시 데이터가 꼬였을 때(중복 참여) 가장 최근 1개만 남기고 정리한다."""
    with db_conn() as con:
        rows = con.execute(
            "SELECT event_id, joined_at FROM event_participants WHERE user_id=? ORDER BY joined_at DESC",
            (user_id,),
        ).fetchall()
        if len(rows) <= 1:
            return rows[0][0] if rows else None
        keep = rows[0][0]
        for r in rows[1:]:
            con.execute("DELETE FROM event_participants WHERE user_id=? AND event_id=?", (user_id, r[0]))
        con.commit()
        return keep

def get_joined_event_id(user_id: str):
    return ensure_single_participation(user_id)

def get_event_capacity(event_id: str) -> int:
    with db_conn() as con:
        row = con.execute("SELECT COALESCE(capacity,10) FROM events WHERE id=?", (event_id,)).fetchone()
    if not row:
        return 0
    try:
        return int(row[0] or 0)
    except Exception:
        return 0

def get_event_participants_count(event_id: str) -> int:
    with db_conn() as con:
        row = con.execute("SELECT COUNT(*) FROM event_participants WHERE event_id=?", (event_id,)).fetchone()
    return int(row[0] or 0)

def is_user_joined(event_id: str, user_id: str) -> bool:
    with db_conn() as con:
        row = con.execute(
            "SELECT 1 FROM event_participants WHERE event_id=? AND user_id=?",
            (event_id, user_id),
        ).fetchone()
    return bool(row)

def join_event(event_id: str, user_id: str):
    event_id = (event_id or "").strip()
    if not event_id:
        return False, "이벤트를 찾을 수 없습니다."

    # 다른 이벤트 참여중인지 확인
    joined = get_joined_event_id(user_id)
    if joined and joined != event_id:
        return False, "이미 참여중인 활동이 있습니다. 먼저 빠지기를 눌러주세요."

    # 정원 체크
    cap = get_event_capacity(event_id)
    if cap <= 0:
        return False, "이벤트를 찾을 수 없습니다."

    cnt = get_event_participants_count(event_id)
    already = is_user_joined(event_id, user_id)
    if (not already) and cnt >= cap:
        return False, "정원이 가득 찼습니다."

    with db_conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO event_participants (event_id, user_id, joined_at) VALUES (?,?,?)",
            (event_id, user_id, now_kst().isoformat(timespec="seconds")),
        )
        con.commit()

    ensure_single_participation(user_id)
    return True, "참여했습니다."

def leave_event(event_id: str, user_id: str):
    event_id = (event_id or "").strip()
    if not event_id:
        return False, "이벤트를 찾을 수 없습니다."
    with db_conn() as con:
        con.execute("DELETE FROM event_participants WHERE event_id=? AND user_id=?", (event_id, user_id))
        con.commit()
    ensure_single_participation(user_id)
    return True, "빠졌습니다."


# =========================================================
# 6) 즐겨찾기 로직
# =========================================================
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
    if not user:
        return "로그인이 필요합니다.", *fav_buttons_update(favs), gr.update(choices=[f["name"] for f in favs], value=None)

    name = (name or "").strip()
    if not name:
        return "활동명을 입력해 주세요.", *fav_buttons_update(favs), gr.update(choices=[f["name"] for f in favs], value=None)

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
    return "✅ 즐겨찾기에 추가되었습니다.", *fav_buttons_update(favs), gr.update(choices=[f["name"] for f in favs], value=None)

def delete_fav_only(name: str, request: gr.Request):
    user = get_current_user(request)
    if not user:
        favs = get_top_favs(10)
        return "로그인이 필요합니다.", *fav_buttons_update(favs), gr.update(choices=[f["name"] for f in favs], value=None)

    name = (name or "").strip()
    if not name:
        favs = get_top_favs(10)
        return "삭제할 즐겨찾기를 선택해 주세요.", *fav_buttons_update(favs), gr.update(choices=[f["name"] for f in favs], value=None)

    with db_conn() as con:
        con.execute("DELETE FROM favs WHERE name=?", (name,))
        con.commit()

    favs = get_top_favs(10)
    return "✅ 즐겨찾기를 삭제했습니다.", *fav_buttons_update(favs), gr.update(choices=[f["name"] for f in favs], value=None)


# =========================================================
# 7) 이벤트 목록/카드 HTML (탐색)
# =========================================================
def fetch_events_enriched(user_id: str | None):
    uid = user_id or ""
    with db_conn() as con:
        rows = con.execute(
            """
            SELECT
              e.id, e.title, e.photo, e.start, e.end, e.addr, e.lat, e.lng, e.created_at,
              COALESCE(e.capacity, 10) AS capacity,
              COALESCE(p.cnt, 0) AS participants,
              CASE WHEN me.user_id IS NULL THEN 0 ELSE 1 END AS joined
            FROM events e
            LEFT JOIN (
              SELECT event_id, COUNT(*) AS cnt
              FROM event_participants
              GROUP BY event_id
            ) p ON p.event_id = e.id
            LEFT JOIN event_participants me
              ON me.event_id = e.id AND me.user_id = ?
            ORDER BY e.created_at DESC
            """,
            (uid,),
        ).fetchall()

    out = []
    for r in rows:
        out.append({
            "id": r[0],
            "title": r[1] or "",
            "photo": r[2] or "",
            "start": r[3] or "",
            "end": r[4] or "",
            "addr": r[5] or "",
            "lat": float(r[6] or 0.0),
            "lng": float(r[7] or 0.0),
            "created_at": r[8] or "",
            "capacity": int(r[9] or 10),
            "participants": int(r[10] or 0),
            "joined": bool(r[11]),
        })
    return out

def render_event_card(d: dict, show_join_btn: bool = True):
    title = html.escape(d.get("title") or "")
    addr = html.escape(d.get("addr") or "장소 미정")
    start_disp = html.escape(fmt_start(d.get("start") or ""))
    remain = html.escape(fmt_remaining(d.get("end") or ""))
    photo = d.get("photo") or ""
    img_html = f"<img class='event-photo' src='data:image/jpeg;base64,{photo}' />" if photo else \
        "<div class='event-photo noimg'>NO IMAGE</div>"

    cap = int(d.get("capacity") or 10)
    participants = int(d.get("participants") or 0)
    joined = bool(d.get("joined"))

    full = (participants >= cap)
    action = "leave" if joined else "join"
    btn_label = "빠지기" if joined else "참여하기"
    disabled = (not joined) and full

    btn_html = ""
    if show_join_btn:
        btn_html = f"""
        <button class="join-btn {'secondary' if joined else 'primary'}"
                data-oseyo-action="{action}"
                data-eid="{html.escape(d.get('id') or '')}"
                {'disabled' if disabled else ''}>
            {btn_label}
        </button>
        """

    return f"""
      <div class='event-card' data-eid='{html.escape(d.get('id') or '')}'>
        {img_html}
        <div class='event-info'>
          <div class='event-title'>{title}</div>
          <div class='event-meta'>⏰ {start_disp} <span class='remain'>{remain}</span></div>
          <div class='event-meta'>📍 {addr}</div>
          <div class='event-actions'>
            <div class='pill'>👥 {participants} / {cap}</div>
            {btn_html}
          </div>
        </div>
      </div>
    """

def build_events_html(user_id: str | None):
    events = fetch_events_enriched(user_id)
    if not events:
        return "<div class='empty'>등록된 이벤트가 없습니다.<br>오른쪽 아래 버튼을 눌러 시작해보세요.</div>"
    cards = "\n".join(render_event_card(d, show_join_btn=True) for d in events)
    return f"<div class='page-wrap'>{cards}</div>"

def build_joined_html(user_id: str | None):
    if not user_id:
        return ""
    joined_id = get_joined_event_id(user_id)
    if not joined_id:
        return ""
    events = fetch_events_enriched(user_id)
    joined_event = next((e for e in events if e["id"] == joined_id), None)
    if not joined_event:
        return ""
    card = render_event_card(joined_event, show_join_btn=True)
    return f"""
    <div class="page-wrap joined-wrap">
      <div class="joined-title">참여중인 활동</div>
      {card}
    </div>
    """


# =========================================================
# 8) 이벤트 저장/삭제(내 글)
# =========================================================
def save_data(title, img, start, end, capacity, addr_obj, request: gr.Request):
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
            im.thumbnail((900, 900))
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

    try:
        cap = int(capacity or 10)
    except Exception:
        cap = 10
    if cap < 1:
        cap = 1
    if cap > 999:
        cap = 999

    eid = uuid.uuid4().hex[:8]
    with db_conn() as con:
        con.execute(
            """
            INSERT INTO events (id, title, photo, start, end, addr, lat, lng, created_at, user_id, capacity)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                eid,
                title,
                pic_b64,
                start or "",
                end or "",
                addr_name,
                lat,
                lng,
                now_kst().isoformat(timespec="seconds"),
                user["id"],
                cap,
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
        # 참여도 같이 삭제(고아 방지)
        con.execute("DELETE FROM event_participants WHERE event_id = ?", (event_id,))
        con.commit()

    new_list = get_my_events(request)
    return "✅ 삭제되었습니다.", gr.update(choices=new_list, value=None)


# =========================================================
# 9) CSS
# =========================================================
CSS = """
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

html, body {
  margin: 0 !important; padding: 0 !important;
  font-family: Pretendard, -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif !important;
  background-color: #ffffff !important;
}
.gradio-container { max-width: 100% !important; padding: 0 !important; margin: 0 !important;}

.page-wrap { max-width: 560px; margin: 0 auto; }
@media (max-width: 600px){
  .page-wrap { max-width: 100%; margin: 0; }
}

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

.joined-wrap { padding: 10px 24px 0 24px; }
.joined-title { font-size: 15px; font-weight: 800; margin: 10px 0 12px 0; color:#111; }

.event-card { margin: 0 24px 22px 24px; cursor: default; }
@media (min-width: 601px){
  .event-card { margin-left: 0; margin-right: 0; }
}

.event-photo {
  width: 100%;
  aspect-ratio: 16/9;
  object-fit: cover;
  border-radius: 16px;
  margin-bottom: 12px;
  background-color: #f0f0f0;
  border: 1px solid #eee;
  max-height: 260px;   /* ✅ 웹에서 너무 길게 보이는 것 방지 */
}
@media (max-width: 600px){
  .event-photo{ max-height: none; }
}
.event-photo.noimg{
  display:flex;align-items:center;justify-content:center;color:#bbb;
}

.event-info { padding: 0 4px; }
.event-title {
  font-size: 18px;
  font-weight: 800;
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
  gap: 6px;
  flex-wrap: wrap;
}
.event-meta .remain{
  color:#111;
  font-weight:700;
}

.event-actions{
  margin-top: 10px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:10px;
  position:relative;
  z-index: 5;
}

.pill{
  display:inline-flex;
  align-items:center;
  gap:6px;
  padding: 8px 10px;
  border-radius: 999px;
  border:1px solid #eee;
  background:#fafafa;
  color:#333;
  font-size: 13px;
  font-weight: 700;
}

.join-btn{
  padding: 10px 12px;
  border-radius: 12px;
  border: none;
  font-weight: 800;
  font-size: 13px;
  cursor:pointer;
  min-width: 92px;
}
.join-btn.primary{ background:#111; color:#fff; }
.join-btn.secondary{ background:#f0f0f0; color:#111; }
.join-btn:disabled{ opacity:0.5; cursor:not-allowed; }

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
.empty{ text-align:center; padding:100px 20px; color:#999; }
"""


# =========================================================
# 10) Gradio UI
# =========================================================
now_dt = now_kst()
later_dt = now_dt + timedelta(hours=2)

with gr.Blocks(css=CSS, title="오세요") as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    gr.HTML("""
    <div class="page-wrap">
      <div class="header-row">
          <div class="main-title">지금, <b>열려 있습니다</b><br><span style="font-size:15px; color:#666; font-weight:400;">편하면 오셔도 됩니다</span></div>
          <a href="/logout" class="logout-link">로그아웃</a>
      </div>
    </div>
    """)

    with gr.Tabs(elem_classes=["tabs"]):
        with gr.Tab("탐색"):
            joined_explore = gr.HTML("<div id='oseyo_joined_explore_root'></div>")
            explore_html = gr.HTML("<div id='oseyo_explore_root' class='page-wrap'></div>")
            refresh_btn = gr.Button("🔄 목록 새로고침", variant="secondary", size="sm")
        with gr.Tab("지도"):
            joined_map = gr.HTML("<div id='oseyo_joined_map_root'></div>")
            gr.HTML('<iframe id="map_iframe" src="/map" style="width:100%;height:70vh;border:none;border-radius:16px;"></iframe>')

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

                with gr.Row():
                    fav_del_sel = gr.Dropdown(label="즐겨찾기 삭제", choices=[], interactive=True)
                    fav_del_btn = gr.Button("삭제", variant="stop")

                fav_msg = gr.Markdown("")
                gr.Markdown("---")

                t_in = gr.Textbox(label="이벤트명", placeholder="예: 30분 산책, 조용히 책 읽기", lines=1)

                with gr.Accordion("사진 추가 (선택)", open=False):
                    img_in = gr.Image(label="사진", type="numpy", height=200)

                with gr.Row():
                    s_in = gr.Textbox(label="시작", value=now_dt.strftime("%Y-%m-%d %H:%M"))
                    e_in = gr.Textbox(label="종료", value=later_dt.strftime("%Y-%m-%d %H:%M"))

                cap_in = gr.Number(label="정원", value=10, precision=0)

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

    # ✅ 초기 렌더(서버)
    def load_all(request: gr.Request):
        user = get_current_user(request)
        uid = user["id"] if user else None
        return (
            build_joined_html(uid),
            f"<div id='oseyo_explore_root'>{build_events_html(uid)}</div>",
        )

    demo.load(fn=load_all, inputs=None, outputs=[joined_explore, explore_html])
    refresh_btn.click(fn=load_all, outputs=[joined_explore, explore_html])

    def open_main_modal(request: gr.Request):
        my_events = get_my_events(request)
        favs = get_top_favs(10)
        fav_names = [f["name"] for f in favs]
        return (
            gr.update(visible=True),
            gr.update(visible=True),
            gr.update(choices=my_events, value=None),
            "",
            *fav_buttons_update(favs),
            "",
            gr.update(choices=fav_names, value=None),
        )

    fab.click(
        open_main_modal,
        None,
        [overlay, modal_m, my_event_list, del_msg] + fav_btns + [fav_msg, fav_del_sel],
    )

    def close_all():
        return gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    m_close.click(close_all, None, [overlay, modal_m, modal_s])

    def set_title_from_fav(btn_label):
        name = (btn_label or "").replace("⭐", "").strip()
        return gr.update(value=name)

    for b in fav_btns:
        b.click(fn=set_title_from_fav, inputs=b, outputs=t_in)

    fav_add_btn.click(
        fn=add_fav_only,
        inputs=[fav_new],
        outputs=[fav_msg] + fav_btns + [fav_del_sel],
    )

    fav_del_btn.click(
        fn=delete_fav_only,
        inputs=[fav_del_sel],
        outputs=[fav_msg] + fav_btns + [fav_del_sel],
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

    def save_and_close(title, img, start, end, cap, addr, req: gr.Request):
        _ = save_data(title, img, start, end, cap, addr, req)
        user = get_current_user(req)
        uid = user["id"] if user else None
        return (
            build_joined_html(uid),
            f"<div id='oseyo_explore_root'>{build_events_html(uid)}</div>",
            gr.update(visible=False),
            gr.update(visible=False),
            *fav_buttons_update(get_top_favs(10)),
        )

    m_save.click(
        save_and_close,
        [t_in, img_in, s_in, e_in, cap_in, selected_addr],
        [joined_explore, explore_html, overlay, modal_m] + fav_btns,
    )

    del_btn.click(
        delete_my_event,
        [my_event_list],
        [del_msg, my_event_list],
    ).then(load_all, None, [joined_explore, explore_html])

    # ✅ 전역 JS (탐색에서 참여/빠지기 즉시 동작 + 탭 동기화)
    gr.HTML("""
<script>
(function(){
  function toast(msg){
    let t = document.getElementById("oseyo_toast");
    if(!t){
      t = document.createElement("div");
      t.id="oseyo_toast";
      t.style.position="fixed";
      t.style.left="50%";
      t.style.bottom="24px";
      t.style.transform="translateX(-50%)";
      t.style.background="rgba(17,17,17,0.92)";
      t.style.color="#fff";
      t.style.padding="10px 14px";
      t.style.borderRadius="12px";
      t.style.fontSize="13px";
      t.style.zIndex="999999";
      t.style.maxWidth="92vw";
      t.style.display="none";
      document.body.appendChild(t);
    }
    t.textContent = msg || "";
    t.style.display="block";
    clearTimeout(window.__oseyo_toast_timer);
    window.__oseyo_toast_timer = setTimeout(()=>{ t.style.display="none"; }, 1800);
  }

  async function refreshExplore(){
    try{
      const r = await fetch("/api/events_html", {credentials:"include"});
      const data = await r.json();
      const root = document.getElementById("oseyo_explore_root");
      if(root && data && typeof data.html === "string"){
        root.innerHTML = data.html;
      }
    }catch(e){}
  }

  async function refreshJoined(){
    try{
      const r = await fetch("/api/joined_html", {credentials:"include"});
      const data = await r.json();
      const h = (data && typeof data.html === "string") ? data.html : "";
      const a = document.getElementById("oseyo_joined_explore_root");
      const b = document.getElementById("oseyo_joined_map_root");
      if(a) a.innerHTML = h;
      if(b) b.innerHTML = h;
    }catch(e){}
  }

  async function refreshAll(){
    await refreshJoined();
    await refreshExplore();
  }

  function notifyMapRefresh(){
    const iframe = document.getElementById("map_iframe");
    if(!iframe) return;
    try{ iframe.contentWindow.postMessage({type:"oseyo_refresh"}, "*"); }catch(e){}
  }

  function findOseyoButton(ev){
    const path = (ev && typeof ev.composedPath === "function") ? ev.composedPath() : null;
    if(path && path.length){
      for(const el of path){
        if(el && el.tagName === "BUTTON" && el.dataset && el.dataset.oseyoAction && el.dataset.eid){
          return el;
        }
      }
    }
    const t = ev.target;
    if(t && t.closest){
      return t.closest("button[data-oseyo-action][data-eid]");
    }
    return null;
  }

  window.addEventListener("message", (ev) => {
    if(!ev || !ev.data) return;
    if(ev.data.type === "oseyo_changed"){
      refreshAll();
    }
  });

  // ✅ 탐색탭에서 클릭 안 먹는 문제 해결: pointerup + capture + composedPath
  window.addEventListener("pointerup", async (ev) => {
    const btn = findOseyoButton(ev);
    if(!btn) return;

    const action = btn.dataset.oseyoAction;
    const eid = btn.dataset.eid;
    if(!action || !eid) return;

    ev.preventDefault();
    ev.stopPropagation();

    btn.disabled = true;

    try{
      const r = await fetch(action === "join" ? "/api/join" : "/api/leave", {
        method:"POST",
        headers: {"Content-Type":"application/json"},
        credentials:"include",
        body: JSON.stringify({event_id: eid})
      });
      const data = await r.json();

      if(!r.ok || !data.ok){
        toast((data && data.message) ? data.message : "요청 실패");
      }else{
        await refreshAll();     // ✅ 탐색/상단 즉시 갱신
        notifyMapRefresh();     // ✅ 지도도 즉시 갱신
      }
    }catch(e){
      toast("네트워크 오류");
    }finally{
      btn.disabled = false;
    }
  }, true);

  window.addEventListener("load", () => {
    refreshAll();
  });

  window.oseyoRefreshAll = refreshAll;
})();
</script>
""")


# =========================================================
# 11) FastAPI + 로그인/회원가입 + API
# =========================================================
app = FastAPI()

PUBLIC_PATHS = {
    "/", "/login", "/signup", "/logout", "/health",
    "/map", "/send_email_otp",
    "/api/events_json", "/api/events_html", "/api/joined_html",
}

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
    # ✅ 참고 UI(이미지)에서 '없는 부분'만 추가: 도메인 분리/비번확인/약관/봇체크/SNS 버튼(placeholder)
    html_content = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>오세요 - 회원가입</title>
  <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
    body {{ font-family:Pretendard, system-ui; background:#fff; margin:0; padding:0;
      display:flex; justify-content:center; align-items:flex-start; min-height:100vh; }}
    .wrap {{ width:100%; max-width:420px; padding:24px 20px 40px 20px; }}
    .brand {{ font-size:18px; font-weight:900; margin-bottom:10px; }}
    h2 {{ margin:10px 0 8px 0; font-size:22px; text-align:center; }}
    .muted {{ color:#777; font-size:13px; margin-bottom:18px; text-align:center; }}
    input, select {{ width:100%; padding:12px; margin:8px 0; border:1px solid #ddd; border-radius:10px; box-sizing:border-box; font-size:14px; }}
    input:focus, select:focus {{ outline:none; border-color:#111; }}
    .row {{ display:flex; gap:8px; align-items:center; }}
    .row > * {{ flex:1; }}
    .btn {{ width:100%; padding:13px; background:#111; color:#fff; border:none; border-radius:10px; cursor:pointer; font-weight:800; margin-top:12px; }}
    .btn2 {{ padding:12px; background:#f0f0f0; color:#111; border:none; border-radius:10px; cursor:pointer; font-weight:800; white-space:nowrap; width:100%; margin-top:6px; }}
    .msg {{ margin-top:10px; font-size:13px; color:#444; }}
    .err {{ color:#c00; }}
    .ok {{ color:#0a7; }}
    a {{ color:#333; }}
    .debug {{ background:#fff7cc; padding:10px; border-radius:10px; font-size:13px; margin-top:10px; display:none; }}
    .sns {{ display:flex; gap:12px; justify-content:center; margin:14px 0 18px 0; }}
    .sns .circle {{ width:44px; height:44px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-weight:900; color:#fff; cursor:pointer; }}
    .c-fb {{ background:#1877F2; }} .c-kk {{ background:#FEE500; color:#111 !important; }} .c-nv {{ background:#03C75A; }}
    .section-title {{ font-size:13px; font-weight:900; margin-top:10px; color:#111; }}
    .agree-box {{ border:1px solid #eee; border-radius:12px; padding:12px; margin-top:10px; }}
    .agree-row {{ display:flex; align-items:center; justify-content:space-between; gap:8px; padding:8px 4px; border-top:1px solid #f3f3f3; }}
    .agree-row:first-child{{ border-top:none; }}
    .agree-row label{{ display:flex; align-items:center; gap:8px; font-size:13px; color:#333; }}
    .agree-row input{{ width:18px; height:18px; margin:0; }}
    .bot {{ display:flex; align-items:center; gap:10px; border:1px solid #eee; border-radius:12px; padding:12px; margin-top:12px; }}
    .bot input{{ width:18px; height:18px; margin:0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="brand">오세요</div>
    <h2>회원가입</h2>
    <div class="muted">이메일 인증 후 가입을 완료해 주세요.</div>

    <div class="sns" title="SNS 가입(예시 UI, 실제 연동은 추후)">
      <div class="circle c-fb">f</div>
      <div class="circle c-kk">K</div>
      <div class="circle c-nv">N</div>
    </div>

    <div class="section-title">이메일</div>

    <div class="row">
      <input id="email_id" placeholder="아이디" />
      <div style="flex:0 0 auto; font-weight:900; color:#777;">@</div>
      <select id="email_domain_sel">
        <option value="">선택해주세요</option>
        <option value="gmail.com">gmail.com</option>
        <option value="naver.com">naver.com</option>
        <option value="daum.net">daum.net</option>
        <option value="hanmail.net">hanmail.net</option>
        <option value="kakao.com">kakao.com</option>
        <option value="_custom">직접입력</option>
      </select>
    </div>
    <input id="email_domain_custom" placeholder="도메인 직접입력 (예: company.com)" style="display:none;" />
    <button class="btn2" type="button" onclick="sendOtp()">이메일 인증하기</button>

    <input id="otp" placeholder="인증번호 6자리" />
    <div id="otpMsg" class="msg"></div>
    <div id="debugBox" class="debug"></div>

    <form method="post" action="/signup" onsubmit="return beforeSubmit();">
      <input id="usernameHidden" name="username" type="hidden" />
      <input id="otpHidden" name="otp" type="hidden" />

      <div class="section-title">비밀번호</div>
      <input id="pw" name="password" type="password" placeholder="비밀번호" required />
      <input id="pw2" type="password" placeholder="비밀번호 확인" required />

      <div class="section-title">기본정보</div>
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

      <div class="section-title">약관동의</div>
      <div class="agree-box">
        <div class="agree-row">
          <label><input id="agree_all" type="checkbox" />전체동의</label>
        </div>
        <div class="agree-row">
          <label><input class="agree_req" type="checkbox" />만 14세 이상입니다(필수)</label>
        </div>
        <div class="agree-row">
          <label><input class="agree_req" type="checkbox" />이용약관 동의(필수)</label>
          <span style="color:#999;">›</span>
        </div>
        <div class="agree-row">
          <label><input class="agree_req" type="checkbox" />개인정보 처리방침 동의(필수)</label>
          <span style="color:#999;">›</span>
        </div>
        <div class="agree-row">
          <label><input type="checkbox" />마케팅 수신 동의(선택)</label>
        </div>
      </div>

      <div class="bot">
        <input id="botcheck" type="checkbox" />
        <div style="font-size:13px;color:#333;font-weight:800;">로봇이 아닙니다.</div>
        <div style="margin-left:auto;color:#aaa;font-size:12px;">(체크박스)</div>
      </div>

      <button class="btn" id="submitBtn" type="submit">회원가입하기</button>
      <p style="margin-top:12px;font-size:13px;color:#666;text-align:center;">
        이미 아이디가 있으신가요? <a href="/login">로그인</a>
      </p>
    </form>
  </div>

<script>
  const sel = document.getElementById("email_domain_sel");
  const custom = document.getElementById("email_domain_custom");
  sel.addEventListener("change", () => {
    if(sel.value === "_custom") {
      custom.style.display = "block";
    } else {
      custom.style.display = "none";
      custom.value = "";
    }
  });

  function buildEmail() {
    const id = document.getElementById("email_id").value.trim();
    const domSel = sel.value;
    const dom = (domSel === "_custom") ? custom.value.trim() : domSel;
    if(!id || !dom) return "";
    return (id + "@" + dom).toLowerCase();
  }

  async function sendOtp() {
    const email = buildEmail();
    const msgEl = document.getElementById("otpMsg");
    const dbg = document.getElementById("debugBox");
    msgEl.textContent = "";
    dbg.style.display = "none";
    dbg.textContent = "";

    if (!email) {
      msgEl.innerHTML = '<span class="err">이메일을 완성해 주세요.</span>';
      return;
    }

    try {
      const r = await fetch("/send_email_otp", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({email})
      });
      const data = await r.json();
      if (!r.ok || !data.ok) {
        msgEl.innerHTML = '<span class="err">' + (data.message || "전송 실패") + '</span>';
        return;
      }
      msgEl.innerHTML = '<span class="ok">' + (data.message || "전송 완료") + '</span>';

      if (data.debug_code) {
        dbg.style.display = "block";
        dbg.textContent = "개발모드 인증번호: " + data.debug_code + " (운영에서는 표시되지 않게 설정해야 함)";
      }
    } catch(e) {
      msgEl.innerHTML = '<span class="err">요청 실패: 네트워크 오류</span>';
    }
  }

  // 전체동의
  const all = document.getElementById("agree_all");
  const reqs = Array.from(document.querySelectorAll(".agree_req"));
  all.addEventListener("change", () => {
    reqs.forEach(x => x.checked = all.checked);
  });
  reqs.forEach(x => x.addEventListener("change", () => {
    all.checked = reqs.every(y => y.checked);
  }));

  function beforeSubmit() {
    const email = buildEmail();
    const otp = document.getElementById("otp").value.trim();
    const pw = document.getElementById("pw").value;
    const pw2 = document.getElementById("pw2").value;

    if(!email) {
      alert("이메일을 입력해 주세요.");
      return false;
    }
    if(!otp) {
      alert("인증번호를 입력해 주세요.");
      return false;
    }
    if(pw !== pw2) {
      alert("비밀번호가 일치하지 않습니다.");
      return false;
    }
    if(!reqs.every(x => x.checked)) {
      alert("필수 약관에 동의해 주세요.");
      return false;
    }
    if(!document.getElementById("botcheck").checked) {
      alert("로봇이 아님을 확인해 주세요.");
      return false;
    }

    document.getElementById("usernameHidden").value = email;
    document.getElementById("otpHidden").value = otp;
    return true;
  }
</script>
</body>
</html>
    """
    return HTMLResponse(html_content)

@app.post("/signup")
def signup(
    username: str = Form(...),
    otp: str = Form(...),
    password: str = Form(...),
    name: str = Form(...),
    gender: str = Form(...),
    birth: str = Form(...),
):
    email = normalize_email(username)

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
# 12) API: events / join / leave / html
# =========================================================
@app.get("/api/events_json")
def api_events_json(request: Request):
    user = get_current_user_fastapi(request)
    uid = user["id"] if user else None
    events = fetch_events_enriched(uid)
    for e in events:
        e["time_left"] = fmt_remaining(e.get("end") or "")
        e["start_disp"] = fmt_start(e.get("start") or "")
    return JSONResponse({"ok": True, "events": events})

@app.get("/api/events_html")
def api_events_html(request: Request):
    user = get_current_user_fastapi(request)
    uid = user["id"] if user else None
    return JSONResponse({"ok": True, "html": build_events_html(uid)})

@app.get("/api/joined_html")
def api_joined_html(request: Request):
    user = get_current_user_fastapi(request)
    uid = user["id"] if user else None
    return JSONResponse({"ok": True, "html": build_joined_html(uid)})

@app.post("/api/join")
async def api_join(request: Request):
    user = get_current_user_fastapi(request)
    if not user:
        return JSONResponse({"ok": False, "message": "로그인이 필요합니다."}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    event_id = (payload.get("event_id") or "").strip()
    ok, msg = join_event(event_id, user["id"])
    code = 200 if ok else 400
    return JSONResponse({"ok": ok, "message": msg}, status_code=code)

@app.post("/api/leave")
async def api_leave(request: Request):
    user = get_current_user_fastapi(request)
    if not user:
        return JSONResponse({"ok": False, "message": "로그인이 필요합니다."}, status_code=401)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    event_id = (payload.get("event_id") or "").strip()
    ok, msg = leave_event(event_id, user["id"])
    code = 200 if ok else 400
    return JSONResponse({"ok": ok, "message": msg}, status_code=code)


# =========================================================
# 13) Map (카카오) - 참여/빠지기 + 남은시간 + 인원/정원
# =========================================================
@app.get("/map")
def map_h():
    html_page = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
</head>
<body>
  <div id="m" style="width:100%;height:100vh;"></div>
  <script src="//dapi.kakao.com/v2/maps/sdk.js?appkey=__KAKAO_JS_KEY__"></script>
  <script>
    // 여긴 JS 중괄호 마음껏 써도 됨
  </script>
</body>
</html>
"""
    html_page = html_page.replace("__KAKAO_JS_KEY__", KAKAO_JAVASCRIPT_KEY)
    return HTMLResponse(html_page)


# =========================================================
# 14) Gradio 마운트
# =========================================================
app = gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

