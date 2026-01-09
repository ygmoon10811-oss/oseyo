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
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()  # "오세요 <me@gmail.com>" 가능


DT_FMT = "%Y-%m-%d %H:%M"


# =========================================================
# 1) 환경/DB (⭐ 기존 DB 유지: 파일명/경로 절대 변경 금지)
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
            return os.path.join(d, "oseyo_final_email_v1.db")  # ✅ 기존과 동일
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
        # 마이그레이션 (있으면 무시)
        for col_sql in [
            "ALTER TABLE events ADD COLUMN user_id TEXT",
            "ALTER TABLE events ADD COLUMN capacity INTEGER DEFAULT 10",
        ]:
            try:
                con.execute(col_sql)
            except Exception:
                pass

        # 즐겨찾기(기존 유지: 글로벌 카운트)
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

        # ✅ 개인 즐겨찾기(새로 추가: 삭제/개인화용)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS user_favs (
                user_id TEXT,
                name TEXT,
                count INTEGER DEFAULT 1,
                updated_at TEXT,
                PRIMARY KEY(user_id, name)
            );
            """
        )

        # 유저
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

        # ✅ 참여(유저는 1개 이벤트만 참여 가능)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS event_members (
                event_id TEXT,
                user_id TEXT UNIQUE,
                joined_at TEXT
            );
            """
        )

        con.commit()

init_db()


# =========================================================
# 2) 비밀번호/세션 유틸 (⭐ 기존 계정 로그인 유지)
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

def get_current_user_gr(request: gr.Request):
    if not request:
        return None
    token = request.cookies.get(COOKIE_NAME)
    return get_user_by_token(token)

def get_current_user_fastapi(request: Request):
    token = request.cookies.get(COOKIE_NAME) if request else None
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
# 4) 시간/상태 헬퍼
# =========================================================
def parse_dt(s: str):
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, DT_FMT).replace(tzinfo=KST)
    except Exception:
        return None

def is_ended(end_str: str) -> bool:
    end_dt = parse_dt(end_str)
    return bool(end_dt and end_dt <= now_kst())

def remain_str(end_str: str) -> str:
    end_dt = parse_dt(end_str)
    if not end_dt:
        return ""
    diff = end_dt - now_kst()
    sec = int(diff.total_seconds())
    if sec <= 0:
        return "종료됨"
    m = sec // 60
    h = m // 60
    d = h // 24
    m = m % 60
    h = h % 24
    if d > 0:
        return f"종료까지 {d}일 {h}시간"
    if h > 0:
        return f"종료까지 {h}시간 {m}분"
    return f"종료까지 {m}분"

def get_joined_event_id(user_id: str):
    if not user_id:
        return None
    with db_conn() as con:
        row = con.execute(
            "SELECT event_id FROM event_members WHERE user_id=?",
            (user_id,),
        ).fetchone()
    return row[0] if row else None

def get_event_counts():
    with db_conn() as con:
        rows = con.execute(
            "SELECT event_id, COUNT(*) FROM event_members GROUP BY event_id"
        ).fetchall()
    return {r[0]: int(r[1]) for r in rows}

def safe(s: str) -> str:
    return html.escape(s or "")


# =========================================================
# 5) 이벤트/즐겨찾기 로직 (저장/삭제/즐겨찾기)
# =========================================================
def save_data(title, img, start, end, addr_obj, capacity, request: gr.Request):
    user = get_current_user_gr(request)
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
        cap = int(capacity) if capacity is not None and str(capacity).strip() != "" else 10
        if cap <= 0:
            cap = 10
        if cap > 999:
            cap = 999
    except Exception:
        cap = 10

    with db_conn() as con:
        con.execute(
            """
            INSERT INTO events (id, title, photo, start, end, addr, lat, lng, created_at, user_id, capacity)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
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
                cap,
            ),
        )

        # 글로벌(기존) 즐겨찾기 카운트 유지
        con.execute(
            """
            INSERT INTO favs (name, count, updated_at) VALUES (?, 1, ?)
            ON CONFLICT(name) DO UPDATE SET count = count + 1, updated_at=excluded.updated_at
            """,
            (title, now_kst().isoformat(timespec="seconds")),
        )

        # 개인 즐겨찾기 업데이트
        con.execute(
            """
            INSERT INTO user_favs (user_id, name, count, updated_at) VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, name) DO UPDATE SET count = count + 1, updated_at=excluded.updated_at
            """,
            (user["id"], title, now_kst().isoformat(timespec="seconds")),
        )

        con.commit()

    return "✅ 이벤트가 생성되었습니다"

def get_my_events(request: gr.Request):
    user = get_current_user_gr(request)
    if not user:
        return []
    with db_conn() as con:
        rows = con.execute(
            "SELECT id, title FROM events WHERE user_id = ? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    return [(f"{r[1]}", r[0]) for r in rows]

def delete_my_event(event_id, request: gr.Request):
    user = get_current_user_gr(request)
    if not user or not event_id:
        return "삭제할 이벤트를 선택해주세요.", gr.update(), ""

    with db_conn() as con:
        con.execute("DELETE FROM events WHERE id = ? AND user_id = ?", (event_id, user["id"]))
        # 그 이벤트에 참여중인 사람도 정리
        con.execute("DELETE FROM event_members WHERE event_id = ?", (event_id,))
        con.commit()

    new_list = get_my_events(request)
    return "✅ 삭제되었습니다.", gr.update(choices=new_list, value=None), uuid.uuid4().hex

def get_my_favs(request: gr.Request, limit=30):
    user = get_current_user_gr(request)
    if not user:
        return []
    with db_conn() as con:
        rows = con.execute(
            """
            SELECT name, count FROM user_favs
            WHERE user_id=?
            ORDER BY count DESC, updated_at DESC
            LIMIT ?
            """,
            (user["id"], limit),
        ).fetchall()
    return [{"name": r[0], "count": r[1]} for r in rows]

def get_global_favs(limit=10):
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

def merged_favs_for_buttons(request: gr.Request, limit=10):
    user = get_current_user_gr(request)
    personal = get_my_favs(request, limit=limit)
    # 개인 즐겨찾기 우선, 부족하면 글로벌로 채움(중복 제거)
    seen = set()
    out = []
    for f in personal:
        n = (f["name"] or "").strip()
        if n and n not in seen:
            out.append({"name": n, "count": f["count"]})
            seen.add(n)
        if len(out) >= limit:
            return out

    for f in get_global_favs(limit=limit * 2):
        n = (f["name"] or "").strip()
        if n and n not in seen:
            out.append({"name": n, "count": f["count"]})
            seen.add(n)
        if len(out) >= limit:
            break
    return out

def fav_buttons_update(favs):
    updates = []
    for i in range(10):
        if i < len(favs):
            updates.append(gr.update(value=f"⭐ {favs[i]['name']}", visible=True))
        else:
            updates.append(gr.update(value="", visible=False))
    return updates

def add_fav_only(name: str, request: gr.Request):
    user = get_current_user_gr(request)
    if not user:
        favs = merged_favs_for_buttons(request, 10)
        return "로그인이 필요합니다.", gr.update(choices=[]), *fav_buttons_update(favs)

    name = (name or "").strip()
    if not name:
        favs = merged_favs_for_buttons(request, 10)
        my_choices = [x["name"] for x in get_my_favs(request, 30)]
        return "활동명을 입력해 주세요.", gr.update(choices=my_choices), *fav_buttons_update(favs)

    with db_conn() as con:
        # 글로벌(기존) 유지
        con.execute(
            """
            INSERT INTO favs (name, count, updated_at) VALUES (?, 1, ?)
            ON CONFLICT(name) DO UPDATE SET count = count + 1, updated_at=excluded.updated_at
            """,
            (name, now_kst().isoformat(timespec="seconds")),
        )
        # 개인 저장
        con.execute(
            """
            INSERT INTO user_favs (user_id, name, count, updated_at) VALUES (?, ?, 1, ?)
            ON CONFLICT(user_id, name) DO UPDATE SET count = count + 1, updated_at=excluded.updated_at
            """,
            (user["id"], name, now_kst().isoformat(timespec="seconds")),
        )
        con.commit()

    favs = merged_favs_for_buttons(request, 10)
    my_choices = [x["name"] for x in get_my_favs(request, 30)]
    return "✅ 즐겨찾기에 추가되었습니다.", gr.update(choices=my_choices, value=None), *fav_buttons_update(favs)

def delete_my_fav(name: str, request: gr.Request):
    user = get_current_user_gr(request)
    if not user:
        favs = merged_favs_for_buttons(request, 10)
        return "로그인이 필요합니다.", gr.update(choices=[]), *fav_buttons_update(favs)

    name = (name or "").strip()
    if not name:
        favs = merged_favs_for_buttons(request, 10)
        my_choices = [x["name"] for x in get_my_favs(request, 30)]
        return "삭제할 즐겨찾기를 선택해 주세요.", gr.update(choices=my_choices), *fav_buttons_update(favs)

    with db_conn() as con:
        con.execute("DELETE FROM user_favs WHERE user_id=? AND name=?", (user["id"], name))
        con.commit()

    favs = merged_favs_for_buttons(request, 10)
    my_choices = [x["name"] for x in get_my_favs(request, 30)]
    return "✅ 삭제했습니다.", gr.update(choices=my_choices, value=None), *fav_buttons_update(favs)


# =========================================================
# 6) CSS + Gradio UI
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

.event-card { margin-bottom: 24px; }
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
}
.join-row {
  display:flex;
  justify-content: space-between;
  align-items:center;
  margin-top: 10px;
  gap: 10px;
}
.join-count {
  font-size: 13px;
  color:#555;
}
.join-btn {
  padding: 10px 14px;
  border-radius: 12px;
  font-weight: 800;
  border: none;
  cursor: pointer;
}
.join-btn.primary { background:#111; color:#fff; }
.join-btn.gray { background:#f0f0f0; color:#333; cursor:not-allowed; }
.join-btn.danger { background:#ffe8e8; color:#b00020; }

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

.joined-sticky {
  position: sticky;
  top: 0;
  z-index: 50;
  background: #fff;
  padding: 14px 24px 10px 24px;
  border-bottom: 1px solid #eee;
}
.joined-title {
  font-size: 14px;
  font-weight: 900;
  color: #111;
  margin-bottom: 10px;
}
.joined-empty {
  font-size: 13px;
  color: #777;
  padding: 10px 12px;
  background: #fafafa;
  border: 1px solid #eee;
  border-radius: 12px;
}
.badge-ended {
  display:inline-block;
  font-size:12px;
  font-weight:900;
  padding:6px 10px;
  border-radius:999px;
  background:#f2f2f2;
  color:#666;
  margin-top:8px;
}
.event-ended {
  filter: grayscale(1);
  opacity: 0.65;
}
.event-ended .join-row { display:none !important; }

/* ✅ 웹에서 사진이 너무 길게 보이는 문제만 완화(모바일 유지) */
@media (min-width: 900px) {
  .event-photo { max-height: 320px; }
}
"""

now_dt = now_kst()
later_dt = now_dt + timedelta(hours=2)

with gr.Blocks(css=CSS, title="오세요") as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    # ✅ JS 메시지/동기화 스크립트 (탐색/지도 즉시 반영)
    gr.HTML("""
<script>
(function(){
  function $(id){ return document.getElementById(id); }

  async function fetchText(url){
    const r = await fetch(url, {cache:"no-store", credentials:"same-origin"});
    return await r.text();
  }

  window.OSEYO = window.OSEYO || {};

  window.OSEYO.refreshExplore = async function(){
    const root = $("explore_root");
    if(!root) return;
    try{
      root.innerHTML = "<div style='padding:60px 24px; color:#999; text-align:center;'>불러오는 중...</div>";
      const html = await fetchText("/api/events_html");
      root.innerHTML = html;
    }catch(e){
      root.innerHTML = "<div style='padding:60px 24px; color:#c00; text-align:center;'>목록 로드 실패</div>";
    }
  }

  window.OSEYO.notifyMapRefresh = function(){
    const iframe = $("map_iframe");
    if(iframe && iframe.contentWindow){
      iframe.contentWindow.postMessage({type:"oseyo_refresh_map"}, "*");
    }
  }

  window.OSEYO.join = async function(eventId){
    const r = await fetch("/api/join_event", {
      method:"POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({event_id:eventId}),
      credentials:"same-origin"
    });
    const data = await r.json().catch(()=>({ok:false, message:"응답 파싱 실패"}));
    if(!r.ok || !data.ok){
      alert(data.message || "참여 실패");
      return;
    }
    await window.OSEYO.refreshExplore();
    window.OSEYO.notifyMapRefresh();
  }

  window.OSEYO.leave = async function(eventId){
    const r = await fetch("/api/leave_event", {
      method:"POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({event_id:eventId}),
      credentials:"same-origin"
    });
    const data = await r.json().catch(()=>({ok:false, message:"응답 파싱 실패"}));
    if(!r.ok || !data.ok){
      alert(data.message || "빠지기 실패");
      return;
    }
    await window.OSEYO.refreshExplore();
    window.OSEYO.notifyMapRefresh();
  }

  // iframe(map) -> parent(explore) 갱신 요청
  window.addEventListener("message", (e)=>{
    if(!e || !e.data) return;
    if(e.data.type === "oseyo_refresh_parent"){
      window.OSEYO.refreshExplore();
    }
  });

  // Gradio 버튼에 JS 핸들러 달기(새로고침 버튼)
  function hookRefreshBtn(){
    const b = $("refresh_btn");
    if(!b) return;
    // gradio는 내부에 button이 한 겹 더 있을 수 있음
    b.addEventListener("click", (ev)=>{
      try{ window.OSEYO.refreshExplore(); window.OSEYO.notifyMapRefresh(); }catch(e){}
    }, {capture:true});
  }

  // 신호값(숨김 인풋)이 바뀌면 목록/지도 갱신
  let lastSig = "";
  setInterval(()=>{
    const sig = $("js_signal");
    if(sig && sig.value !== lastSig){
      lastSig = sig.value;
      window.OSEYO.refreshExplore();
      window.OSEYO.notifyMapRefresh();
    }
  }, 700);

  // 최초 로드
  window.addEventListener("load", ()=>{
    hookRefreshBtn();
    window.OSEYO.refreshExplore();
  });
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
            refresh_btn = gr.Button("🔄 목록 새로고침", variant="secondary", size="sm", elem_id="refresh_btn")
            explore_html = gr.HTML("<div id='explore_root'></div>")
        with gr.Tab("지도"):
            gr.HTML('<iframe id="map_iframe" src="/map" style="width:100%;height:70vh;border:none;border-radius:16px;"></iframe>')

    with gr.Row(elem_classes=["fab-wrapper"]):
        fab = gr.Button("+")

    overlay = gr.HTML("<div class='overlay'></div>", visible=False)

    # ✅ JS 신호 (값 바뀌면 목록/지도 갱신)
    js_signal = gr.Textbox(value="", visible=False, elem_id="js_signal")

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

                with gr.Accordion("즐겨찾기 삭제", open=False):
                    fav_del_dd = gr.Dropdown(label="삭제할 즐겨찾기", choices=[], value=None, interactive=True)
                    fav_del_btn = gr.Button("선택 삭제", variant="stop")
                    fav_manage_msg = gr.Markdown("")

                gr.Markdown("---")

                t_in = gr.Textbox(label="이벤트명", placeholder="예: 30분 산책, 조용히 책 읽기", lines=1)

                with gr.Accordion("사진 추가 (선택)", open=False):
                    img_in = gr.Image(label="사진", type="numpy", height=200)

                with gr.Row():
                    s_in = gr.Textbox(label="시작", value=now_dt.strftime(DT_FMT))
                    e_in = gr.Textbox(label="종료", value=later_dt.strftime(DT_FMT))

                cap_in = gr.Number(label="정원(기본 10)", value=10, precision=0)

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

    def open_main_modal(request: gr.Request):
        my_events = get_my_events(request)
        favs = merged_favs_for_buttons(request, 10)
        my_choices = [x["name"] for x in get_my_favs(request, 30)]
        return (
            gr.update(visible=True),
            gr.update(visible=True),
            gr.update(choices=my_events, value=None),
            "",
            *fav_buttons_update(favs),
            "",
            gr.update(choices=my_choices, value=None),
            "",
        )

    fab.click(
        open_main_modal,
        None,
        [overlay, modal_m, my_event_list, del_msg] + fav_btns + [fav_msg, fav_del_dd, fav_manage_msg],
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
        outputs=[fav_msg, fav_del_dd] + fav_btns,
    )

    fav_del_btn.click(
        fn=delete_my_fav,
        inputs=[fav_del_dd],
        outputs=[fav_manage_msg, fav_del_dd] + fav_btns,
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

    def save_and_close(title, img, start, end, addr, cap, req: gr.Request):
        msg = save_data(title, img, start, end, addr, cap, req)
        # 성공/실패 메시지는 여기선 안 띄우고, JS signal만 갱신해서 목록/지도 갱신
        return gr.update(visible=False), gr.update(visible=False), uuid.uuid4().hex

    m_save.click(
        save_and_close,
        [t_in, img_in, s_in, e_in, selected_addr, cap_in],
        [overlay, modal_m, js_signal],
    )

    del_btn.click(
        delete_my_event,
        [my_event_list],
        [del_msg, my_event_list, js_signal],
    )


# =========================================================
# 7) FastAPI + 로그인/회원가입 + 이메일 OTP + API
# =========================================================
app = FastAPI()

PUBLIC_PATHS = {"/", "/login", "/signup", "/logout", "/health", "/send_email_otp"}

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path or "/"
    if path.startswith("/static") or path.startswith("/assets") or path in PUBLIC_PATHS:
        return await call_next(request)

    # Gradio assets under /app/* are protected by login
    token = request.cookies.get(COOKIE_NAME)
    u = get_user_by_token(token)

    if not u:
        # fetch api는 JSON으로
        if path.startswith("/api/"):
            return JSONResponse({"ok": False, "message": "로그인이 필요합니다."}, status_code=401)
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


# ✅ 회원가입 UI: 스샷 스타일 + 이메일(아이디/도메인 분리) + 비번확인/약관 UI (기존 기능 유지)
@app.get("/signup")
def signup_page():
    html_content = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>오세요 - 회원가입</title>
  <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
    body { font-family:Pretendard, system-ui; background:#fff; margin:0; padding:0; }
    .top { padding:22px 24px; }
    .brand { font-weight:900; font-size:18px; }
    .wrap { max-width:420px; margin:0 auto; padding:10px 18px 40px 18px; }
    .title { text-align:center; font-weight:900; font-size:18px; margin:18px 0 10px 0; }
    .sns { display:flex; justify-content:center; gap:14px; margin:16px 0 18px 0; }
    .sns .c { width:44px; height:44px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-weight:900; color:#fff; }
    .c.fb { background:#3b5998; } .c.kk { background:#FEE500; color:#111; } .c.nv { background:#03C75A; }
    .divider { height:1px; background:#eee; margin:16px 0; }
    label { display:block; font-size:12px; color:#333; font-weight:900; margin-top:14px; }
    .row { display:flex; gap:10px; align-items:center; }
    input, select { width:100%; padding:12px; border:1px solid #ddd; border-radius:6px; font-size:14px; box-sizing:border-box; }
    input:focus, select:focus { outline:none; border-color:#111; }
    .btn { width:100%; padding:13px; background:#111; color:#fff; border:none; border-radius:6px; cursor:pointer; font-weight:900; margin-top:14px; }
    .btn2 { padding:12px 12px; background:#f2f2f2; color:#111; border:none; border-radius:6px; cursor:pointer; font-weight:900; white-space:nowrap; }
    .msg { margin-top:10px; font-size:13px; color:#444; }
    .err { color:#c00; font-weight:900; }
    .ok { color:#0a7; font-weight:900; }
    .agreements { border:1px solid #eee; border-radius:8px; padding:12px; margin-top:14px; }
    .agreements .line { display:flex; align-items:center; gap:8px; margin:10px 0; font-size:13px; color:#333; }
    .agreements small { color:#777; }
    .hint { font-size:12px; color:#777; margin-top:6px; }
    .debug { background:#fff7cc; padding:10px; border-radius:8px; font-size:13px; margin-top:10px; display:none; }
    .foot { text-align:center; margin-top:14px; font-size:13px; color:#666; }
    .foot a { color:#333; text-decoration:underline; }
  </style>
</head>
<body>
  <div class="top">
    <div class="brand">오세요</div>
  </div>

  <div class="wrap">
    <div class="title">회원가입</div>
    <div style="text-align:center; font-size:12px; color:#777;">SNS계정으로 간편하게 회원가입</div>
    <div class="sns">
      <div class="c fb">f</div>
      <div class="c kk">톡</div>
      <div class="c nv">N</div>
    </div>

    <div class="divider"></div>

    <label>이메일</label>
    <div class="row">
      <input id="emailId" placeholder="아이디" />
      <span style="font-weight:900;color:#666;">@</span>
      <select id="emailDomainSel" onchange="onDomainChange()">
        <option value="">선택해주세요</option>
        <option value="gmail.com">gmail.com</option>
        <option value="naver.com">naver.com</option>
        <option value="daum.net">daum.net</option>
        <option value="_custom">직접입력</option>
      </select>
    </div>
    <input id="emailDomainCustom" placeholder="도메인 직접입력 (예: company.com)" style="display:none; margin-top:10px;" />
    <button class="btn2" type="button" onclick="sendOtp()" style="width:100%; margin-top:10px;">이메일 인증하기</button>

    <label style="margin-top:14px;">인증번호</label>
    <input id="otp" placeholder="인증번호 6자리" />
    <div id="otpMsg" class="msg"></div>
    <div id="debugBox" class="debug"></div>

    <form method="post" action="/signup" onsubmit="return beforeSubmit();">
      <input id="usernameHidden" name="username" type="hidden" />
      <input id="otpHidden" name="otp" type="hidden" />

      <label>비밀번호</label>
      <input id="pw1" name="password" type="password" placeholder="비밀번호" required />
      <div class="hint">영문/숫자 포함 8자 이상 권장</div>

      <label>비밀번호 확인</label>
      <input id="pw2" type="password" placeholder="비밀번호 확인" required />

      <!-- 기존 기능 유지: 이름/성별/생년월일 -->
      <label>이름</label>
      <input name="name" placeholder="이름" required />

      <div class="row">
        <div style="flex:1;">
          <label>성별</label>
          <select name="gender" required>
            <option value="">선택해주세요</option>
            <option value="F">여성</option>
            <option value="M">남성</option>
            <option value="N">선택안함</option>
          </select>
        </div>
        <div style="flex:1;">
          <label>생년월일</label>
          <input name="birth" type="date" required />
        </div>
      </div>

      <label>약관동의</label>
      <div class="agreements">
        <div class="line"><input type="checkbox" id="agreeAll" onchange="toggleAll()"> <b>전체동의</b> <small>(선택항목 포함)</small></div>
        <div class="divider"></div>
        <div class="line"><input type="checkbox" class="ag req" id="agAge"> 만 14세 이상입니다 <b style="color:#0a7;">(필수)</b></div>
        <div class="line"><input type="checkbox" class="ag req" id="agTerms"> 이용약관 동의 <b style="color:#0a7;">(필수)</b></div>
        <div class="line"><input type="checkbox" class="ag req" id="agPrivacy"> 개인정보 처리방침 동의 <b style="color:#0a7;">(필수)</b></div>
        <div class="line"><input type="checkbox" class="ag" id="agMarketing"> 마케팅 수신 동의 <small>(선택)</small></div>
      </div>

      <button class="btn" type="submit">회원가입하기</button>

      <div class="foot">
        이미 아이디가 있으신가요? <a href="/login">로그인</a>
      </div>
    </form>
  </div>

<script>
  function onDomainChange(){
    const sel = document.getElementById("emailDomainSel").value;
    const c = document.getElementById("emailDomainCustom");
    if(sel === "_custom"){
      c.style.display = "block";
    }else{
      c.style.display = "none";
      c.value = "";
    }
  }

  function buildEmail(){
    const id = (document.getElementById("emailId").value || "").trim();
    const sel = document.getElementById("emailDomainSel").value;
    const custom = (document.getElementById("emailDomainCustom").value || "").trim();
    let domain = sel;
    if(sel === "_custom") domain = custom;
    if(!id || !domain) return "";
    return (id + "@" + domain).trim();
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

  function toggleAll(){
    const all = document.getElementById("agreeAll").checked;
    document.querySelectorAll(".ag").forEach(x => x.checked = all);
  }

  function beforeSubmit() {
    const email = buildEmail();
    const otp = (document.getElementById("otp").value || "").trim();
    const pw1 = document.getElementById("pw1").value || "";
    const pw2 = document.getElementById("pw2").value || "";

    if(!email){
      alert("이메일을 완성해 주세요.");
      return false;
    }
    if(!otp){
      alert("인증번호를 입력해 주세요.");
      return false;
    }
    if(pw1 !== pw2){
      alert("비밀번호가 일치하지 않습니다.");
      return false;
    }

    // 필수 약관 체크
    const reqs = ["agAge","agTerms","agPrivacy"];
    for(const id of reqs){
      if(!document.getElementById(id).checked){
        alert("필수 약관에 동의해 주세요.");
        return false;
      }
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


# -----------------------------
# API: 이벤트 HTML/JSON
# -----------------------------
def fetch_events_rows():
    with db_conn() as con:
        rows = con.execute(
            """
            SELECT id, title, photo, start, end, addr, lat, lng, created_at, user_id, capacity
            FROM events
            ORDER BY created_at DESC
            """
        ).fetchall()
    return rows

@app.get("/api/events_json")
def api_events_json(request: Request):
    user = get_current_user_fastapi(request)
    user_id = user["id"] if user else None
    joined_id = get_joined_event_id(user_id) if user_id else None
    counts = get_event_counts()

    data = []
    for r in fetch_events_rows():
        eid, title, photo, start, end, addr, lat, lng, created_at, owner, cap = r
        cap = int(cap or 0) or 10
        c = counts.get(eid, 0)
        ended = is_ended(end)
        data.append({
            "id": eid,
            "title": title or "",
            "photo": photo or "",
            "start": start or "",
            "end": end or "",
            "addr": addr or "",
            "lat": float(lat or 0),
            "lng": float(lng or 0),
            "capacity": cap,
            "count": c,
            "ended": ended,
            "remain": remain_str(end),
            "is_joined": (joined_id == eid) if user_id else False,
            "joined_event_id": joined_id or "",
        })
    return JSONResponse({"ok": True, "events": data, "joined_event_id": joined_id or ""}, headers={"Cache-Control":"no-store"})

@app.get("/api/events_html")
def api_events_html(request: Request):
    user = get_current_user_fastapi(request)
    user_id = user["id"] if user else None
    joined_id = get_joined_event_id(user_id) if user_id else None
    counts = get_event_counts()

    rows = fetch_events_rows()

    # 참여중 카드
    sticky = "<div class='joined-sticky'><div class='joined-title'>참여중인 활동</div>"
    joined_row = next((r for r in rows if r[0] == joined_id), None) if joined_id else None
    if not joined_row:
        sticky += "<div class='joined-empty'>현재 참여중인 활동이 없습니다.</div></div>"
    else:
        eid, title, photo, start, end, addr, lat, lng, created_at, owner, cap = joined_row
        cap = int(cap or 0) or 10
        c = counts.get(eid, 0)
        ended = is_ended(end)
        remain = remain_str(end)

        img_html = f"<img class='event-photo' style='margin-bottom:10px;' src='data:image/jpeg;base64,{photo}' />" if photo else \
                   "<div class='event-photo' style='display:flex;align-items:center;justify-content:center;color:#ccc;margin-bottom:10px;'>NO IMAGE</div>"

        # 버튼
        btn = ""
        if not ended:
            btn = f"""
              <button class="join-btn danger" onclick="OSEYO.leave('{eid}')">빠지기</button>
            """
        sticky += f"""
          <div class="event-card {'event-ended' if ended else ''}" style="margin-bottom:0;">
            {img_html}
            <div class="event-info">
              <div class="event-title">{safe(title)}</div>
              <div class="event-meta">⏰ {safe(start)} <span style="color:#999;">· {safe(remain)}</span></div>
              <div class="event-meta">📍 {safe(addr or "장소 미정")}</div>
              <div class="join-row">
                <div class="join-count">👥 {c}/{cap}</div>
                {btn}
              </div>
              {"<div class='badge-ended'>종료됨</div>" if ended else ""}
            </div>
          </div>
        </div>
        """

    # 목록
    if not rows:
        return HTMLResponse(sticky + "<div style='text-align:center; padding:60px 20px; color:#999;'>등록된 이벤트가 없습니다.<br>오른쪽 아래 버튼을 눌러 시작해보세요.</div>")

    out = sticky + "<div style='padding:0 24px 80px 24px;'>"

    for r in rows:
        eid, title, photo, start, end, addr, lat, lng, created_at, owner, cap = r
        cap = int(cap or 0) or 10
        c = counts.get(eid, 0)
        ended = is_ended(end)
        remain = remain_str(end)
        is_joined = (joined_id == eid) if user_id else False

        img_html = f"<img class='event-photo' src='data:image/jpeg;base64,{photo}' />" if photo else \
                   "<div class='event-photo' style='display:flex;align-items:center;justify-content:center;color:#ccc;'>NO IMAGE</div>"

        # 버튼 상태
        btn_html = ""
        if not ended:
            if is_joined:
                btn_html = f'<button class="join-btn danger" onclick="OSEYO.leave(\'{eid}\')">빠지기</button>'
            else:
                if joined_id and joined_id != eid:
                    btn_html = '<button class="join-btn gray" disabled>다른 활동 참여중</button>'
                elif c >= cap:
                    btn_html = '<button class="join-btn gray" disabled>정원마감</button>'
                else:
                    btn_html = f'<button class="join-btn primary" onclick="OSEYO.join(\'{eid}\')">참여하기</button>'

        out += f"""
        <div class='event-card {"event-ended" if ended else ""}'>
          {img_html}
          <div class='event-info'>
            <div class='event-title'>{safe(title)}</div>
            <div class='event-meta'>⏰ {safe(start)} <span style="color:#999;">· {safe(remain)}</span></div>
            <div class='event-meta'>📍 {safe(addr or "장소 미정")}</div>
            <div class='join-row'>
              <div class='join-count'>👥 {c}/{cap}</div>
              {btn_html}
            </div>
            {"<div class='badge-ended'>종료됨</div>" if ended else ""}
          </div>
        </div>
        """

    out += "</div>"
    return HTMLResponse(out, headers={"Cache-Control":"no-store"})


# -----------------------------
# API: 참여/빠지기
# -----------------------------
@app.post("/api/join_event")
async def api_join_event(request: Request):
    user = get_current_user_fastapi(request)
    if not user:
        return JSONResponse({"ok": False, "message": "로그인이 필요합니다."}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    event_id = (payload.get("event_id") or "").strip()
    if not event_id:
        return JSONResponse({"ok": False, "message": "이벤트 id가 없습니다."}, status_code=400)

    with db_conn() as con:
        ev = con.execute(
            "SELECT id, end, capacity FROM events WHERE id=?",
            (event_id,),
        ).fetchone()
        if not ev:
            return JSONResponse({"ok": False, "message": "이벤트를 찾을 수 없습니다."}, status_code=404)

        _id, end, cap = ev
        if is_ended(end):
            return JSONResponse({"ok": False, "message": "이미 종료된 이벤트입니다."}, status_code=400)

        cap = int(cap or 0) or 10
        c = con.execute("SELECT COUNT(*) FROM event_members WHERE event_id=?", (event_id,)).fetchone()[0]
        if c >= cap:
            return JSONResponse({"ok": False, "message": "정원이 가득 찼습니다."}, status_code=400)

        # 이미 다른 이벤트 참여중인지
        row = con.execute("SELECT event_id FROM event_members WHERE user_id=?", (user["id"],)).fetchone()
        if row and row[0] != event_id:
            return JSONResponse({"ok": False, "message": "이미 다른 활동에 참여중입니다. 먼저 빠지기 해주세요."}, status_code=400)

        # 참여(이미 참여면 OK)
        con.execute(
            """
            INSERT INTO event_members (event_id, user_id, joined_at)
            VALUES (?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              event_id=excluded.event_id,
              joined_at=excluded.joined_at
            """,
            (event_id, user["id"], now_kst().isoformat(timespec="seconds")),
        )
        con.commit()

    return JSONResponse({"ok": True, "message": "참여했습니다."}, headers={"Cache-Control":"no-store"})

@app.post("/api/leave_event")
async def api_leave_event(request: Request):
    user = get_current_user_fastapi(request)
    if not user:
        return JSONResponse({"ok": False, "message": "로그인이 필요합니다."}, status_code=401)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    event_id = (payload.get("event_id") or "").strip()
    if not event_id:
        return JSONResponse({"ok": False, "message": "이벤트 id가 없습니다."}, status_code=400)

    with db_conn() as con:
        con.execute("DELETE FROM event_members WHERE user_id=? AND event_id=?", (user["id"], event_id))
        con.commit()

    return JSONResponse({"ok": True, "message": "빠졌습니다."}, headers={"Cache-Control":"no-store"})


# =========================================================
# 8) Map (Kakao 지도) - ✅ 버튼 갱신/동기화/인포윈도우 유지
# =========================================================
MAP_TEMPLATE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
    body{margin:0; font-family:Pretendard, system-ui;}
    #bar{
      position:fixed; top:0; left:0; right:0; z-index:9999;
      background:#fff; border-bottom:1px solid #eee;
      padding:12px 12px;
    }
    #bar h3{margin:0 0 8px 0; font-size:14px; font-weight:900;}
    #bar .empty{
      font-size:13px; color:#777; background:#fafafa; border:1px solid #eee;
      padding:10px 12px; border-radius:12px;
    }
    #bar .card{
      border:1px solid #eee; border-radius:12px; padding:10px 12px;
      display:flex; justify-content:space-between; align-items:center; gap:10px;
      box-shadow:0 4px 12px rgba(0,0,0,0.05);
    }
    #bar .meta{font-size:12px; color:#666; margin-top:4px;}
    #bar .btn{
      padding:10px 12px; border:none; border-radius:10px; font-weight:900; cursor:pointer;
      background:#ffe8e8; color:#b00020;
      white-space:nowrap;
    }
    #m{width:100%; height:100vh; padding-top:92px; box-sizing:border-box;}
    .iw{padding:10px; width:220px;}
    .iw-title{font-weight:900; font-size:14px;}
    .iw-meta{font-size:12px; margin-top:4px; color:#666;}
    .iw-img{width:100%; height:110px; object-fit:cover; border-radius:8px; margin-top:8px; border:1px solid #eee;}
    .iw-row{display:flex; justify-content:space-between; align-items:center; margin-top:10px; gap:10px;}
    .iw-count{font-size:12px; color:#555;}
    .iw-btn{padding:8px 10px; border:none; border-radius:10px; font-weight:900; cursor:pointer;}
    .iw-btn.primary{background:#111; color:#fff;}
    .iw-btn.gray{background:#f0f0f0; color:#333; cursor:not-allowed;}
    .iw-btn.danger{background:#ffe8e8; color:#b00020;}
    .ended{opacity:.65; filter:grayscale(1);}
  </style>
</head>
<body>
  <div id="bar">
    <h3>참여중인 활동</h3>
    <div id="barContent" class="empty">현재 참여중인 활동이 없습니다.</div>
  </div>

  <div id="m"></div>

  <script>
    const KAKAO_KEY = "__KAKAO_JS_KEY__";
    if(!KAKAO_KEY){
      document.getElementById("m").innerHTML = "<div style='padding:40px; text-align:center; color:#c00;'>KAKAO_JAVASCRIPT_KEY가 필요합니다.</div>";
    }
  </script>

  <script src="//dapi.kakao.com/v2/maps/sdk.js?appkey=__KAKAO_JS_KEY__"></script>
  <script>
    function esc(s){
      return String(s||"")
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;")
        .replaceAll("'","&#039;");
    }

    let map = null;
    let markers = [];
    let openIw = null;
    let openEventId = "";
    let openMarker = null;

    async function apiJson(url, body){
      const r = await fetch(url, {
        method:"POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify(body||{}),
        credentials:"same-origin"
      });
      const data = await r.json().catch(()=>({ok:false, message:"응답 파싱 실패"}));
      return {ok:r.ok && data.ok, data};
    }

    function setMarkers(events, joinedId){
      // 기존 마커 제거
      markers.forEach(m => m.setMap(null));
      markers = [];

      // 지도 없으면 생성
      if(!map){
        map = new kakao.maps.Map(document.getElementById('m'), {
          center: new kakao.maps.LatLng(36.019, 129.343),
          level: 7
        });
      }

      // 참여중 bar
      const bar = document.getElementById("barContent");
      const joined = events.find(e => e.id === joinedId);
      if(!joined){
        bar.className = "empty";
        bar.innerHTML = "현재 참여중인 활동이 없습니다.";
      }else{
        bar.className = "card" + (joined.ended ? " ended" : "");
        const btn = (!joined.ended) ? `<button class="btn" onclick="leaveEvent('${joined.id}')">빠지기</button>` : "";
        bar.innerHTML = `
          <div style="flex:1;">
            <div style="font-weight:900;">${esc(joined.title)}</div>
            <div class="meta">⏰ ${esc(joined.start)} · ${esc(joined.remain)}</div>
            <div class="meta">👥 ${joined.count}/${joined.capacity}</div>
          </div>
          ${btn}
        `;
      }

      events.forEach(e => {
        if(!e.lat || !e.lng) return;

        const marker = new kakao.maps.Marker({
          position: new kakao.maps.LatLng(e.lat, e.lng),
          map: map
        });
        markers.push(marker);

        kakao.maps.event.addListener(marker, 'click', () => {
          openEventId = e.id;
          openMarker = marker;
          renderInfo(e, joinedId);
        });
      });

      // 열려있던 인포윈도우 유지(가능하면)
      if(openEventId){
        const cur = events.find(e => e.id === openEventId);
        if(cur && openMarker){
          renderInfo(cur, joinedId);
        }
      }
    }

    function renderInfo(e, joinedId){
      if(openIw) openIw.close();

      const title = esc(e.title);
      const addr = esc(e.addr);
      const start = esc(e.start);
      const remain = esc(e.remain);
      const img = e.photo ? `<img class="iw-img" src="data:image/jpeg;base64,${e.photo}">` : "";

      let btn = "";
      if(!e.ended){
        if(e.is_joined){
          btn = `<button class="iw-btn danger" onclick="leaveEvent('${e.id}')">빠지기</button>`;
        }else if(joinedId && joinedId !== e.id){
          btn = `<button class="iw-btn gray" disabled>다른 활동 참여중</button>`;
        }else if(e.count >= e.capacity){
          btn = `<button class="iw-btn gray" disabled>정원마감</button>`;
        }else{
          btn = `<button class="iw-btn primary" onclick="joinEvent('${e.id}')">참여하기</button>`;
        }
      }

      const content = `
        <div class="iw ${e.ended ? "ended" : ""}">
          <div class="iw-title">${title}</div>
          <div class="iw-meta">⏰ ${start} · ${remain}</div>
          <div class="iw-meta">📍 ${addr}</div>
          ${img}
          <div class="iw-row">
            <div class="iw-count">👥 ${e.count}/${e.capacity}</div>
            ${btn}
          </div>
        </div>
      `;

      openIw = new kakao.maps.InfoWindow({ content, removable: true });
      openIw.open(map, openMarker);
    }

    async function refresh(){
      const r = await fetch("/api/events_json", {cache:"no-store", credentials:"same-origin"});
      const j = await r.json().catch(()=>({ok:false}));
      if(!j.ok) return;
      setMarkers(j.events || [], j.joined_event_id || "");
    }

    async function joinEvent(id){
      const res = await apiJson("/api/join_event", {event_id:id});
      if(!res.ok){ alert(res.data.message || "참여 실패"); return; }
      // ✅ 인포윈도우 닫지 않고 내용만 갱신: refresh 후 renderInfo가 다시 세팅
      await refresh();
      parent.postMessage({type:"oseyo_refresh_parent"}, "*");
    }

    async function leaveEvent(id){
      const res = await apiJson("/api/leave_event", {event_id:id});
      if(!res.ok){ alert(res.data.message || "빠지기 실패"); return; }
      await refresh();
      parent.postMessage({type:"oseyo_refresh_parent"}, "*");
    }

    // parent -> map 갱신
    window.addEventListener("message", (e)=>{
      if(!e || !e.data) return;
      if(e.data.type === "oseyo_refresh_map"){
        refresh();
      }
    });

    window.addEventListener("load", refresh);
  </script>
</body>
</html>
"""

@app.get("/map")
def map_h():
    page = MAP_TEMPLATE.replace("__KAKAO_JS_KEY__", KAKAO_JAVASCRIPT_KEY or "")
    return HTMLResponse(page, headers={"Cache-Control":"no-store"})


# =========================================================
# 9) Gradio 마운트
# =========================================================
app = gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
