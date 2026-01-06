# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
import requests
from PIL import Image
import gradio as gr
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# [1. 환경 설정]
# Render는 /tmp 폴더가 쓰기 권한이 가장 확실합니다.
DB_PATH = "/tmp/oseyo.db" 

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

# DB 초기화 (오류 방지를 위해 단순화)
with db_conn() as con:
    con.execute("CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, title TEXT, photo_b64 TEXT, address TEXT, lat REAL, lng REAL);")
    con.commit()

# [2. UI 스타일] - 복잡한 애니메이션 제거 (에러 방지)
CSS = """
.container { max-width: 800px; margin: auto; }
.footer { text-align: center; margin-top: 20px; color: #888; }
"""

# [3. 로직 함수]
def save_simple(title, img, addr_name):
    if not title: return "❌ 활동명을 입력하세요."
    
    pic = ""
    if img is not None:
        try:
            im = Image.fromarray(img)
            if im.mode == 'RGBA': im = im.convert('RGB')
            im.thumbnail((400, 400)) # 용량 최적화
            buf = io.BytesIO()
            im.save(buf, format='JPEG')
            pic = base64.b64encode(buf.getvalue()).decode()
        except: pass

    try:
        with db_conn() as con:
            con.execute("INSERT INTO spaces (id, title, photo_b64, address, lat, lng) VALUES (?,?,?,?,?,?)",
                       (uuid.uuid4().hex[:8], title, pic, addr_name, 37.5665, 126.9780))
            con.commit()
        return f"✅ '{title}' 등록 완료! 페이지를 새로고침 하세요."
    except Exception as e:
        return f"❌ 오류: {str(e)}"

# [4. Gradio UI]
with gr.Blocks(css=CSS) as demo:
    gr.Markdown("# 🏠 오세요 (Render Test)")
    
    with gr.Tabs():
        with gr.Tab("개설하기"):
            with gr.Column(elem_classes=["container"]):
                in_title = gr.Textbox(label="활동명", placeholder="예: 독서 모임")
                in_img = gr.Image(label="사진", type="numpy")
                in_addr = gr.Textbox(label="장소명", value="서울 어딘가")
                btn_submit = gr.Button("공간 만들기", variant="primary")
                out_msg = gr.Markdown()
                
                btn_submit.click(save_simple, [in_title, in_img, in_addr], out_msg)

        with gr.Tab("지도 보기"):
            gr.HTML('<iframe src="/map" style="width:100%;height:500px;border:1px solid #eee;"></iframe>')
            btn_refresh = gr.Button("지도 새로고침 (페이지 전체 새로고침 권장)")

# [5. FastAPI & Map]
app = FastAPI()

@app.get("/map")
def get_map():
    with db_conn() as con:
        rows = con.execute("SELECT title, lat, lng FROM spaces").fetchall()
    
    # 카카오맵 대신 구글맵(임시) 또는 단순 텍스트로 데이터 확인
    # Render 환경에서 카카오 SDK가 차단되는 경우가 있어 우선 데이터 리스트로 표시
    items_html = "".join([f"<li><b>{r[0]}</b> (좌표: {r[1]}, {r[2]})</li>" for r in rows])
    return HTMLResponse(f"""
        <html>
        <body style='padding:20px; font-family: sans-serif;'>
            <h3>현재 등록된 공간 목록</h3>
            <ul>{items_html if items_html else "등록된 공간이 없습니다."}</ul>
            <p style='color:blue'>* 데이터가 보인다면 DB는 정상입니다!</p>
        </body>
        </html>
    """)

app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    # Render 전용 포트 설정
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
