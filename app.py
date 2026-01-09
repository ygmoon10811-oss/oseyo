# -*- coding: utf-8 -*-
import os
import uuid
import base64
import io
import sqlite3
import json
import html as html_mod
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
SMTP_PORT = int((os.getenv("SMTP_PORT", "465").strip() or "465"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM = os.getenv("SMTP_FROM", "").strip()  # "오세요 <me@gmail.com>" 형태 가능


# =========================================================
# 1) 환경/DB (기존 DB 파일 유지 + 마이그레이션)
# =========================================================
def pick_db_path():
    """
    ✅ 기존 계정/이벤트가 안 보이던 이유가 'DB 파일명이 바뀌어서 새 DB를 봤기 때문'인 경우가 많다.
    그래서 /var/data 또는 /tmp에 이미 존재하는 DB 파일을 우선 사용한다.
    """
    candidates_dirs = ["/var/data", "/tmp"]
    known_names = [
        "oseyo_final.db",
        "oseyo_final_email_v1.db",
        "oseyo.db",
    ]

    # 1) 기존 DB가 있으면 그걸 사용
    for d in candidates_dirs:
        try:
            if not os.path.isdir(d):
                continue
            for nm in known_names:
                p = os.path.join(d, nm)
                if os.path.exists(p) and os.path.getsize(p) > 0:
                    return p
        except Exception:
            pass

    # 2) 없으면, 쓸 수 있는 경로에 새로 생성
    for d in candidates_dirs:
        try:
            os.makedirs(d, exist_ok=True)
            test = os.path.join(d, ".writetest")
            with open(test, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test)
            return os.path.join(d, "oseyo.db")
        except Exception:
            continue

    return "/tmp/oseyo.db"


DB_PATH = pick_db_path()
print(f"[DB] Using: {DB_PATH}")

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def col_exists(con, table, col):
    try:
        rows = con.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == col for r in rows)
    except Exception:
        return False

def init_db():
    with db_conn() as con:
        # 이벤트 (가능한 한 기존 테이블 구조 유지)
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
        # 구형 DB 마이그레이션
        if not col_exists(con, "events", "user_id"):
            try:
                con.execute("ALTER TABLE events ADD COLUMN user_id TEXT")
            except Exception:
                pass

        # 정원(capacity) 추가 (구형 DB에도 안전)
        if not col_exists(con, "events", "capacity"):
            try:
                con.execute("ALTER TABLE events ADD COLUMN capacity INTEGER DEFAULT 0")
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
        if not col_exists(con, "favs", "updated_at"):
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
            "ALTER TABLE users ADD COLUMN created_at TEXT",
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

        # 참여(1인 1활동 제한을 위해 user_id를 PK로 둔다)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS participants (
                user_id TEXT PRIMARY KEY,
                event_id TEXT,
                joined_at TEXT
            );
            """
        )

        con.commit()

init_db()


# =========================================================
# 2) 날짜/시간 유틸
# =========================================================
def parse_dt_any(s: str):
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None

    fmts = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S.%f",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=KST)
            return dt.astimezone(KST)
        except Exception:
            continue

    # 마지막 fallback: "2026-01-09 16:00:00+09:00" 같은 형태
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        return None

def is_active_event(end_str: str):
    """
    end가 없거나 파싱 불가면 '활성'로 간주(사용자 요구: 진행중 이벤트가 탐색탭에서 안 사라지게).
    """
    if not end_str:
        return True
    end_dt = parse_dt_any(end_str)
    if not end_dt:
        return True
    return end_dt >= now_kst()

def human_left(end_str: str):
    end_dt = parse_dt_any(end_str)
    if not end_dt:
        return ""
    td = end_dt - now_kst()
    sec = int(td.total_seconds())
    if sec <= 0:
        return "종료"
    days = sec // 86400
    sec %= 86400
    hours = sec // 3600
    sec %= 3600
    mins = sec // 60

    parts = []
    if days > 0:
        parts.append(f"{days}일")
    if hours > 0:
        parts.append(f"{hours}시간")
    if mins > 0:
        parts.append(f"{mins}분")
    if not parts:
        parts.append("1분 미만")
    return "남음 " + " ".join(parts)

def fmt_start(s: str):
    dt = parse_dt_any(s)
    if not dt:
        return (s or "")
    return dt.strftime("%m월 %d일 %H:%M")


# =========================================================
# 3) 비밀번호/세션 유틸
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

def get_current_user_api(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    return get_user_by_token(token)


# =========================================================
# 4) 이메일 OTP 유틸
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
# 5) 즐겨찾기 로직 (삭제 추가)
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

def get_all_fav_names(limit=200):
    with db_conn() as con:
        rows = con.execute(
            """
            SELECT name FROM favs
            WHERE name IS NOT NULL AND TRIM(name) != ''
            ORDER BY count DESC, updated_at DESC
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

def fav_del_dropdown_update():
    names = get_all_fav_names(200)
    return gr.update(choices=names, value=None)

def add_fav_only(name: str, request: gr.Request):
    user = get_current_user_gr(request)
    if not user:
        favs = get_top_favs(10)
        return "로그인이 필요합니다.", *fav_buttons_update(favs), fav_del_dropdown_update()

    name = (name or "").strip()
    if not name:
        favs = get_top_favs(10)
        return "활동명을 입력해 주세요.", *fav_buttons_update(favs), fav_del_dropdown_update()

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
    return "✅ 즐겨찾기에 추가되었습니다.", *fav_buttons_update(favs), fav_del_dropdown_update()

def delete_fav(name: str, request: gr.Request):
    user = get_current_user_gr(request)
    if not user:
        favs = get_top_favs(10)
        return "로그인이 필요합니다.", *fav_buttons_update(favs), fav_del_dropdown_update()

    name = (name or "").strip()
    if not name:
        favs = get_top_favs(10)
        return "삭제할 즐겨찾기를 선택해 주세요.", *fav_buttons_update(favs), fav_del_dropdown_update()

    with db_conn() as con:
        con.execute("DELETE FROM favs WHERE name = ?", (name,))
        con.commit()

    favs = get_top_favs(10)
    return "✅ 즐겨찾기를 삭제했습니다.", *fav_buttons_update(favs), fav_del_dropdown_update()


# =========================================================
# 6) 이벤트 생성/삭제 (Gradio용)
# =========================================================
def save_data(title, img, start, end, addr_obj, capacity, request: gr.Request):
    user = get_current_user_gr(request)
    if not user:
        return False, "로그인이 필요합니다."

    title = (title or "").strip()
    if not title:
        return False, "제목을 입력해 주세요."

    # capacity
    try:
        cap = int(capacity) if capacity is not None else 0
        if cap < 0:
            cap = 0
    except Exception:
        cap = 0

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
        with db_conn() as con:
            con.execute(
                """
                INSERT INTO events (id, title, photo, start, end, addr, lat, lng, created_at, user_id, capacity)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    uuid.uuid4().hex[:10],
                    title,
                    pic_b64,
                    (start or "").strip(),
                    (end or "").strip(),
                    addr_name,
                    lat,
                    lng,
                    now_kst().isoformat(timespec="seconds"),
                    user["id"],
                    cap,
                ),
            )

            # 즐겨찾기 count up
            con.execute(
                """
                INSERT INTO favs (name, count, updated_at) VALUES (?, 1, ?)
                ON CONFLICT(name) DO UPDATE SET count = count + 1, updated_at=excluded.updated_at
                """,
                (title, now_kst().isoformat(timespec="seconds")),
            )
            con.commit()
        return True, "✅ 이벤트가 생성되었습니다."
    except Exception as e:
        return False, f"❌ 이벤트 생성 실패: {html_mod.escape(str(e))}"

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
        return "삭제할 이벤트를 선택해주세요.", gr.update()

    with db_conn() as con:
        con.execute("DELETE FROM events WHERE id = ? AND user_id = ?", (event_id, user["id"]))
        # 참여 중인 사람도 정리
        con.execute("DELETE FROM participants WHERE event_id = ?", (event_id,))
        con.commit()

    new_list = get_my_events(request)
    return "✅ 삭제되었습니다.", gr.update(choices=new_list, value=None)


# =========================================================
# 7) 참여 로직(API용) + 정리
# =========================================================
def cleanup_participants():
    """
    종료된 이벤트에 묶인 참여는 자동 해제한다.
    """
    with db_conn() as con:
        rows = con.execute("SELECT user_id, event_id FROM participants").fetchall()
        for uid, eid in rows:
            ev = con.execute("SELECT end FROM events WHERE id = ?", (eid,)).fetchone()
            if not ev:
                con.execute("DELETE FROM participants WHERE user_id = ?", (uid,))
                continue
            end_str = (ev[0] or "")
            if not is_active_event(end_str):
                con.execute("DELETE FROM participants WHERE user_id = ?", (uid,))
        con.commit()

def get_participant_counts():
    with db_conn() as con:
        rows = con.execute(
            "SELECT event_id, COUNT(*) FROM participants GROUP BY event_id"
        ).fetchall()
    return {r[0]: int(r[1]) for r in rows}

def get_user_joined_event_id(user_id: str):
    cleanup_participants()
    with db_conn() as con:
        row = con.execute("SELECT event_id FROM participants WHERE user_id = ?", (user_id,)).fetchone()
    return row[0] if row else None

def get_events_for_api(user_id: str):
    """
    종료된 이벤트는 제외하여 탐색/지도 모두 일관되게 처리한다.
    """
    cleanup_participants()
    joined_id = get_user_joined_event_id(user_id)
    counts = get_participant_counts()

    with db_conn() as con:
        rows = con.execute(
            """
            SELECT id, title, photo, start, end, addr, lat, lng, created_at, user_id, COALESCE(capacity,0)
            FROM events
            ORDER BY created_at DESC
            """
        ).fetchall()

    events = []
    joined_event = None
    for r in rows:
        eid, title, photo, start, end, addr, lat, lng, created_at, owner_id, cap = r
        if not is_active_event(end):
            continue

        c = counts.get(eid, 0)
        cap = int(cap or 0)
        cap_text = "∞" if cap <= 0 else str(cap)

        ev = {
            "id": eid,
            "title": title or "",
            "photo": photo or "",
            "start": start or "",
            "end": end or "",
            "addr": addr or "",
            "lat": lat or 0,
            "lng": lng or 0,
            "created_at": created_at or "",
            "owner_id": owner_id or "",
            "capacity": cap,
            "capacity_text": cap_text,
            "count": c,
            "joined_by_me": (eid == joined_id),
            "time_left": human_left(end),
            "start_text": fmt_start(start),
        }
        events.append(ev)
        if ev["joined_by_me"]:
            joined_event = ev

    return joined_event, events

def join_event_api(user_id: str, event_id: str):
    cleanup_participants()
    with db_conn() as con:
        ev = con.execute(
            "SELECT id, end, COALESCE(capacity,0) FROM events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if not ev:
            return False, "이벤트를 찾을 수 없습니다.", None

        _, end_str, cap = ev
        if not is_active_event(end_str):
            return False, "이미 종료된 이벤트입니다.", None

        # 이미 다른 이벤트 참여 중인지
        row = con.execute("SELECT event_id FROM participants WHERE user_id = ?", (user_id,)).fetchone()
        if row and row[0] and row[0] != event_id:
            return False, "이미 다른 활동에 참여중입니다. (빠지기 후 참여 가능합니다)", row[0]

        # 정원 체크
        cap = int(cap or 0)
        if cap > 0:
            cnt = con.execute("SELECT COUNT(*) FROM participants WHERE event_id = ?", (event_id,)).fetchone()[0]
            if int(cnt) >= cap:
                return False, "정원이 가득 찼습니다.", None

        # 참여 등록(동일 이벤트는 upsert)
        con.execute(
            """
            INSERT INTO participants (user_id, event_id, joined_at)
            VALUES (?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                event_id=excluded.event_id,
                joined_at=excluded.joined_at
            """,
            (user_id, event_id, now_kst().isoformat(timespec="seconds")),
        )
        con.commit()
    return True, "✅ 참여했습니다.", None

def leave_event_api(user_id: str, event_id: str):
    cleanup_participants()
    with db_conn() as con:
        row = con.execute("SELECT event_id FROM participants WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return False, "현재 참여중인 활동이 없습니다."
        if row[0] != event_id:
            return False, "이 활동에 참여중이 아닙니다."
        con.execute("DELETE FROM participants WHERE user_id = ?", (user_id,))
        con.commit()
    return True, "✅ 빠졌습니다."


# =========================================================
# 8) Gradio UI (탐색/지도는 iframe으로 분리)
# =========================================================
CSS = """
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');

html, body {
  margin: 0 !important; padding: 0 !important;
  font-family: Pretendard, -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif !important;
  background-color: #ffffff !important;
}
.gradio-container { max-width: 100% !important; padding: 0 !important; margin: 0 !important; }

/* ✅ 웹에서는 너무 넓게 퍼지지 않도록(모바일 느낌 유지) */
@media (min-width: 900px) {
  .gradio-container { max-width: 560px !important; margin: 0 auto !important; }
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

.iframe-wrap { padding: 0 16px 90px 16px; }
.iframe-wrap iframe {
  width: 100%;
  height: 74vh;
  border: none;
  border-radius: 16px;
  background: #fff;
}
"""

now_dt = now_kst()
later_dt = now_dt + timedelta(hours=2)

with gr.Blocks(css=CSS, title="오세요") as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    # ✅ 부모(Gradio)에서 메시지 받아 두 iframe 모두 갱신
    gr.HTML("""
<script>
  function __refresh_iframe(id){
    const f = document.getElementById(id);
    if(!f || !f.contentWindow) return;
    f.contentWindow.postMessage({type:"REFRESH"}, "*");
  }
  function __refresh_all(){
    __refresh_iframe("explore_iframe");
    __refresh_iframe("map_iframe");
  }
  window.addEventListener("message", (e) => {
    if(!e || !e.data) return;
    if(e.data.type === "REFRESH_ALL"){
      __refresh_all();
    }
  });
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
            gr.HTML("""
              <div class="iframe-wrap">
                <iframe id="explore_iframe" src="/explore"></iframe>
              </div>
            """)
        with gr.Tab("지도"):
            gr.HTML("""
              <div class="iframe-wrap">
                <iframe id="map_iframe" src="/map"></iframe>
              </div>
            """)

    with gr.Row(elem_classes=["fab-wrapper"]):
        fab = gr.Button("+")

    overlay = gr.HTML("<div class='overlay'></div>", visible=False)
    js_poke = gr.HTML("", visible=False)  # ✅ 저장/삭제 후 iframe 갱신용 스크립트 주입

    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_m:
        gr.HTML("<div class='modal-header'>새 이벤트 만들기</div>")

        create_msg = gr.Markdown("")

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
                    fav_del_dd = gr.Dropdown(label="즐겨찾기 삭제", choices=[], interactive=True)
                    fav_del_btn = gr.Button("삭제", variant="stop")
                fav_msg = gr.Markdown("")
                gr.Markdown("---")

                t_in = gr.Textbox(label="이벤트명", placeholder="예: 30분 산책, 조용히 책 읽기", lines=1)

                with gr.Accordion("사진 추가 (선택)", open=False):
                    img_in = gr.Image(label="사진", type="numpy", height=200)

                with gr.Row():
                    s_in = gr.Textbox(label="시작(예: 2026-01-09 16:00)", value=now_dt.strftime("%Y-%m-%d %H:%M"))
                    e_in = gr.Textbox(label="종료(예: 2026-01-09 18:00)", value=later_dt.strftime("%Y-%m-%d %H:%M"))

                # 정원 스텝퍼
                with gr.Row():
                    cap_minus = gr.Button("−", variant="secondary")
                    cap_in = gr.Number(label="정원(0=무제한)", value=0, precision=0)
                    cap_plus = gr.Button("+", variant="secondary")

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
        favs = get_top_favs(10)
        return (
            gr.update(visible=True),
            gr.update(visible=True),
            gr.update(choices=my_events, value=None),
            "",
            "",
            *fav_buttons_update(favs),
            "",
            fav_del_dropdown_update(),
            ""
        )

    fab.click(
        open_main_modal,
        None,
        [overlay, modal_m, my_event_list, del_msg, create_msg] + fav_btns + [fav_msg, fav_del_dd, js_poke],
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
        outputs=[fav_msg] + fav_btns + [fav_del_dd],
    )

    fav_del_btn.click(
        fn=delete_fav,
        inputs=[fav_del_dd],
        outputs=[fav_msg] + fav_btns + [fav_del_dd],
    )

    def cap_dec(x):
        try:
            v = int(x or 0)
        except Exception:
            v = 0
        v = max(0, v - 1)
        return gr.update(value=v)

    def cap_inc(x):
        try:
            v = int(x or 0)
        except Exception:
            v = 0
        v = v + 1
        return gr.update(value=v)

    cap_minus.click(cap_dec, inputs=cap_in, outputs=cap_in)
    cap_plus.click(cap_inc, inputs=cap_in, outputs=cap_in)

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
        try:
            docs = res.json().get("documents", [])
        except Exception:
            docs = []

        for d in docs:
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

    def poke_refresh_script():
        # ✅ iframe 내부에서 REFRESH 메시지를 받아 fetch로 즉시 갱신
        return gr.update(value=f"""
<script>
  (function(){{
    try {{
      const f1 = document.getElementById("explore_iframe");
      const f2 = document.getElementById("map_iframe");
      if(f1 && f1.contentWindow) f1.contentWindow.postMessage({{type:"REFRESH"}}, "*");
      if(f2 && f2.contentWindow) f2.contentWindow.postMessage({{type:"REFRESH"}}, "*");
    }} catch(e) {{}}
  }})();
</script>
""")

    def save_and_close(title, img, start, end, addr, cap, req: gr.Request):
        ok, msg = save_data(title, img, start, end, addr, cap, req)
        favs = get_top_favs(10)
        if ok:
            return (
                msg,
                gr.update(visible=False),
                gr.update(visible=False),
                *fav_buttons_update(favs),
                fav_del_dropdown_update(),
                poke_refresh_script(),
            )
        else:
            return (
                msg,
                gr.update(visible=True),
                gr.update(visible=True),
                *fav_buttons_update(favs),
                fav_del_dropdown_update(),
                gr.update(value=""),
            )

    m_save.click(
        save_and_close,
        [t_in, img_in, s_in, e_in, selected_addr, cap_in],
        [create_msg, overlay, modal_m] + fav_btns + [fav_del_dd, js_poke],
    )

    def delete_and_refresh(event_id, req: gr.Request):
        msg, dd_up = delete_my_event(event_id, req)
        return msg, dd_up, poke_refresh_script()

    del_btn.click(
        delete_and_refresh,
        [my_event_list],
        [del_msg, my_event_list, js_poke],
    )


# =========================================================
# 9) FastAPI + 로그인/회원가입 + API + 탐색/지도 페이지
# =========================================================
app = FastAPI()

PUBLIC_PATHS = {"/", "/login", "/signup", "/logout", "/health", "/send_email_otp"}

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path or "/"

    # 정적 리소스
    if path.startswith("/static") or path.startswith("/assets"):
        return await call_next(request)

    # 공개 경로
    if path in PUBLIC_PATHS:
        return await call_next(request)

    # 나머지는 로그인 필요
    token = request.cookies.get(COOKIE_NAME)
    user = get_user_by_token(token)

    # API는 401
    if path.startswith("/api/"):
        if not user:
            return JSONResponse({"ok": False, "message": "로그인이 필요합니다."}, status_code=401)
        return await call_next(request)

    # HTML(탐색/지도/앱)은 로그인 없으면 로그인으로
    if not user:
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


# -----------------------------
# 로그인
# -----------------------------
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
    input { width:100%; padding:14px; margin-bottom:10px; border:1px solid #ddd; border-radius:10px; box-sizing:border-box; font-size:15px; }
    input:focus { outline:none; border-color:#333; }
    .login-btn { width:100%; padding:15px; border-radius:12px; border:none; background:#111; color:white;
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


# -----------------------------
# 이메일 OTP 발송
# -----------------------------
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


# -----------------------------
# 회원가입 (이메일 아이디/도메인 분리 + 약관 정렬 개선)
# -----------------------------
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
    body { font-family:Pretendard, system-ui; background:#fff; margin:0; padding:0;
      display:flex; justify-content:center; align-items:flex-start; min-height:100vh; }
    .wrap { width:100%; max-width:520px; padding:24px 18px 40px 18px; }
    h2 { margin:0 0 14px 0; font-size:22px; }
    .muted { color:#777; font-size:13px; margin-bottom:18px; }

    .label { font-size:13px; color:#111; font-weight:700; margin:14px 0 6px; }
    .row { display:flex; gap:10px; align-items:center; }
    .row > * { flex:1; }

    input, select {
      width:100%; padding:12px 12px; border:1px solid #ddd; border-radius:10px;
      box-sizing:border-box; font-size:14px; background:#fff;
    }
    input:focus, select:focus { outline:none; border-color:#111; }

    .at { flex:0 0 auto; color:#666; font-weight:700; }

    .btn-wide {
      width:100%; padding:12px; border-radius:10px;
      border:1px solid #ddd; background:#f7f7f7; color:#111;
      font-weight:800; cursor:pointer; margin-top:10px;
    }
    .btn { width:100%; padding:14px; background:#111; color:#fff; border:none; border-radius:12px;
      cursor:pointer; font-weight:800; margin-top:14px; }

    .msg { margin-top:10px; font-size:13px; color:#444; }
    .err { color:#c00; }
    .ok { color:#0a7; }
    a { color:#333; }

    /* 약관 박스 */
    .terms-box {
      border:1px solid #eee; border-radius:14px; padding:14px 14px; margin-top:10px;
      background:#fff;
    }
    .terms-head {
      display:flex; justify-content:space-between; align-items:center; padding-bottom:10px; border-bottom:1px solid #f2f2f2;
      font-weight:800; font-size:14px;
    }
    .terms-item {
      display:flex; justify-content:space-between; align-items:center;
      padding:12px 0; border-bottom:1px solid #f6f6f6;
      font-size:14px;
    }
    .terms-item:last-child{ border-bottom:none; }
    .left {
      display:flex; align-items:center; gap:10px;
    }
    .right {
      display:flex; align-items:center; gap:10px;
      color:#777; font-size:12px;
    }
    .tag {
      font-size:12px; font-weight:800;
      padding:3px 8px; border-radius:999px;
      background:#f1f1f1; color:#111;
    }
    .tag.req { background:#e8fff3; color:#0a7; }
    .tag.opt { background:#f3f3ff; color:#556; }
    .tiny { font-size:12px; color:#777; margin-top:6px; }
    .debug { background:#fff7cc; padding:10px; border-radius:10px; font-size:13px; margin-top:10px; display:none; }
  </style>
</head>
<body>
  <div class="wrap">
    <h2>회원가입</h2>
    <div class="muted">이메일 인증 후 가입을 완료해 주세요.</div>

    <div class="label">이메일</div>
    <div class="row">
      <input id="emailId" placeholder="아이디" />
      <div class="at">@</div>
      <select id="emailDomain" onchange="onDomainChange()">
        <option value="">선택해주세요</option>
        <option value="gmail.com">gmail.com</option>
        <option value="naver.com">naver.com</option>
        <option value="daum.net">daum.net</option>
        <option value="hanmail.net">hanmail.net</option>
        <option value="kakao.com">kakao.com</option>
        <option value="_custom">직접입력</option>
      </select>
    </div>
    <div id="customDomainRow" class="row" style="margin-top:10px; display:none;">
      <input id="emailDomainCustom" placeholder="도메인 직접입력 (예: mycompany.com)" />
    </div>

    <button class="btn-wide" type="button" onclick="sendOtp()">이메일 인증하기</button>

    <div class="label">인증번호</div>
    <input id="otp" placeholder="인증번호 6자리" />
    <div id="otpMsg" class="msg"></div>
    <div id="debugBox" class="debug"></div>

    <form method="post" action="/signup" onsubmit="return beforeSubmit();">
      <input id="usernameHidden" name="username" type="hidden" />
      <input id="otpHidden" name="otp" type="hidden" />

      <div class="label">비밀번호</div>
      <input id="pw" name="password" type="password" placeholder="비밀번호" required />
      <div class="tiny">영문/숫자 포함 8자 이상 권장</div>

      <div class="label">비밀번호 확인</div>
      <input id="pw2" name="password2" type="password" placeholder="비밀번호 확인" required />

      <div class="label">이름</div>
      <input name="name" placeholder="이름" required />

      <div class="row" style="margin-top:10px;">
        <div>
          <div class="label" style="margin:0 0 6px;">성별</div>
          <select name="gender" required>
            <option value="">선택해주세요</option>
            <option value="F">여성</option>
            <option value="M">남성</option>
            <option value="N">선택안함</option>
          </select>
        </div>
        <div>
          <div class="label" style="margin:0 0 6px;">생년월일</div>
          <input name="birth" type="date" required />
        </div>
      </div>

      <div class="label" style="margin-top:18px;">약관동의</div>
      <div class="terms-box">
        <div class="terms-head">
          <div class="left">
            <input type="checkbox" id="t_all" onclick="toggleAllTerms(this)">
            <label for="t_all" style="cursor:pointer;">전체 동의</label>
          </div>
          <div class="right">(선택항목 포함)</div>
        </div>

        <div class="terms-item">
          <div class="left">
            <input type="checkbox" id="t_age" class="t_req">
            <label for="t_age" style="cursor:pointer;">만 14세 이상입니다</label>
          </div>
          <div class="right"><span class="tag req">필수</span></div>
        </div>

        <div class="terms-item">
          <div class="left">
            <input type="checkbox" id="t_terms" class="t_req">
            <label for="t_terms" style="cursor:pointer;">이용약관 동의</label>
          </div>
          <div class="right"><span class="tag req">필수</span></div>
        </div>

        <div class="terms-item">
          <div class="left">
            <input type="checkbox" id="t_priv" class="t_req">
            <label for="t_priv" style="cursor:pointer;">개인정보 처리방침 동의</label>
          </div>
          <div class="right"><span class="tag req">필수</span></div>
        </div>

        <div class="terms-item">
          <div class="left">
            <input type="checkbox" id="t_mkt">
            <label for="t_mkt" style="cursor:pointer;">마케팅 수신 동의</label>
          </div>
          <div class="right"><span class="tag opt">선택</span></div>
        </div>
      </div>

      <button class="btn" type="submit">회원가입하기</button>
      <p style="margin-top:12px;font-size:13px;color:#666; text-align:center;">
        이미 아이디가 있으신가요? <a href="/login">로그인</a>
      </p>
    </form>
  </div>

<script>
  function onDomainChange(){
    const sel = document.getElementById("emailDomain").value;
    const row = document.getElementById("customDomainRow");
    row.style.display = (sel === "_custom") ? "flex" : "none";
  }

  function getFullEmail(){
    const id = document.getElementById("emailId").value.trim();
    const domSel = document.getElementById("emailDomain").value.trim();
    const domCustom = document.getElementById("emailDomainCustom").value.trim();
    if(!id) return "";
    if(!domSel) return "";
    const dom = (domSel === "_custom") ? domCustom : domSel;
    if(!dom) return "";
    return id + "@" + dom;
  }

  async function sendOtp() {
    const email = getFullEmail();
    const msgEl = document.getElementById("otpMsg");
    const dbg = document.getElementById("debugBox");
    msgEl.textContent = "";
    dbg.style.display = "none";
    dbg.textContent = "";

    if (!email) {
      msgEl.innerHTML = '<span class="err">이메일 아이디/도메인을 입력해 주세요.</span>';
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

  function toggleAllTerms(box){
    const all = box.checked;
    document.getElementById("t_age").checked = all;
    document.getElementById("t_terms").checked = all;
    document.getElementById("t_priv").checked = all;
    document.getElementById("t_mkt").checked = all;
  }

  function beforeSubmit() {
    const email = getFullEmail();
    const otp = document.getElementById("otp").value.trim();

    const pw = document.getElementById("pw").value;
    const pw2 = document.getElementById("pw2").value;
    if(pw !== pw2){
      alert("비밀번호가 일치하지 않습니다.");
      return false;
    }

    // 필수 약관 체크
    const reqs = ["t_age","t_terms","t_priv"];
    for(const id of reqs){
      if(!document.getElementById(id).checked){
        alert("필수 약관에 동의해 주세요.");
        return false;
      }
    }

    if (!email) {
      alert("이메일을 입력해 주세요.");
      return false;
    }
    if (!otp) {
      alert("인증번호를 입력해 주세요.");
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
    password2: str = Form(...),
    name: str = Form(...),
    gender: str = Form(...),
    birth: str = Form(...),
):
    email = normalize_email(username)

    if password != password2:
        return HTMLResponse("<script>alert('비밀번호가 일치하지 않습니다.');history.back();</script>")

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
# API: 이벤트/참여
# -----------------------------
@app.get("/api/events")
def api_events(request: Request):
    user = get_current_user_api(request)
    if not user:
        return JSONResponse({"ok": False, "message": "로그인이 필요합니다."}, status_code=401)
    joined, events = get_events_for_api(user["id"])
    return {"ok": True, "joined": joined, "events": events}

@app.post("/api/events/{event_id}/join")
def api_join(event_id: str, request: Request):
    user = get_current_user_api(request)
    if not user:
        return JSONResponse({"ok": False, "message": "로그인이 필요합니다."}, status_code=401)
    ok, msg, cur = join_event_api(user["id"], event_id)
    if not ok:
        return JSONResponse({"ok": False, "message": msg, "current_event_id": cur}, status_code=400)
    joined, events = get_events_for_api(user["id"])
    return {"ok": True, "message": msg, "joined": joined, "events": events}

@app.post("/api/events/{event_id}/leave")
def api_leave(event_id: str, request: Request):
    user = get_current_user_api(request)
    if not user:
        return JSONResponse({"ok": False, "message": "로그인이 필요합니다."}, status_code=401)
    ok, msg = leave_event_api(user["id"], event_id)
    if not ok:
        return JSONResponse({"ok": False, "message": msg}, status_code=400)
    joined, events = get_events_for_api(user["id"])
    return {"ok": True, "message": msg, "joined": joined, "events": events}


# -----------------------------
# 탐색 페이지 (참여중인 활동 상단 고정 + 참여/빠지기 즉시 동작)
# -----------------------------
@app.get("/explore")
def explore_page():
    # ✅ iframe 내부 페이지: JS로 /api/events를 fetch하여 렌더링
    html_content = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>오세요 - 탐색</title>
  <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
    body { margin:0; font-family:Pretendard, system-ui; background:#fff; }
    .wrap { max-width:520px; margin:0 auto; padding:14px 10px 28px 10px; }
    .section-title { font-size:15px; font-weight:900; margin:10px 6px 10px; color:#111; }
    .muted { font-size:12px; color:#777; margin:0 6px 12px; }

    .card { margin: 0 6px 16px; border-radius:16px; overflow:hidden; border:1px solid #eee; background:#fff; }
    .photo {
      width:100%;
      aspect-ratio: 16/9;
      object-fit:cover;
      background:#f3f3f3;
      display:block;
    }
    .body { padding:12px 12px 14px; }
    .title-row { display:flex; gap:10px; align-items:flex-start; justify-content:space-between; }
    .title { font-size:16px; font-weight:900; color:#111; line-height:1.35; }
    .meta { margin-top:8px; font-size:13px; color:#555; display:flex; gap:8px; flex-wrap:wrap; }
    .meta span { display:inline-flex; gap:6px; align-items:center; }
    .footer { margin-top:12px; display:flex; justify-content:space-between; align-items:center; gap:10px; }
    .cap { font-size:12px; color:#777; font-weight:800; }
    .btn {
      padding:10px 12px; border-radius:12px; border:none; cursor:pointer; font-weight:900; font-size:13px;
    }
    .btn-join { background:#111; color:#fff; }
    .btn-leave { background:#f1f1f1; color:#111; }
    .btn-disabled { opacity:0.35; cursor:not-allowed; }
    .pill { font-size:12px; font-weight:900; padding:4px 8px; border-radius:999px; background:#f3f3f3; color:#111; }
    .toast { position:fixed; left:50%; transform:translateX(-50%); bottom:12px; background:#111; color:#fff;
      padding:10px 12px; border-radius:999px; font-size:13px; display:none; z-index:9999; }
  </style>
</head>
<body>
  <div class="wrap">
    <div id="joinedBox"></div>
    <div class="section-title">열려 있는 활동</div>
    <div class="muted">참여하기는 1개 활동만 가능하다. 다른 활동에 참여하려면 먼저 빠지기를 해야 한다.</div>
    <div id="list"></div>
  </div>
  <div id="toast" class="toast"></div>

<script>
  function esc(s){
    return String(s||"")
      .replaceAll("&","&amp;")
      .replaceAll("<","&lt;")
      .replaceAll(">","&gt;")
      .replaceAll('"',"&quot;")
      .replaceAll("'","&#039;");
  }

  function toast(msg){
    const t = document.getElementById("toast");
    t.textContent = msg || "";
    t.style.display = "block";
    setTimeout(()=>{ t.style.display="none"; }, 1400);
  }

  function capText(ev){
    const cap = (ev.capacity_text || "∞");
    return `${ev.count}/${cap}`;
  }

  function buildCard(ev, isJoinedSection){
    const img = ev.photo ? `<img class="photo" src="data:image/jpeg;base64,${ev.photo}">` : `<div class="photo"></div>`;
    const left = ev.time_left ? ` · <span class="pill">${esc(ev.time_left)}</span>` : ``;

    let btnHtml = "";
    if(ev.joined_by_me){
      btnHtml = `<button class="btn btn-leave" onclick="leaveEvent('${ev.id}')">빠지기</button>`;
    } else {
      const full = (ev.capacity && ev.capacity>0 && ev.count >= ev.capacity);
      const dis = full ? "btn-disabled" : "";
      const disAttr = full ? "disabled" : "";
      btnHtml = `<button class="btn btn-join ${dis}" ${disAttr} onclick="joinEvent('${ev.id}')">참여하기</button>`;
    }

    return `
      <div class="card">
        ${img}
        <div class="body">
          <div class="title-row">
            <div class="title">${esc(ev.title)}</div>
          </div>
          <div class="meta">
            <span>⏰ ${esc(ev.start_text || ev.start || "")}${left}</span>
            <span>📍 ${esc(ev.addr || "장소 미정")}</span>
          </div>
          <div class="footer">
            <div class="cap">👥 ${capText(ev)}</div>
            ${btnHtml}
          </div>
        </div>
      </div>
    `;
  }

  function render(data){
    const joinedBox = document.getElementById("joinedBox");
    const list = document.getElementById("list");
    joinedBox.innerHTML = "";

    if(data.joined){
      joinedBox.innerHTML = `
        <div class="section-title">참여중인 활동</div>
        ${buildCard(data.joined, true)}
      `;
    }

    const items = (data.events || []);
    if(!items.length){
      list.innerHTML = "<div style='padding:60px 12px; text-align:center; color:#999;'>열려 있는 활동이 없다.</div>";
      return;
    }
    // 참여중인 활동은 목록에서 중복 제거(위에 고정 표시되므로)
    const filtered = data.joined ? items.filter(x => x.id !== data.joined.id) : items;
    list.innerHTML = filtered.map(ev => buildCard(ev, false)).join("");
  }

  async function load(){
    const r = await fetch("/api/events");
    const d = await r.json();
    if(!r.ok || !d.ok){
      render({joined:null, events:[]});
      return;
    }
    render(d);
  }

  async function joinEvent(id){
    try{
      const r = await fetch(`/api/events/${id}/join`, {method:"POST"});
      const d = await r.json();
      if(!r.ok || !d.ok){
        toast(d.message || "참여 실패");
        return;
      }
      render(d);
      toast("참여했다");
      // ✅ 다른 탭(지도)도 즉시 갱신
      window.parent && window.parent.postMessage({type:"REFRESH_ALL"}, "*");
    }catch(e){
      toast("네트워크 오류");
    }
  }

  async function leaveEvent(id){
    try{
      const r = await fetch(`/api/events/${id}/leave`, {method:"POST"});
      const d = await r.json();
      if(!r.ok || !d.ok){
        toast(d.message || "빠지기 실패");
        return;
      }
      render(d);
      toast("빠졌다");
      window.parent && window.parent.postMessage({type:"REFRESH_ALL"}, "*");
    }catch(e){
      toast("네트워크 오류");
    }
  }

  window.addEventListener("message", (e)=>{
    if(e && e.data && e.data.type==="REFRESH"){
      load();
    }
  });

  load();
</script>
</body>
</html>
    """
    return HTMLResponse(html_content)


# -----------------------------
# 지도 페이지 (종료된 이벤트 숨김 + 참여/빠지기 + 인포윈도우 유지하며 버튼 갱신)
# -----------------------------
@app.get("/map")
def map_page():
    if not KAKAO_JAVASCRIPT_KEY:
        return HTMLResponse("<div style='padding:30px;font-family:sans-serif;'>KAKAO_JAVASCRIPT_KEY가 필요합니다.</div>")

    html_content = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>오세요 - 지도</title>
  <style>
    @import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
    body {{ margin:0; font-family:Pretendard, system-ui; }}
    #m {{ width:100%; height:100vh; }}
    .panel {{
      position: absolute; top: 10px; left: 10px; right: 10px;
      max-width:520px; margin:0 auto;
      background: rgba(255,255,255,0.98);
      border:1px solid #eee; border-radius:16px;
      padding:12px 12px; z-index:9999;
    }}
    .panel-title {{ font-weight:900; font-size:14px; margin-bottom:8px; }}
    .panel-card {{
      border:1px solid #eee; border-radius:14px; overflow:hidden; background:#fff;
    }}
    .panel-body {{ padding:10px 10px; }}
    .iw-img {{ width:100%; height:110px; object-fit:cover; border-radius:10px; margin-top:8px; background:#f3f3f3; }}
    .iw-title {{ font-weight:900; font-size:14px; }}
    .iw-meta {{ font-size:12px; margin-top:6px; color:#666; display:flex; gap:8px; flex-wrap:wrap; }}
    .iw-footer {{ margin-top:10px; display:flex; justify-content:space-between; align-items:center; gap:10px; }}
    .btn {{
      padding:9px 10px; border-radius:12px; border:none; cursor:pointer; font-weight_known:900;
      font-weight:900; font-size:12px;
    }}
    .btn-join {{ background:#111; color:#fff; }}
    .btn-leave {{ background:#f1f1f1; color:#111; }}
    .btn-disabled {{ opacity:0.35; cursor:not-allowed; }}
    .cap {{ font-size:12px; color:#777; font-weight:900; }}
    .pill {{ font-size:12px; font-weight:900; padding:3px 8px; border-radius:999px; background:#f3f3f3; color:#111; }}
    .toast {{ position:fixed; left:50%; transform:translateX(-50%); bottom:12px; background:#111; color:#fff;
      padding:10px 12px; border-radius:999px; font-size:13px; display:none; z-index:99999; }}
  </style>
</head>
<body>
  <div id="m"></div>
  <div id="panel" class="panel" style="display:none;"></div>
  <div id="toast" class="toast"></div>

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

    function toast(msg){{
      const t = document.getElementById("toast");
      t.textContent = msg || "";
      t.style.display = "block";
      setTimeout(()=>{{მი t.style.display="none"; }}, 1400);
    }}

    const map = new kakao.maps.Map(document.getElementById('m'), {{
      center: new kakao.maps.LatLng(36.019, 129.343),
      level: 7
    }});

    let markers = {{}};
    let eventsMap = {{}};
    let openIw = null;
    let openEventId = null;

    function capText(ev){{
      const cap = (ev.capacity_text || "∞");
      return `${{ev.count}}/${{cap}}`;
    }}

    function buildIwContent(ev){{
      const img = ev.photo ? `<img class="iw-img" src="data:image/jpeg;base64,${{ev.photo}}">` : `<div class="iw-img"></div>`;
      const left = ev.time_left ? ` <span class="pill">${{esc(ev.time_left)}}</span>` : ``;

      let btn = "";
      if(ev.joined_by_me){{
        btn = `<button class="btn btn-leave" onclick="leaveEvent('${{ev.id}}', true)">빠지기</button>`;
      }} else {{
        const full = (ev.capacity && ev.capacity>0 && ev.count >= ev.capacity);
        const dis = full ? "btn-disabled" : "";
        const disAttr = full ? "disabled" : "";
        btn = `<button class="btn btn-join ${{dis}}" ${{disAttr}} onclick="joinEvent('${{ev.id}}', true)">참여하기</button>`;
      }}

      return `
        <div style="padding:10px;width:240px;">
          <div class="iw-title">${{esc(ev.title)}}</div>
          <div class="iw-meta">
            <span>⏰ ${{esc(ev.start_text || ev.start || "")}}</span>
            ${{left}}
            <span>📍 ${{esc(ev.addr || "장소 미정")}}</span>
          </div>
          ${{img}}
          <div class="iw-footer">
            <div class="cap">👥 ${{capText(ev)}}</div>
            ${{btn}}
          </div>
        </div>
      `;
    }}

    function renderPanel(joined){{
      const panel = document.getElementById("panel");
      if(!joined){{
        panel.style.display = "none";
        panel.innerHTML = "";
        return;
      }}
      panel.style.display = "block";
      panel.innerHTML = `
        <div class="panel-title">참여중인 활동</div>
        <div class="panel-card">
          <div class="panel-body">
            <div style="font-weight:900;">${{esc(joined.title)}}</div>
            <div style="margin-top:6px;font-size:12px;color:#666;display:flex;gap:8px;flex-wrap:wrap;">
              <span>⏰ ${{esc(joined.start_text || joined.start || "")}}</span>
              ${{joined.time_left ? `<span class="pill">${{esc(joined.time_left)}}</span>` : ""}}
              <span>📍 ${{esc(joined.addr || "장소 미정")}}</span>
            </div>
            <div style="margin-top:10px;display:flex;justify-content:space-between;align-items:center;gap:10px;">
              <div class="cap">👥 ${{capText(joined)}}</div>
              <button class="btn btn-leave" onclick="leaveEvent('${{joined.id}}', false)">빠지기</button>
            </div>
          </div>
        </div>
      `;
    }}

    async function loadData(keepIw){{
      const r = await fetch("/api/events");
      const d = await r.json();
      if(!r.ok || !d.ok) return;

      eventsMap = {{}};
      (d.events||[]).forEach(ev => {{ eventsMap[ev.id] = ev; }});

      // panel
      renderPanel(d.joined);

      // marker sync
      const newIds = new Set((d.events||[]).map(x=>x.id));
      // remove old markers
      Object.keys(markers).forEach(id => {{
        if(!newIds.has(id)){{
          markers[id].setMap(null);
          delete markers[id];
        }}
      }});
      // add/update markers
      (d.events||[]).forEach(ev => {{
        if(!ev.lat || !ev.lng) return;
        const pos = new kakao.maps.LatLng(ev.lat, ev.lng);
        if(!markers[ev.id]){{
          const mk = new kakao.maps.Marker({{
            position: pos,
            map: map
          }});
          markers[ev.id] = mk;
          kakao.maps.event.addListener(mk, 'click', () => {{
            openEventId = ev.id;
            const cur = eventsMap[ev.id];
            if(!cur) return;
            const content = buildIwContent(cur);
            const iw = new kakao.maps.InfoWindow({{
              content: content,
              removable: true
            }});
            if(openIw) openIw.close();
            iw.open(map, mk);
            openIw = iw;
          }});
        }} else {{
          markers[ev.id].setPosition(pos);
        }}
      }});

      // keep info window open and just update content
      if(keepIw && openIw && openEventId && eventsMap[openEventId]){{
        openIw.setContent(buildIwContent(eventsMap[openEventId]));
      }}
    }}

    async function joinEvent(id, keepIw){{
      try {{
        const r = await fetch(`/api/events/${{id}}/join`, {{method:"POST"}});
        const d = await r.json();
        if(!r.ok || !d.ok){{
          toast(d.message || "참여 실패");
          return;
        }}
        // info window 유지하면서 내용만 갱신
        await loadData(true);
        toast("참여했다");
        window.parent && window.parent.postMessage({{type:"REFRESH_ALL"}}, "*");
      }} catch(e) {{
        toast("네트워크 오류");
      }}
    }}

    async function leaveEvent(id, keepIw){{
      try {{
        const r = await fetch(`/api/events/${{id}}/leave`, {{method:"POST"}});
        const d = await r.json();
        if(!r.ok || !d.ok){{
          toast(d.message || "빠지기 실패");
          return;
        }}
        await loadData(true);
        toast("빠졌다");
        window.parent && window.parent.postMessage({{type:"REFRESH_ALL"}}, "*");
      }} catch(e) {{
        toast("네트워크 오류");
      }}
    }}

    window.addEventListener("message", (e)=>{{
      if(e && e.data && e.data.type==="REFRESH") {{
        loadData(true);
      }}
    }});

    loadData(false);
  </script>
</body>
</html>
"""
    return HTMLResponse(html_content)


# =========================================================
# 10) Gradio 마운트
# =========================================================
app = gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
