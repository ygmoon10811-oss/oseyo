# -*- coding: utf-8 -*-
import os
import uuid
import base64
import io
import sqlite3
import json
import html
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import requests
from PIL import Image

import gradio as gr
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
import uvicorn

# =========================================================
# 기본 설정
# =========================================================
KST = timezone(timedelta(hours=9))
COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 24 * 7

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
DB_PATH = os.getenv("DB_PATH", "/var/data/oseyo_final.db")

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "")
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "")

print("[DB] Using:", DB_PATH)


def now_kst():
    return datetime.now(tz=KST)


def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


# =========================================================
# DB 초기화
# =========================================================
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
        CREATE TABLE IF NOT EXISTS favs (
            name TEXT PRIMARY KEY,
            count INTEGER DEFAULT 1
        );
    """)
    con.commit()


# =========================================================
# 인증 유틸
# =========================================================
def make_pw_hash(pw: str) -> str:
    salt = uuid.uuid4().hex
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000)
    return f"{salt}${base64.b64encode(dk).decode()}"


def check_pw(pw: str, stored: str) -> bool:
    try:
        salt, hv = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120000)
        return hmac.compare_digest(base64.b64encode(dk).decode(), hv)
    except Exception:
        return False


def new_session(user_id: str) -> str:
    token = uuid.uuid4().hex
    exp = now_kst() + timedelta(hours=SESSION_HOURS)
    with db_conn() as con:
        con.execute("INSERT INTO sessions VALUES (?,?,?)", (token, user_id, exp.isoformat()))
        con.commit()
    return token


def get_user_by_token(token: str):
    if not token:
        return None
    with db_conn() as con:
        row = con.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token=?", (token,)
        ).fetchone()
        if not row:
            return None
        uid, exp = row
        if datetime.fromisoformat(exp) < now_kst():
            return None
        u = con.execute("SELECT id, username FROM users WHERE id=?", (uid,)).fetchone()
        if not u:
            return None
        return {"id": u[0], "username": u[1]}


def set_auth_cookie(resp, token: str):
    resp.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        secure=False,              # 🔥 강제
        max_age=SESSION_HOURS * 3600,
    )
    return resp



# =========================================================
# FastAPI
# =========================================================
app = FastAPI()

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path

    # 🔓 공개 경로
    if path in ("/login", "/signup", "/logout", "/whoami", "/health"):
        return await call_next(request)

    # 🔐 보호는 /app 진입만
    if path == "/app" or path.startswith("/app?"):
        token = request.cookies.get(COOKIE_NAME)
        if not token or not get_user_by_token(token):
            return RedirectResponse("/login", status_code=303)

    return await call_next(request)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def root():
    return RedirectResponse("/app", status_code=303)


@app.get("/whoami")
def whoami(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    return {"cookie": bool(token), "user": get_user_by_token(token)}


# =========================================================
# 로그인 / 회원가입
# =========================================================
@app.get("/login")
def login_page():
    return HTMLResponse("""
    <h2 style="text-align:center;margin-top:60px;">오세요 로그인</h2>
    <form method="post" action="/login" style="max-width:360px;margin:30px auto;">
      <input name="username" placeholder="아이디" required style="width:100%;padding:12px;margin:6px 0"/>
      <input name="password" type="password" placeholder="비밀번호" required style="width:100%;padding:12px;margin:6px 0"/>
      <button style="width:100%;padding:12px;background:#ff6b00;color:white;border:none;border-radius:8px;">
        로그인
      </button>
      <p style="text-align:center;margin-top:10px;">
        <a href="/signup">회원가입</a>
      </p>
    </form>
    """)


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    ...
    resp = RedirectResponse("/app", status_code=303)
    return set_auth_cookie(resp, token)


@app.get("/signup")
def signup_page():
    return HTMLResponse("""
    <h2 style="text-align:center;margin-top:60px;">회원가입</h2>
    <form method="post" action="/signup" style="max-width:360px;margin:30px auto;">
      <input name="username" placeholder="아이디" required style="width:100%;padding:12px;margin:6px 0"/>
      <input name="password" type="password" placeholder="비밀번호" required style="width:100%;padding:12px;margin:6px 0"/>
      <button style="width:100%;padding:12px;background:#111;color:white;border:none;border-radius:8px;">
        가입
      </button>
    </form>
    """)


@app.post("/signup")
def signup(username: str = Form(...), password: str = Form(...)):
    ...
    resp = RedirectResponse("/app", status_code=303)
    return set_auth_cookie(resp, token)


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# =========================================================
# 👉 여기서부터가 "네가 만든 UI" (요약판)
# =========================================================
with gr.Blocks(title="오세요") as demo:
    gr.Markdown("# 지금, 열려 있습니다\n원하시면 오세요")
    gr.Markdown("✅ 이 화면은 **로그인 후에만** 보입니다.")
    gr.Markdown("👉 로그아웃: [/logout](/logout)")


# Gradio는 반드시 /app
app = gr.mount_gradio_app(app, demo, path="/app")


# =========================================================
# 실행
# =========================================================
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


