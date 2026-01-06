# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from PIL import Image
import gradio as gr
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

# [환경 설정 및 DB 초기화 생략 - 기존과 동일]
KST = ZoneInfo("Asia/Seoul")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()
def now_kst(): return datetime.now(KST)
DATA_DIR = "/var/data" if os.path.isdir("/var/data") else os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "oseyo.db")
def db_conn(): return sqlite3.connect(DB_PATH, check_same_thread=False)

# CSS 패치: Gradio의 간섭을 차단하고 여백을 강제함
CSS = """
.modal-wrapper {
    display: flex !important; flex-direction: column !important;
    gap: 25px !important; /* 항목 간 충분한 간격 */
    padding-bottom: 100px !important; /* 하단 버튼 공간 확보 */
}
.fav-grid { display: grid !important; grid-template-columns: 1fr 1fr !important; gap: 10px !important; }
.map-container { width: 100%; height: 500px; border-radius: 15px; overflow: hidden; border: 1px solid #ddd; }
#fab-btn { position: fixed !important; right: 20px !important; bottom: 20px !important; z-index: 2000 !important; }
#fab-btn button { width: 60px !important; height: 60px !important; border-radius: 50% !important; background: #ff6b00 !important; color: white !important; font-size: 30px !important; }
"""

def get_map_html():
    return f'<iframe src="/kakao_map?v={uuid.uuid4().hex}" style="width:100%; height:100%; border:none;"></iframe>'

with gr.Blocks(css=CSS) as demo:
    search_state = gr.State([])
    
    with gr.Tabs():
        with gr.Tab("탐색"):
            home_ui = gr.HTML(lambda: "공간 목록을 불러오는 중...")
        with gr.Tab("지도"):
            gr.HTML('<div class="map-container">' + get_map_html() + '</div>')

    # FAB 버튼 및 모달 레이어
    fab_btn = gr.Button("+", elem_id="fab-btn")
    overlay = gr.HTML("<div id='over' style='position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:10000;display:none;'></div>")

    # 모달 본체: Column 대신 박스 형태의 구조 사용
    with gr.Box(visible=False, elem_id="modal-box") as modal:
        gr.Markdown("### 🏠 새 공간 열기")
        
        # 내부 스크롤을 위한 별도 Column
        with gr.Column(elem_classes=["modal-wrapper"]):
            act_in = gr.Textbox(label="활동명", placeholder="예: 커피, 산책")
            
            with gr.Row(elem_classes=["fav-grid"]):
                fav_btns = [gr.Button(f"즐겨찾기 {i}", visible=False) for i in range(4)]
            
            img_in = gr.Image(label="현장 사진", type="numpy")
            
            with gr.Row():
                st_in = gr.Textbox(label="시작", value=lambda: now_kst().strftime("%Y-%m-%dT%H:%M"))
                en_in = gr.Textbox(label="종료", value=lambda: (now_kst()+timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"))
            
            # 주소 검색 영역을 별도 섹션으로 분리하여 절대 겹치지 않게 함
            with gr.Group():
                gr.Markdown("#### 📍 장소 선택")
                loc_in = gr.Textbox(show_label=False, placeholder="장소명을 입력하세요")
                loc_btn = gr.Button("🔍 장소 찾기")
                loc_sel = gr.Radio(label="검색 결과", choices=[], visible=False)

        with gr.Row():
            close_btn = gr.Button("취소")
            save_btn = gr.Button("✅ 생성하기", variant="primary")

    # [이벤트 핸들러 및 FastAPI 설정 생략 - 이전 구조와 동일하게 연결]
