# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json, html
from datetime import datetime, timedelta

import requests
from PIL import Image

import gradio as gr
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# -----------------------------
# 1) 환경/DB - Render 최적화
# -----------------------------
def pick_db_path():
    # Render에서는 현재 작업 디렉토리에 생성하는 것이 가장 안전합니다.
    return "oseyo_final.db"

DB_PATH = pick_db_path()
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

with db_conn() as con:
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, title TEXT, photo TEXT, start TEXT, 
            end TEXT, addr TEXT, lat REAL, lng REAL, created_at TEXT
        );
    """)
    con.execute("CREATE TABLE IF NOT EXISTS favs (name TEXT PRIMARY KEY, count INTEGER DEFAULT 1);")
    con.commit()

# -----------------------------
# 2) CSS - 레이아웃 가림 방지 및 고정
# -----------------------------
CSS = """
html, body { margin: 0 !important; padding: 0 !important; overflow-x: hidden !important; }
.gradio-container { max-width: 100% !important; padding-bottom: 100px !important; }

.fab-wrapper { position: fixed !important; right: 30px !important; bottom: 30px !important; z-index: 9999 !important; width: auto !important; height: auto !important; }
.fab-wrapper button { width: 65px !important; height: 65px !important; border-radius: 50% !important; background: #ff6b00 !important; color: white !important; font-size: 40px !important; border: none !important; box-shadow: 0 4px 15px rgba(0,0,0,0.4) !important; cursor: pointer !important; }

.main-modal { position: fixed !important; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 92vw; max-width: 500px; height: 86vh; background: white; z-index: 10001; border-radius: 24px; box-shadow: 0 20px 60px rgba(0,0,0,0.4); display: flex; flex-direction: column; overflow: hidden; }
.modal-header { padding: 20px; border-bottom: 2px solid #eee; font-weight: 800; font-size: 20px; flex-shrink: 0; }
.modal-body { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 16px; }
.modal-footer { padding: 16px 20px; border-top: 2px solid #eee; background: #f9f9f9; display: flex; gap: 10px; flex-shrink: 0; }

/* 입력창이 길어져도 이미지 등을 가리지 않도록 고정 */
.modal-body .gradio-textbox textarea { max-height: 80px !important; }
.modal-body .gradio-image { min-height: 150px !important; flex-shrink: 0 !important; }

.fav-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; flex-shrink: 0; }
.fav-grid button { font-size: 13px; padding: 8px; border-radius: 10px; overflow: hidden; text-overflow: ellipsis; }

.event-card { background: #f9f9f9; border-radius: 16px; padding: 16px; margin-bottom: 14px; border: 1px solid #e5e5e5; display: grid; grid-template-columns: 1fr 120px; gap: 14px; align-items: center; }
.event-photo { width: 120px; height: 120px; object-fit: cover; border-radius: 12px; }
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 10000; }
"""

# -----------------------------
# 3) 로직
# -----------------------------
def get_list_html():
    with db_conn() as con:
        rows = con.execute("SELECT title, photo, start, addr FROM events ORDER BY created_at DESC").fetchall()
    if not rows: return "<div style='text-align:center; padding:60px; color:#999;'>등록된 이벤트가 없습니다.</div>"
    html_out = "<div style='padding:16px;'>"
    for title, photo, start, addr in rows:
        img_html = f"<img class='event-photo' src='data:image/jpeg;base64,{photo}' />" if photo else "<div class='event-photo' style='background:#e0e0e0;'></div>"
        html_out += f"<div class='event-card'><div class='event-info'><div style='font-weight:800; font-size:18px;'>{html.escape(title or '')}</div><div style='font-size:13px; color:#666;'>📅 {html.escape(start or '')}</div><div style='font-size:13px; color:#666;'>📍 {html.escape(addr or '')}</div></div>{img_html}</div>"
    return html_out + "</div>"

def save_data(title, img, start, end, addr_obj):
    title = (title or "").strip()
    if not title: return "제목을 입력해 주세요"
    pic_b64 = ""
    if img is not None:
        try:
            im = Image.fromarray(img).convert("RGB")
            im.thumbnail((600, 600))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            pic_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        except: pass
    addr_name = (addr_obj or {}).get("name", "").strip()
    lat, lng = (addr_obj or {}).get("y", 0), (addr_obj or {}).get("x", 0)
    with db_conn() as con:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex[:8], title, pic_b64, start or "", end or "", addr_name, lat, lng, datetime.now().isoformat(timespec="seconds")))
        con.execute("INSERT INTO favs (name, count) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET count = count + 1", (title,))
        con.commit()
    return "✅ 생성 완료"

# -----------------------------
# 4) Gradio UI
# -----------------------------
now_dt = datetime.now()
later_dt = now_dt + timedelta(hours=2)

with gr.Blocks(css=CSS, title="오세요") as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    gr.Markdown("# 지금, 열려 있습니다\n원하시면 오세요")
    with gr.Tabs():
        with gr.Tab("탐색"):
            explore_html = gr.HTML(get_list_html())
            refresh_btn = gr.Button("🔄 새로고침", size="sm")
        with gr.Tab("지도"):
            gr.HTML('<iframe src="/map" style="width:100%;height:70vh;border:none;border-radius:16px;"></iframe>')

    fab = gr.Button("+", elem_classes=["fab-wrapper"])
    overlay = gr.HTML("<div class='overlay'></div>", visible=False)

    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_m:
        gr.HTML("<div class='modal-header'>새 이벤트 만들기</div>")
        with gr.Column(elem_classes=["modal-body"]):
            with gr.Row():
                t_in = gr.Textbox(label="📝 이벤트명", lines=1, max_lines=3, scale=3)
                add_fav_btn = gr.Button("⭐", scale=1)
            fav_msg = gr.Markdown("")
            with gr.Column(elem_classes=["fav-grid"]):
                f_btns = [gr.Button("", visible=False, size="sm") for _ in range(10)]
            img_in = gr.Image(label="📸 사진 (선택)", type="numpy", height=150)
            with gr.Row():
                s_in = gr.Textbox(label="📅 시작", value=now_dt.strftime("%Y-%m-%d %H:%M"))
                e_in = gr.Textbox(label="⏰ 종료", value=later_dt.strftime("%Y-%m-%d %H:%M"))
            addr_v = gr.Textbox(label="📍 장소", interactive=False)
            addr_btn = gr.Button("🔍 장소 검색")
            msg_out = gr.Markdown("")
        with gr.Row(elem_classes=["modal-footer"]):
            m_close = gr.Button("취소")
            m_save = gr.Button("✅ 생성", variant="primary")

    # 핸들러 (생략된 서브모달 등 기존 로직 유지)
    # ... (중략: 기존 검색 및 모달 열기 로직 동일) ...
    def save_and_close(title, img, start, end, addr):
        msg = save_data(title, img, start, end, addr)
        return msg, get_list_html(), gr.update(visible=False), gr.update(visible=False)

    m_save.click(save_and_close, [t_in, img_in, s_in, e_in, selected_addr], [msg_out, explore_html, overlay, modal_m])
    # ... (나머지 핸들러 동일) ...
