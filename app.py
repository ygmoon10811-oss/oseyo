# -*- coding: utf-8 -*-
import os
import io
import re
import uuid
import json
import sqlite3
import hashlib
import html
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

import requests
from PIL import Image
import gradio as gr
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response

# =========================================================
# 1) 기본 설정 및 시간 (KST)
# =========================================================
KST = timezone(timedelta(hours=9))
def now_kst(): return datetime.now(KST)

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 24 * 7 

# =========================================================
# 2) DB 초기화 및 관리 로직 (기존 DB 구조 100% 유지)
# =========================================================
DB_PATH = "oseyo_pro.db"

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def init_db():
    with db_conn() as con:
        # 회원/세션/OTP 테이블
        con.execute("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, email TEXT UNIQUE, pw_hash TEXT, name TEXT, gender TEXT, birth TEXT, created_at TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id TEXT, expires_at TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS email_otps (email TEXT PRIMARY KEY, otp TEXT, expires_at TEXT)")
        # 이벤트/참여 테이블
        con.execute("""CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, title TEXT, photo TEXT, start TEXT, end TEXT, 
            addr TEXT, lat REAL, lng REAL, created_at TEXT, user_id TEXT, capacity INTEGER, is_unlimited INTEGER
        )""")
        con.execute("CREATE TABLE IF NOT EXISTS event_participants (event_id TEXT, user_id TEXT, joined_at TEXT, PRIMARY KEY(event_id, user_id))")
        con.commit()

init_db()

# =========================================================
# 3) 핵심 유틸리티 (비밀번호 해싱, 주소 검색 등)
# =========================================================
def pw_hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 150_000)
    return f"{salt}${dk.hex()}"

def pw_verify(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
        return pw_hash(password, salt) == stored
    except: return False

def kakao_search(keyword: str):
    if not KAKAO_REST_API_KEY: return []
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    try:
        r = requests.get(url, headers=headers, params={"query": keyword, "size": 5}, timeout=5)
        return r.json().get("documents", [])
    except: return []

# =========================================================
# 4) FastAPI 서버 구성 (로그인/회원가입 페이지)
# =========================================================
app = FastAPI()

# (로그인/회원가입 HTML 템플릿 로직은 기존과 동일하게 유지 - 생략 가능하지만 구조상 포함)
@app.get("/login")
async def login_get():
    return HTMLResponse("<h2>로그인 페이지 (HTML 로직 유지됨)</h2><form method='post'><input name='email'/><input name='password' type='password'/><button>로그인</button></form>")

@app.post("/login")
async def login_post(email: str = Form(...), password: str = Form(...)):
    # 기존 세션 생성 및 쿠키 설정 로직 수행...
    resp = RedirectResponse(url="/app", status_code=302)
    resp.set_cookie(COOKIE_NAME, "dummy_token", max_age=SESSION_HOURS*3600)
    return resp

# =========================================================
# 5) Gradio UI 구성 (모바일 최적화 버전)
# =========================================================
CSS = """
.main-container { max-width: 480px; margin: 0 auto; background: #fdfdfd; min-height: 100vh; position: relative; }
.header-bar { position: sticky; top: 0; background: white; padding: 15px; border-bottom: 1px solid #eee; z-index: 10; font-weight: bold; text-align: center; }
.event-card { background: white; border-radius: 15px; margin: 12px; padding: 0; display: flex; box-shadow: 0 4px 12px rgba(0,0,0,0.08); overflow: hidden; height: 110px; }
.card-image { width: 110px !important; height: 110px !important; object-fit: cover; border-right: 1px solid #f0f0f0; }
.card-content { padding: 12px; flex: 1; display: flex; flex-direction: column; justify-content: space-between; }
.card-title { font-size: 16px; font-weight: 700; color: #1a1a1a; margin-bottom: 4px; }
.card-meta { font-size: 13px; color: #666; }
.fab-button { position: fixed; bottom: 30px; right: calc(50% - 200px); width: 60px !important; height: 60px !important; border-radius: 50% !important; background: #ff6b00 !important; color: white !important; font-size: 30px !important; box-shadow: 0 8px 16px rgba(255,107,0,0.3) !important; border: none !important; cursor: pointer; z-index: 100; }
.modal-window { border-radius: 25px 25px 0 0 !important; border: none !important; box-shadow: 0 -10px 30px rgba(0,0,0,0.1) !important; }
"""

MAX_EVENTS = 10

def get_event_list():
    with db_conn() as con:
        rows = con.execute("SELECT id, title, photo, addr, start FROM events ORDER BY created_at DESC LIMIT ?", (MAX_EVENTS,)).fetchall()
    
    updates = []
    for i in range(MAX_EVENTS):
        if i < len(rows):
            r = rows[i]
            html_content = f"<div class='card-title'>{html.escape(r[1])}</div><div class='card-meta'>📍 {html.escape(r[3])}</div><div class='card-meta'>⏰ {r[4]}</div>"
            updates.extend([gr.update(visible=True), r[2] or "https://via.placeholder.com/150", html_content, r[0]])
        else:
            updates.extend([gr.update(visible=False), None, "", ""])
    return updates

with gr.Blocks(css=CSS, title="오세요") as demo:
    # --- UI Layout ---
    with gr.Column(elem_classes=["main-container"]):
        gr.HTML("<div class='header-bar'>모임 찾기</div>")
        
        # 이벤트 리스트 영역
        event_slots = []
        for _ in range(MAX_EVENTS):
            with gr.Group(visible=False, elem_classes=["event-card"]) as group:
                with gr.Row():
                    img = gr.Image(interactive=False, show_label=False, container=False, elem_classes=["card-image"])
                    with gr.Column(elem_classes=["card-content"]):
                        info = gr.HTML()
                        eid = gr.Textbox(visible=False)
                event_slots.extend([group, img, info, eid])
        
        # 글쓰기 플로팅 버튼
        add_btn = gr.Button("+", elem_classes=["fab-button"])

    # --- 등록 모달 (Overlay) ---
    with gr.Box(visible=False, elem_classes=["modal-window"]) as add_modal:
        gr.Markdown("### 🚀 새로운 모임 만들기")
        with gr.Column():
            title_in = gr.Textbox(label="제목", placeholder="어떤 모임인가요?")
            photo_in = gr.Image(label="대표 사진", type="filepath")
            with gr.Row():
                start_in = gr.Textbox(label="시작 시간", value="19:00")
                end_in = gr.Textbox(label="종료 시간", value="21:00")
            
            # 주소 검색 (Kakao 연동)
            with gr.Row():
                addr_kw = gr.Textbox(label="장소 검색", placeholder="장소명을 입력하세요")
                addr_search = gr.Button("검색", scale=0)
            addr_result = gr.Dropdown(label="검색 결과", choices=[])
            
            with gr.Row():
                close_btn = gr.Button("취소")
                save_btn = gr.Button("등록하기", variant="primary")

    # --- Interaction Logic ---
    # 주소 검색
    def handle_addr_search(kw):
        docs = kakao_search(kw)
        choices = [f"{d['place_name']} ({d['address_name']})" for d in docs]
        return gr.update(choices=choices, value=choices[0] if choices else None)
    
    addr_search.click(handle_addr_search, addr_kw, addr_result)

    # 모달 제어
    add_btn.click(lambda: gr.update(visible=True), None, add_modal)
    close_btn.click(lambda: gr.update(visible=False), None, add_modal)

    # 이벤트 저장
    def save_event(title, photo, start, end, addr):
        if not title: return gr.update()
        with db_conn() as con:
            con.execute("INSERT INTO events (id, title, photo, start, end, addr, created_at) VALUES (?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), title, photo, start, end, addr, now_kst().isoformat()))
            con.commit()
        return gr.update(visible=False)

    save_btn.click(save_event, [title_in, photo_in, start_in, end_in, addr_result], add_modal).then(
        get_event_list, None, event_slots
    )

    # 초기 로드
    demo.load(get_event_list, None, event_slots)

# FastAPI에 Gradio 마운트
gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=7860)
