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
DT_FMT = "%Y-%m-%d %H:%M"

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
                user_id TEXT,
                capacity INTEGER DEFAULT 10
            );
            """
        )
        for col_sql in [
            "ALTER TABLE events ADD COLUMN user_id TEXT",
            "ALTER TABLE events ADD COLUMN capacity INTEGER DEFAULT 10",
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

        # 참여(1유저 1이벤트 제한은 로직으로 강제)
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
# 4) 유틸: 남은시간/참여 현황
# =========================================================
def _parse_dt(s: str):
    try:
        return datetime.strptime((s or "").strip(), DT_FMT).replace(tzinfo=KST)
    except Exception:
        return None

def time_left_text(end_str: str) -> str:
    end_dt = _parse_dt(end_str)
    if not end_dt:
        return ""
    delta = end_dt - now_kst()
    if delta.total_seconds() <= 0:
        return "종료됨"
    mins = int(delta.total_seconds() // 60)
    days = mins // (24*60)
    mins = mins % (24*60)
    hours = mins // 60
    mins = mins % 60

    if days > 0:
        if hours > 0:
            return f"D-{days} {hours}시간"
        return f"D-{days}"
    if hours > 0 and mins > 0:
        return f"{hours}시간 {mins}분 남음"
    if hours > 0:
        return f"{hours}시간 남음"
    return f"{mins}분 남음"

def get_joined_event_id(user_id: str):
    with db_conn() as con:
        row = con.execute(
            "SELECT event_id FROM event_participants WHERE user_id=? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row[0] if row else None

def get_participant_counts():
    with db_conn() as con:
        rows = con.execute(
            "SELECT event_id, COUNT(*) FROM event_participants GROUP BY event_id"
        ).fetchall()
    return {r[0]: int(r[1]) for r in rows}

def list_events(user_id: str | None):
    with db_conn() as con:
        rows = con.execute(
            """
            SELECT id, title, photo, start, end, addr, lat, lng, capacity
            FROM events
            ORDER BY created_at DESC
            """
        ).fetchall()

    counts = get_participant_counts()

    joined_set = set()
    if user_id:
        with db_conn() as con:
            jrows = con.execute(
                "SELECT event_id FROM event_participants WHERE user_id=?",
                (user_id,),
            ).fetchall()
        joined_set = {r[0] for r in jrows}

    out = []
    for r in rows:
        eid, title, photo, start, end, addr, lat, lng, cap = r
        cap = int(cap or 10)
        cur = counts.get(eid, 0)
        joined = (eid in joined_set)
        out.append(
            {
                "id": eid,
                "title": title or "",
                "photo": photo or "",
                "start": start or "",
                "end": end or "",
                "addr": addr or "",
                "lat": float(lat or 0),
                "lng": float(lng or 0),
                "capacity": cap,
                "participants": cur,
                "joined": joined,
                "time_left": time_left_text(end or ""),
            }
        )
    return out


# =========================================================
# 5) CSS (참여중인 활동 섹션 + 데스크톱 이미지 축소 포함)
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
  gap: 6px;
  flex-wrap: wrap;
}
.event-actions {
  margin-top: 10px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}
.pill {
  font-size: 12px;
  color: #333;
  background: #f3f3f3;
  border: 1px solid #eee;
  padding: 6px 10px;
  border-radius: 999px;
}
.oseyo-btn {
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid #111;
  background: #111;
  color: #fff;
  font-weight: 800;
  font-size: 13px;
  cursor: pointer;
  white-space: nowrap;
}
.oseyo-btn.secondary {
  background: #fff;
  color: #111;
}
.oseyo-btn:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

/* ✅ 참여중인 활동 섹션 */
.joined-wrap{
  padding: 14px 24px 6px 24px;
}
.joined-title{
  font-size: 14px;
  font-weight: 900;
  color: #111;
  margin: 6px 0 10px 0;
}
.joined-box{
  background: #fafafa;
  border: 1px solid #eee;
  border-radius: 18px;
  padding: 14px;
}
.joined-box .event-card{
  margin-bottom: 0;
}
.joined-hint{
  font-size: 12px;
  color: #777;
  margin-top: 8px;
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

.event-actions { position: relative; z-index: 5; }
.event-photo { position: relative; z-index: 1; }

/* ✅ 데스크톱에서는 탐색탭 카드 이미지를 줄이고 가로 레이아웃으로 */
@media (min-width: 900px) {
  .event-card {
    display: flex;
    gap: 14px;
    align-items: flex-start;
  }
  .event-photo {
    width: 320px;
    height: 180px;
    aspect-ratio: auto;
    margin-bottom: 0;
    flex: 0 0 auto;
  }
  .event-info { padding: 0; flex: 1 1 auto; }
}
"""


# =========================================================
# 6) 이벤트/즐겨찾기 로직 (생성/삭제 등 기존 유지)
# =========================================================
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
            "INSERT INTO events (id,title,photo,start,end,addr,lat,lng,created_at,user_id,capacity) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
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
                10,  # 기본 정원
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
        con.execute("DELETE FROM event_participants WHERE event_id = ?", (event_id,))
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
    if not user:
        favs = get_top_favs(10)
        return "로그인이 필요합니다.", *fav_buttons_update(favs)

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


# =========================================================
# 7) 탐색/참여중 HTML 렌더
# =========================================================
def render_event_card(e: dict, force_joined: bool | None = None) -> str:
    title = html.escape(e.get("title") or "")
    addr = html.escape(e.get("addr") or "장소 미정")

    # 시작 표시
    start_raw = e.get("start") or ""
    try:
        sdt = datetime.strptime(start_raw, DT_FMT)
        start_str = sdt.strftime("%m월 %d일 %H:%M")
    except Exception:
        start_str = start_raw

    left = e.get("time_left") or ""
    left_str = f" · {html.escape(left)}" if left else ""

    # 이미지
    photo = e.get("photo") or ""
    if photo:
        img_html = f"<img class='event-photo' src='data:image/jpeg;base64,{photo}' />"
    else:
        img_html = "<div class='event-photo' style='display:flex;align-items:center;justify-content:center;color:#ccc;'>NO IMAGE</div>"

    participants = int(e.get("participants") or 0)
    capacity = int(e.get("capacity") or 10)

    joined = bool(e.get("joined"))
    if force_joined is not None:
        joined = bool(force_joined)

    full = participants >= capacity

    if joined:
        btn = f"<button class='oseyo-btn secondary' data-oseyo-action='leave' data-eid='{e.get('id','')}'>빠지기</button>"
    else:
        dis = "disabled" if full else ""
        btn = f"<button class='oseyo-btn' data-oseyo-action='join' data-eid='{e.get('id','')}' {dis}>참여하기</button>"

    return f"""
      <div class='event-card'>
        {img_html}
        <div class='event-info'>
          <div class='event-title'>{title}</div>
          <div class='event-meta'>⏰ {html.escape(start_str)}{left_str}</div>
          <div class='event-meta'>📍 {addr}</div>
          <div class='event-actions'>
            <div class='pill'>👥 {participants} / {capacity}</div>
            {btn}
          </div>
        </div>
      </div>
    """

def render_explore_cards(user_id: str | None):
    events = list_events(user_id)
    if not events:
        return "<div style='text-align:center; padding:100px 20px; color:#999;'>등록된 이벤트가 없습니다.<br>오른쪽 아래 버튼을 눌러 시작해보세요.</div>"
    out = ""
    for e in events:
        out += render_event_card(e)
    return out

def render_joined_block(user_id: str | None) -> str:
    if not user_id:
        return ""  # 로그인 전에는 숨김
    joined_id = get_joined_event_id(user_id)
    if not joined_id:
        return ""

    with db_conn() as con:
        row = con.execute(
            """
            SELECT id, title, photo, start, end, addr, lat, lng, capacity
            FROM events
            WHERE id=?
            """,
            (joined_id,),
        ).fetchone()
        if not row:
            return ""

        cnt = con.execute(
            "SELECT COUNT(*) FROM event_participants WHERE event_id=?",
            (joined_id,),
        ).fetchone()

    participants = int(cnt[0] or 0)
    eid, title, photo, start, end, addr, lat, lng, cap = row
    cap = int(cap or 10)

    e = {
        "id": eid,
        "title": title or "",
        "photo": photo or "",
        "start": start or "",
        "end": end or "",
        "addr": addr or "",
        "lat": float(lat or 0),
        "lng": float(lng or 0),
        "capacity": cap,
        "participants": participants,
        "joined": True,
        "time_left": time_left_text(end or ""),
    }

    card = render_event_card(e, force_joined=True)

    return f"""
    <div class="joined-wrap">
      <div class="joined-title">참여중인 활동</div>
      <div class="joined-box">
        {card}
        <div class="joined-hint">여기서 바로 빠지기를 눌러 참여를 해제할 수 있습니다.</div>
      </div>
    </div>
    """


# =========================================================
# 8) Gradio UI
# =========================================================
now_dt = now_kst()
later_dt = now_dt + timedelta(hours=2)

with gr.Blocks(css=CSS, title="오세요") as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    # ✅ 전역 JS: 탐색/지도 상단 '참여중인 활동' + 목록 동기화 + 지도 iframe 갱신
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
    try{
      iframe.contentWindow.postMessage({type:"oseyo_refresh"}, "*");
    }catch(e){}
  }

  // iframe -> parent 메시지 처리(지도에서 참여/빠지기 했을 때 탐색/상단 즉시 반영)
  window.addEventListener("message", (ev) => {
    if(!ev || !ev.data) return;
    if(ev.data.type === "oseyo_changed"){
      refreshAll();
    }
  });

  // 탐색탭/상단 카드 클릭 델리게이션(참여/빠지기)
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
    try{
      iframe.contentWindow.postMessage({type:"oseyo_refresh"}, "*");
    }catch(e){}
  }

  // ✅ Shadow DOM에서도 버튼을 잡기 위한 helper
  function findOseyoButton(ev){
    const path = (ev && typeof ev.composedPath === "function") ? ev.composedPath() : null;
    if(path && path.length){
      for(const el of path){
        if(el && el.tagName === "BUTTON" && el.dataset && el.dataset.oseyoAction && el.dataset.eid){
          return el;
        }
      }
    }
    // fallback
    const t = ev.target;
    if(t && t.closest){
      return t.closest("button[data-oseyo-action][data-eid]");
    }
    return null;
  }

  // iframe -> parent 메시지 처리
  window.addEventListener("message", (ev) => {
    if(!ev || !ev.data) return;
    if(ev.data.type === "oseyo_changed"){
      refreshAll();
    }
  });

  // ✅ click 대신 pointerup + capture + composedPath 사용
  window.addEventListener("pointerup", async (ev) => {
    const btn = findOseyoButton(ev);
    if(!btn) return;

    ev.preventDefault();
    ev.stopPropagation();

    const action = btn.dataset.oseyoAction;
    const eid = btn.dataset.eid;
    if(!action || !eid) return;

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
        await refreshAll();      // ✅ 탐색/상단 즉시 갱신
        notifyMapRefresh();      // ✅ 지도도 즉시 갱신
      }
    }catch(e){
      toast("네트워크 오류");
    }finally{
      btn.disabled = false;
    }
  }, true); // ✅ capture=true

  window.addEventListener("load", () => {
    refreshAll();
  });

  window.oseyoRefreshAll = refreshAll;
  window.oseyoNotifyMapRefresh = notifyMapRefresh;
})();
</script>

  // 최초 1회 로드
  window.addEventListener("load", () => {
    refreshAll();
  });

  // 외부에서 호출용
  window.oseyoRefreshAll = refreshAll;
  window.oseyoNotifyMapRefresh = notifyMapRefresh;
})();
</script>
    """)

    gr.HTML("""
    <div class="header-row">
        <div class="main-title">지금, <b>열려 있습니다</b><br><span style="font-size:15px; color:#666; font-weight:400;">편하면 오셔도 됩니다</span></div>
        <a href="/logout" class="logout-link">로그아웃</a>
    </div>
    """)

    with gr.Tabs(elem_classes=["tabs"]):
        with gr.Tab("탐색"):
            explore_html = gr.HTML("""
              <div id="oseyo_joined_explore_root"></div>
              <div id="oseyo_explore_root"></div>
            """)
            refresh_btn = gr.Button("🔄 목록 새로고침", variant="secondary", size="sm")

        with gr.Tab("지도"):
            gr.HTML("""
              <div id="oseyo_joined_map_root"></div>
              <iframe id="map_iframe" src="/map" style="width:100%;height:70vh;border:none;border-radius:16px;"></iframe>
            """)

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
                gr.Markdown("---")

                t_in = gr.Textbox(label="이벤트명", placeholder="예: 30분 산책, 조용히 책 읽기", lines=1)

                with gr.Accordion("사진 추가 (선택)", open=False):
                    img_in = gr.Image(label="사진", type="numpy", height=200)

                with gr.Row():
                    s_in = gr.Textbox(label="시작", value=now_dt.strftime(DT_FMT))
                    e_in = gr.Textbox(label="종료", value=later_dt.strftime(DT_FMT))

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

    # refresh 버튼도 참여중/목록 둘 다 갱신되도록(파이썬 방식)
    def refresh_all_py(request: gr.Request):
        user = get_current_user(request)
        uid = user["id"] if user else None
        joined = render_joined_block(uid)
        explore = render_explore_cards(uid)
        return f"""
          <div id="oseyo_joined_explore_root">{joined}</div>
          <div id="oseyo_explore_root">{explore}</div>
        """

    refresh_btn.click(fn=refresh_all_py, inputs=None, outputs=explore_html)

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

    fab.click(
        open_main_modal,
        None,
        [overlay, modal_m, my_event_list, del_msg] + fav_btns + [fav_msg],
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
        outputs=[fav_msg] + fav_btns,
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

    def save_and_close(title, img, start, end, addr, req: gr.Request):
        _ = save_data(title, img, start, end, addr, req)
        # 파이썬 렌더도 참여중 + 목록 갱신
        user = get_current_user(req)
        uid = user["id"] if user else None
        joined = render_joined_block(uid)
        explore = render_explore_cards(uid)
        favs = get_top_favs(10)
        return f"""
          <div id="oseyo_joined_explore_root">{joined}</div>
          <div id="oseyo_explore_root">{explore}</div>
        """, gr.update(visible=False), gr.update(visible=False), *fav_buttons_update(favs)

    m_save.click(
        save_and_close,
        [t_in, img_in, s_in, e_in, selected_addr],
        [explore_html, overlay, modal_m] + fav_btns,
    )

    del_btn.click(
        delete_my_event,
        [my_event_list],
        [del_msg, my_event_list],
    ).then(refresh_all_py, None, explore_html)


# =========================================================
# 9) FastAPI + 로그인/회원가입 + 이메일 OTP + 참여 API
# =========================================================
app = FastAPI()

PUBLIC_PATHS = {
    "/", "/login", "/signup", "/logout", "/health", "/map",
    "/send_email_otp",
    "/api/events_html", "/api/events_json", "/api/joined_html",
    "/api/join", "/api/leave"
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
        resp["debug_code"] = code

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
    body {{ font-family:Pretendard, system-ui; background:#fff; margin:0; padding:0;
      display:flex; justify-content:center; align-items:center; min-height:100vh; }}
    .wrap {{ width:100%; max-width:390px; padding:20px; }}
    h2 {{ margin:0 0 12px 0; font-size:22px; }}
    .muted {{ color:#777; font-size:13px; margin-bottom:18px; }}
    input, select {{ width:100%; padding:12px; margin:8px 0; border:1px solid #ddd; border-radius:8px; box-sizing:border-box; font-size:14px; }}
    input:focus, select:focus {{ outline:none; border-color:#111; }}
    .row {{ display:flex; gap:8px; }}
    .row > * {{ flex:1; }}
    .btn {{ width:100%; padding:13px; background:#111; color:#fff; border:none; border-radius:8px; cursor:pointer; font-weight:700; margin-top:10px; }}
    .btn2 {{ padding:12px; background:#f0f0f0; color:#111; border:none; border-radius:8px; cursor:pointer; font-weight:700; white-space:nowrap; }}
    .msg {{ margin-top:10px; font-size:13px; color:#444; }}
    .err {{ color:#c00; }}
    .ok {{ color:#0a7; }}
    a {{ color:#333; }}
    .debug {{ background:#fff7cc; padding:10px; border-radius:8px; font-size:13px; margin-top:10px; display:none; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h2>회원가입</h2>
    <div class="muted">이메일 인증 후 가입을 완료해 주세요.</div>

    <div class="row">
      <input id="email" placeholder="이메일(아이디)" />
      <button class="btn2" type="button" onclick="sendOtp()">인증메일 받기</button>
    </div>
    <input id="otp" placeholder="인증번호 6자리" />
    <div id="otpMsg" class="msg"></div>
    <div id="debugBox" class="debug"></div>

    <form method="post" action="/signup" onsubmit="return beforeSubmit();">
      <input id="usernameHidden" name="username" type="hidden" />
      <input id="otpHidden" name="otp" type="hidden" />

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

      <button class="btn" type="submit">가입완료</button>
      <p style="margin-top:12px;font-size:13px;color:#666;">
        이미 계정이 있나요? <a href="/login">로그인</a>
      </p>
    </form>
  </div>

<script>
  async function sendOtp() {{
    const email = document.getElementById("email").value.trim();
    const msgEl = document.getElementById("otpMsg");
    const dbg = document.getElementById("debugBox");
    msgEl.textContent = "";
    dbg.style.display = "none";
    dbg.textContent = "";

    if (!email) {{
      msgEl.innerHTML = '<span class="err">이메일을 입력해 주세요.</span>';
      return;
    }}

    try {{
      const r = await fetch("/send_email_otp", {{
        method: "POST",
        headers: {{"Content-Type":"application/json"}},
        body: JSON.stringify({{email}})
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
    const email = document.getElementById("email").value.trim();
    const otp = document.getElementById("otp").value.trim();
    document.getElementById("usernameHidden").value = email;
    document.getElementById("otpHidden").value = otp;
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


# -------------------------
# ✅ API: 탐색 HTML / 지도 JSON / 참여중 HTML
# -------------------------
def _user_from_cookie(req: Request):
    token = req.cookies.get(COOKIE_NAME)
    return get_user_by_token(token)

@app.get("/api/events_html")
def api_events_html(req: Request):
    user = _user_from_cookie(req)
    inner = render_explore_cards(user["id"] if user else None)
    return JSONResponse({"ok": True, "html": inner})

@app.get("/api/events_json")
def api_events_json(req: Request):
    user = _user_from_cookie(req)
    events = list_events(user["id"] if user else None)
    return JSONResponse({"ok": True, "events": events})

@app.get("/api/joined_html")
def api_joined_html(req: Request):
    user = _user_from_cookie(req)
    h = render_joined_block(user["id"] if user else None)
    return JSONResponse({"ok": True, "html": h})


# -------------------------
# ✅ API: 참여/빠지기
# -------------------------
@app.post("/api/join")
async def api_join(req: Request):
    user = _user_from_cookie(req)
    if not user:
        return JSONResponse({"ok": False, "message": "로그인이 필요합니다."}, status_code=401)

    try:
        payload = await req.json()
    except Exception:
        payload = {}
    eid = (payload.get("event_id") or "").strip()
    if not eid:
        return JSONResponse({"ok": False, "message": "이벤트 ID가 없습니다."}, status_code=400)

    with db_conn() as con:
        ev = con.execute("SELECT capacity FROM events WHERE id=?", (eid,)).fetchone()
        if not ev:
            return JSONResponse({"ok": False, "message": "이벤트를 찾을 수 없습니다."}, status_code=404)
        cap = int(ev[0] or 10)

        # 이미 다른 이벤트 참여 중인지 확인 (1개 제한)
        row = con.execute(
            "SELECT event_id FROM event_participants WHERE user_id=? LIMIT 1",
            (user["id"],),
        ).fetchone()
        if row and row[0] != eid:
            return JSONResponse({"ok": False, "message": "이미 다른 이벤트에 참여 중입니다. 먼저 빠지기를 해주세요."}, status_code=409)

        # 이미 참여 중이면 OK
        already = con.execute(
            "SELECT 1 FROM event_participants WHERE event_id=? AND user_id=?",
            (eid, user["id"]),
        ).fetchone()
        if already:
            return JSONResponse({"ok": True})

        # 정원 체크
        cur = con.execute(
            "SELECT COUNT(*) FROM event_participants WHERE event_id=?",
            (eid,),
        ).fetchone()
        cur_n = int(cur[0] or 0)
        if cur_n >= cap:
            return JSONResponse({"ok": False, "message": "정원이 다 찼습니다."}, status_code=409)

        con.execute(
            "INSERT INTO event_participants (event_id, user_id, joined_at) VALUES (?,?,?)",
            (eid, user["id"], now_kst().isoformat(timespec="seconds")),
        )
        con.commit()

    return JSONResponse({"ok": True})

@app.post("/api/leave")
async def api_leave(req: Request):
    user = _user_from_cookie(req)
    if not user:
        return JSONResponse({"ok": False, "message": "로그인이 필요합니다."}, status_code=401)

    try:
        payload = await req.json()
    except Exception:
        payload = {}
    eid = (payload.get("event_id") or "").strip()
    if not eid:
        return JSONResponse({"ok": False, "message": "이벤트 ID가 없습니다."}, status_code=400)

    with db_conn() as con:
        con.execute(
            "DELETE FROM event_participants WHERE event_id=? AND user_id=?",
            (eid, user["id"]),
        )
        con.commit()

    return JSONResponse({"ok": True})


# =========================================================
# 10) Map (참여/빠지기 + 남은시간 + 인원/정원 + 탭 동기화)
# =========================================================
@app.get("/map")
def map_h():
    return HTMLResponse(f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    body{{margin:0; font-family: Pretendard, system-ui;}}
    .iw-title{{font-weight:800; font-size:14px; margin-bottom:6px;}}
    .iw-meta{{font-size:12px; margin-top:4px; color:#666;}}
    .iw-img{{width:100%; height:110px; object-fit:cover; border-radius:8px; margin-top:8px; border:1px solid #eee;}}
    .iw-row{{display:flex; justify-content:space-between; align-items:center; gap:10px; margin-top:10px;}}
    .pill{{font-size:12px; color:#333; background:#f3f3f3; border:1px solid #eee; padding:6px 10px; border-radius:999px;}}
    .btn{{padding:8px 10px; border-radius:10px; border:1px solid #111; font-weight:800; font-size:12px; cursor:pointer; white-space:nowrap;}}
    .btn.primary{{background:#111; color:#fff;}}
    .btn.secondary{{background:#fff; color:#111;}}
    .btn:disabled{{opacity:.35; cursor:not-allowed;}}
  </style>
</head>
<body>
  <div id="m" style="width:100%;height:100vh;"></div>
  <script src="//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}"></script>
  <script>
  function esc(s) {
    return String(s||"")
      .replaceAll("&","&amp;")
      .replaceAll("<","&lt;")
      .replaceAll(">","&gt;")
      .replaceAll('"',"&quot;")
      .replaceAll("'","&#039;");
  }

  const map = new kakao.maps.Map(document.getElementById('m'), {
    center: new kakao.maps.LatLng(36.019, 129.343),
    level: 7
  });

  // ✅ event_id -> { marker, iw, data }
  const store = new Map();
  let openEventId = null;

  function renderInfo(d){
    const title = esc(d.title);
    const addr = esc(d.addr);
    const start = esc(d.start);
    const left = d.time_left ? (" · " + esc(d.time_left)) : "";
    const img = d.photo ? `<img class="iw-img" src="data:image/jpeg;base64,${d.photo}">` : "";
    const full = (d.participants >= d.capacity);
    const joined = !!d.joined;

    const btnLabel = joined ? "빠지기" : "참여하기";
    const btnCls = joined ? "btn secondary" : "btn primary";
    const dis = (!joined && full) ? "disabled" : "";

    return `
      <div style="padding:10px;width:240px;">
        <div class="iw-title">${title}</div>
        <div class="iw-meta">⏰ ${start}${left}</div>
        <div class="iw-meta">📍 ${addr}</div>
        ${img}
        <div class="iw-row">
          <div class="pill">👥 ${d.participants} / ${d.capacity}</div>
          <button class="${btnCls}" data-oseyo-action="${joined ? "leave" : "join"}" data-eid="${d.id}" ${dis}>${btnLabel}</button>
        </div>
      </div>
    `;
  }

  function upsertEvent(d){
    if(!d.lat || !d.lng) return;

    const pos = new kakao.maps.LatLng(d.lat, d.lng);

    if(!store.has(d.id)){
      const marker = new kakao.maps.Marker({ position: pos, map: map });
      const iw = new kakao.maps.InfoWindow({ content: renderInfo(d), removable: true });

      kakao.maps.event.addListener(marker, 'click', () => {
        // ✅ 열려 있던 것이 있으면 닫되, "닫는 것"은 marker 클릭에만 반응
        // (참여/빠지기에서는 안 닫음)
        for(const [eid, obj] of store.entries()){
          if(obj.iw && eid !== d.id) obj.iw.close();
        }
        iw.open(map, marker);
        openEventId = d.id;
      });

      store.set(d.id, { marker, iw, data: d });
    }else{
      const obj = store.get(d.id);
      obj.data = d;
      obj.marker.setPosition(pos);
      obj.iw.setContent(renderInfo(d)); // ✅ 열린 상태면 닫히지 않고 버튼만 갱신됨
    }
  }

  function removeMissing(newIds){
    for(const [eid, obj] of store.entries()){
      if(!newIds.has(eid)){
        try{ obj.iw.close(); }catch(e){}
        try{ obj.marker.setMap(null); }catch(e){}
        store.delete(eid);
        if(openEventId === eid) openEventId = null;
      }
    }
  }

  async function loadData(){
    try{
      const r = await fetch("/api/events_json", {credentials:"include"});
      const data = await r.json();
      if(!r.ok || !data.ok) return;

      const events = data.events || [];
      const ids = new Set(events.map(x => x.id));

      // ✅ 업데이트/추가
      events.forEach(upsertEvent);

      // ✅ 삭제 반영
      removeMissing(ids);

      // ✅ 열린 인포윈도우 유지 (content는 이미 setContent로 갱신됨)
      if(openEventId && store.has(openEventId)){
        const obj = store.get(openEventId);
        // 열려있는지 확실히 유지 (가끔 브라우저가 닫는 경우 대비)
        obj.iw.open(map, obj.marker);
      }

    }catch(e){}
  }

  // ✅ 지도 내 참여/빠지기: 인포윈도우 닫지 않고 content만 업데이트
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-oseyo-action][data-eid]");
    if(!btn) return;

    const action = btn.getAttribute("data-oseyo-action");
    const eid = btn.getAttribute("data-eid");
    btn.disabled = true;

    try{
      const r = await fetch(action === "join" ? "/api/join" : "/api/leave", {
        method:"POST",
        headers: {"Content-Type":"application/json"},
        credentials:"include",
        body: JSON.stringify({event_id:eid})
      });
      const data = await r.json();

      if(!r.ok || !data.ok){
        alert((data && data.message) ? data.message : "요청 실패");
      }else{
        // ✅ 닫지 말고, 데이터만 새로 로드해서 setContent 갱신
        await loadData();

        // ✅ 부모 탭(탐색/상단) 즉시 갱신 트리거
        try{ parent.postMessage({type:"oseyo_changed"}, "*"); }catch(e){}
      }
    }catch(e){
      alert("네트워크 오류");
    }finally{
      btn.disabled = false;
    }
  });

  // ✅ 부모에서 갱신 신호 받으면 로드
  window.addEventListener("message", (ev) => {
    if(ev && ev.data && ev.data.type === "oseyo_refresh"){
      loadData();
    }
  });

  // 최초 + 폴링
  loadData();
  setInterval(loadData, 4000);
</script>
      
# =========================================================
# 11) Gradio 마운트
# =========================================================
app = gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
