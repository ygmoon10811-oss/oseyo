# -*- coding: utf-8 -*-
import os
import re
import uuid
import time
import hmac
import json
import html
import base64
import sqlite3
import hashlib
import io
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image

import gradio as gr
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
import uvicorn

# =============================================================================
# 기본 설정
# =============================================================================
APP_NAME = "오세요"
KST = timezone(timedelta(hours=9))

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 168  # 7일

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "")
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "")

# =============================================================================
# 시간/DB 유틸
# =============================================================================
def now_kst():
    return datetime.now(tz=KST)


def pick_db_path():
    for d in ["/var/data", "/tmp"]:
        try:
            os.makedirs(d, exist_ok=True)
            test = os.path.join(d, ".test")
            with open(test, "w") as f:
                f.write("ok")
            os.remove(test)
            return os.path.join(d, "oseyo.db")
        except Exception:
            pass
    return "/tmp/oseyo.db"


DB_PATH = pick_db_path()
print("[DB]", DB_PATH)


def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


# =============================================================================
# DB 초기 스키마
# =============================================================================
with db_conn() as con:
    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE,
            pw_hash TEXT,
            created_at TEXT
        );
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT,
            expires_at TEXT
        );
    """)
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
            created_at TEXT
        );
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS participants (
            event_id TEXT,
            user_id TEXT,
            PRIMARY KEY(event_id, user_id)
        );
    """)
    con.commit()


# =============================================================================
# 🔥 DB 마이그레이션 (중요)
# =============================================================================
def migrate_events_table():
    with db_conn() as con:
        cols = [r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()]
        if "owner_user_id" not in cols:
            con.execute("ALTER TABLE events ADD COLUMN owner_user_id TEXT DEFAULT ''")
        if "max_people" not in cols:
            con.execute("ALTER TABLE events ADD COLUMN max_people INTEGER DEFAULT 10")
        con.commit()


migrate_events_table()

# =============================================================================
# 보안
# =============================================================================
def make_pw_hash(pw):
    salt = uuid.uuid4().hex
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000)
    return f"{salt}${base64.b64encode(dk).decode()}"


def check_pw(pw, stored):
    try:
        salt, hv = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000)
        return hmac.compare_digest(base64.b64encode(dk).decode(), hv)
    except Exception:
        return False


def new_session(user_id):
    token = uuid.uuid4().hex
    exp = now_kst() + timedelta(hours=SESSION_HOURS)
    with db_conn() as con:
        con.execute(
            "INSERT INTO sessions VALUES (?,?,?)",
            (token, user_id, exp.isoformat())
        )
        con.commit()
    return token


def get_user_by_token(token):
    if not token:
        return None
    with db_conn() as con:
        row = con.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token=?",
            (token,)
        ).fetchone()
        if not row:
            return None
        uid, exp = row
        if datetime.fromisoformat(exp) < now_kst():
            return None
        u = con.execute(
            "SELECT id, username FROM users WHERE id=?",
            (uid,)
        ).fetchone()
        if not u:
            return None
        return {"id": u[0], "username": u[1]}


# =============================================================================
# FastAPI
# =============================================================================
app = FastAPI()


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    if request.url.path.startswith(("/login", "/signup", "/health")):
        return await call_next(request)
    if request.url.path.startswith(("/app", "/explore", "/map", "/api")):
        token = request.cookies.get(COOKIE_NAME)
        if not get_user_by_token(token):
            return RedirectResponse("/login")
    return await call_next(request)


@app.get("/health")
def health():
    return {"ok": True}


# =============================================================================
# 로그인 / 회원가입
# =============================================================================
@app.get("/login")
def login_page():
    return HTMLResponse("""
    <form method="post">
      <h2>로그인</h2>
      <input name="username" placeholder="아이디"><br>
      <input name="password" type="password" placeholder="비밀번호"><br>
      <button>로그인</button>
      <p><a href="/signup">회원가입</a></p>
    </form>
    """)


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    with db_conn() as con:
        row = con.execute(
            "SELECT id, pw_hash FROM users WHERE username=?",
            (username,)
        ).fetchone()
    if not row or not check_pw(password, row[1]):
        return RedirectResponse("/login", status_code=302)

    token = new_session(row[0])
    res = RedirectResponse("/app")
    res.set_cookie(COOKIE_NAME, token, httponly=True)
    return res


@app.get("/signup")
def signup_page():
    return HTMLResponse("""
    <form method="post">
      <h2>회원가입</h2>
      <input name="username" placeholder="아이디"><br>
      <input name="password" type="password" placeholder="비밀번호"><br>
      <button>가입</button>
    </form>
    """)


@app.post("/signup")
def signup(username: str = Form(...), password: str = Form(...)):
    uid = uuid.uuid4().hex
    try:
        with db_conn() as con:
            con.execute(
                "INSERT INTO users VALUES (?,?,?,?)",
                (uid, username, make_pw_hash(password), now_kst().isoformat())
            )
            con.commit()
    except sqlite3.IntegrityError:
        return RedirectResponse("/signup", status_code=302)

    token = new_session(uid)
    res = RedirectResponse("/app")
    res.set_cookie(COOKIE_NAME, token, httponly=True)
    return res


# =============================================================================
# 이벤트 API
# =============================================================================
def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


@app.post("/api/events/create")
def create_event(
    request: Request,
    title: str = Form(...),
    start: str = Form(...),
    end: str = Form(...),
    max_people: int = Form(10)
):
    user = get_user_by_token(request.cookies.get(COOKIE_NAME))
    eid = uuid.uuid4().hex[:8]

    with db_conn() as con:
        con.execute("""
            INSERT INTO events
            (id, owner_user_id, title, start, end, max_people, created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (
            eid,
            user["id"],
            title,
            start,
            end,
            max_people,
            now_kst().strftime("%Y-%m-%d %H:%M:%S")
        ))
        con.commit()

    return {"ok": True}


# =============================================================================
# 탐색
# =============================================================================
@app.get("/explore")
def explore(request: Request):
    user = get_user_by_token(request.cookies.get(COOKIE_NAME))
    now_s = now_kst().strftime("%Y-%m-%d %H:%M")

    with db_conn() as con:
        rows = con.execute("""
            SELECT id, title, start, end, owner_user_id, max_people
            FROM events
            WHERE end > ?
            ORDER BY created_at DESC
        """, (now_s,)).fetchall()

    html_rows = ""
    for r in rows:
        html_rows += f"<li>{html.escape(r[1])} ({r[2]} ~ {r[3]})</li>"

    return HTMLResponse(f"""
    <h2>탐색</h2>
    <ul>{html_rows}</ul>
    <a href="/app">← 돌아가기</a>
    """)


# =============================================================================
# Gradio 앱
# =============================================================================
with gr.Blocks(title=APP_NAME) as demo:
    gr.Markdown(f"# {APP_NAME}")
    gr.Markdown("로그인한 사용자만 접근 가능")

    with gr.Tab("탐색"):
        gr.HTML('<iframe src="/explore" style="width:100%;height:70vh;border:none"></iframe>')

    with gr.Tab("이벤트 생성"):
        title = gr.Textbox(label="이벤트명")
        start = gr.Textbox(label="시작일시 (YYYY-MM-DD HH:MM)")
        end = gr.Textbox(label="종료일시 (YYYY-MM-DD HH:MM)")
        max_people = gr.Number(label="제한 인원", value=10)
        btn = gr.Button("생성")
        out = gr.Markdown()

        def submit(title, start, end, max_people, request: gr.Request):
            r = requests.post(
                request.url_root + "api/events/create",
                data={
                    "title": title,
                    "start": start,
                    "end": end,
                    "max_people": int(max_people)
                },
                cookies=request.cookies
            )
            return "✅ 생성 완료" if r.ok else "❌ 실패"

        btn.click(submit, [title, start, end, max_people], out)

app = gr.mount_gradio_app(app, demo, path="/app")

# =============================================================================
# Run
# =============================================================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
