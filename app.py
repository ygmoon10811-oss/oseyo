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
# 1) 환경 및 DB 설정
# -----------------------------
def db_conn():
    # Render 환경에서는 현재 폴더에 생성하는 것이 가장 안전함
    return sqlite3.connect("oseyo_final.db", check_same_thread=False)

with db_conn() as con:
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, title TEXT, photo TEXT, start TEXT, 
            end TEXT, addr TEXT, lat REAL, lng REAL, created_at TEXT
        );
    """)
    con.execute("CREATE TABLE IF NOT EXISTS favs (name TEXT PRIMARY KEY, count INTEGER DEFAULT 1);")
    con.commit()

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

# -----------------------------
# 2) CSS (UI 가림 및 위치 수정)
# -----------------------------
CSS = """
.gradio-container { max-width: 100% !important; padding-bottom: 100px !important; }
.fab-wrapper { position: fixed !important; right: 30px !important; bottom: 30px !important; z-index: 9999 !important; }
.fab-wrapper button { width: 65px !important; height: 65px !important; border-radius: 50% !important; background: #ff6b00 !important; color: white !important; font-size: 40px !important; border: none !important; box-shadow: 0 4px 15px rgba(0,0,0,0.4) !important; }

.main-modal { position: fixed !important; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 92vw; max-width: 500px; height: 86vh; background: white; z-index: 10001; border-radius: 24px; display: flex; flex-direction: column; overflow: hidden; }
.modal-body { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 16px; }
.modal-footer { padding: 16px 20px; border-top: 2px solid #eee; background: #f9f9f9; display: flex; gap: 10px; flex-shrink: 0; }

/* 텍스트박스 높이 제한 및 이미지 영역 확보 */
.modal-body .gradio-textbox textarea { max-height: 100px !important; }
.modal-body .gradio-image { min-height: 150px !important; flex-shrink: 0 !important; }
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 10000; }
"""

# -----------------------------
# 3) 핵심 로직
# -----------------------------
def get_list_html():
    with db_conn() as con:
        rows = con.execute("SELECT title, photo, start, addr FROM events ORDER BY created_at DESC").fetchall()
    if not rows: return "<div style='text-align:center; padding:60px; color:#999;'>등록된 이벤트가 없습니다.</div>"
    html_out = "<div style='padding:16px;'>"
    for title, photo, start, addr in rows:
        img_html = f"<img src='data:image/jpeg;base64,{photo}' style='width:120px;height:120px;object-fit:cover;border-radius:12px;' />" if photo else "<div style='width:120px;height:120px;background:#eee;border-radius:12px;'></div>"
        html_out += f"<div style='display:flex;justify-content:space-between;align-items:center;background:#f9f9f9;padding:16px;border-radius:16px;margin-bottom:12px;border:1px solid #eee;'><div><div style='font-weight:800;font-size:16px;'>{html.escape(title[:30])}</div><div style='font-size:12px;color:#666;'>📅 {start}</div><div style='font-size:12px;color:#666;'>📍 {addr}</div></div>{img_html}</div>"
    return html_out + "</div>"

def save_data(title, img, start, end, addr_obj):
    if not title: return "제목 필수"
    pic_b64 = ""
    if img is not None:
        im = Image.fromarray(img).convert("RGB")
        im.thumbnail((600, 600))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        pic_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    with db_conn() as con:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex[:8], title, pic_b64, start, end, addr_obj.get("name",""), addr_obj.get("y",0), addr_obj.get("x",0), datetime.now().isoformat()))
        con.commit()
    return "✅ 생성 완료"

# -----------------------------
# 4) UI 및 실행 (핵심 수정 부분)
# -----------------------------
app = FastAPI()  # uvicorn이 찾는 'app' 변수

with gr.Blocks(css=CSS) as demo:
    selected_addr = gr.State({})
    
    # UI 구성 (가림 현상 해결을 위해 텍스트박스 줄수 제한)
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_m:
        gr.HTML("<div style='padding:20px;font-weight:800;font-size:18px;'>새 이벤트 만들기</div>")
        with gr.Column(elem_classes=["modal-body"]):
            t_in = gr.Textbox(label="이벤트명", lines=1, max_lines=3) # 줄수 제한
            img_in = gr.Image(label="사진", height=150)
            with gr.Row():
                s_in = gr.Textbox(label="시작", value=datetime.now().strftime("%Y-%m-%d %H:%M"))
                e_in = gr.Textbox(label="종료", value=(datetime.now()+timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"))
            addr_v = gr.Textbox(label="장소", interactive=False)
            addr_btn = gr.Button("🔍 장소 검색")
            msg_out = gr.Markdown("")
        with gr.Row(elem_classes=["modal-footer"]):
            m_close = gr.Button("취소")
            m_save = gr.Button("✅ 생성", variant="primary")

    # 리턴 값 개수 오류 수정 (logs에 찍혔던 문제 해결)
    def save_and_close(t, i, s, e, a):
        res = save_data(t, i, s, e, a)
        return res, get_list_html(), gr.update(visible=False)

    m_save.click(save_and_close, [t_in, img_in, s_in, e_in, selected_addr], [msg_out, gr.HTML(), modal_m])

# FastAPI에 마운트
app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=True)
