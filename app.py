# -*- coding: utf-8 -*-
import os
import uuid
import sqlite3
import html
from datetime import datetime, timedelta, timezone

import gradio as gr
from PIL import Image

# =========================================================
# 0) 설정 및 상수
# =========================================================
KST = timezone(timedelta(hours=9))

def now_kst():
    return datetime.now(KST)

DB_PATH = "events.db"
MAX_ITEMS = 10  # 리스트 최대 표시 개수

# =========================================================
# 1) 데이터베이스 초기화
# =========================================================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            eid TEXT PRIMARY KEY,
            title TEXT,
            img_path TEXT,
            start_time TEXT,
            end_time TEXT,
            addr_text TEXT,
            cap_val INTEGER,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# =========================================================
# 2) DB 핸들링
# =========================================================
def save_event_db(title, start, end, addr, cap_v):
    eid = str(uuid.uuid4())
    created = now_kst().isoformat()
    # 이미지 경로는 데모용 더미 이미지 사용
    dummy_img = "https://dummyimage.com/100x100/ff6f0f/ffffff&text=Event"
    
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO events (eid, title, img_path, start_time, end_time, addr_text, cap_val, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (eid, title, dummy_img, start, end, addr, cap_v, created))
    conn.commit()
    conn.close()

def get_events_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT * FROM events ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

# =========================================================
# 3) UI 로직 (CSS 포함)
# =========================================================
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;700&display=swap');

body, gradio-app {
    font-family: 'Noto Sans KR', sans-serif !important;
    background-color: #f0f2f5;
}
.app-container {
    max-width: 420px !important;
    margin: 0 auto !important;
    background-color: white;
    min-height: 100vh;
    box-shadow: 0 0 15px rgba(0,0,0,0.1);
    position: relative;
    padding-bottom: 80px; 
}
.header-bar {
    padding: 15px;
    border-bottom: 1px solid #eee;
    background: white;
    position: sticky;
    top: 0;
    z-index: 10;
}
.header-title {
    font-size: 1.2rem;
    font-weight: bold;
    color: #333;
    margin: 0;
}
.custom-tabs button.selected {
    color: #ff6f0f !important;
    border-bottom: 2px solid #ff6f0f !important;
}
.event-card {
    border-bottom: 1px solid #f0f0f0;
    padding: 15px;
    display: flex;
    gap: 12px;
    background: white;
    cursor: pointer;
}
.card-img {
    width: 90px !important;
    height: 90px !important;
    border-radius: 8px !important;
    object-fit: cover;
    background-color: #eee;
    overflow: hidden;
}
.card-img img {
    width: 100%; height: 100%; object-fit: cover;
}
.card-info {
    flex-grow: 1; display: flex; flex-direction: column; justify-content: center;
}
.card-title { font-size: 16px; font-weight: bold; color: #222; }
.card-meta { font-size: 13px; color: #888; margin-top: 4px; }
.fab-btn {
    position: fixed !important;
    bottom: 25px;
    left: 50%;
    transform: translateX(140px);
    width: 56px !important; height: 56px !important;
    border-radius: 50% !important;
    background: #ff6f0f !important;
    box-shadow: 0 4px 10px rgba(255, 111, 15, 0.4) !important;
    color: white !important;
    font-size: 24px !important;
    z-index: 999;
}
.modal-overlay {
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.5); z-index: 2000;
    display: flex; align-items: center; justify-content: center;
    backdrop-filter: blur(2px);
}
.modal-content {
    background: white; width: 90%; max-width: 400px;
    border-radius: 16px; padding: 20px;
    box-shadow: 0 10px 25px rgba(0,0,0,0.2);
}
"""

def make_card_html(title, start_time, addr):
    return f"""
    <div class='card-title'>{html.escape(title)}</div>
    <div class='card-meta'>📍 {html.escape(addr)}</div>
    <div class='card-meta'>⏰ {html.escape(start_time)}</div>
    """

def refresh_view():
    rows = get_events_db()
    
    # Gradio의 update 객체 리스트 (순서 중요: Visible, Image, HTML, EID)
    updates_joined = []
    
    # 1. 모임 찾기 (Joined) 탭 데이터 채우기
    for i in range(MAX_ITEMS):
        if i < len(rows):
            r = rows[i]
            # r: 0=eid, 1=title, 2=img, 3=start, 4=end, 5=addr, 6=cap...
            eid, title, img_path, start, addr = r[0], r[1], r[2], r[3], r[5]
            
            updates_joined.append(gr.update(visible=True))       # Group
            updates_joined.append(gr.update(value=img_path))     # Image
            updates_joined.append(gr.update(value=make_card_html(title, start, addr))) # HTML
            updates_joined.append(gr.update(value=eid))          # Textbox(hidden)
        else:
            updates_joined.append(gr.update(visible=False))
            updates_joined.append(gr.update())
            updates_joined.append(gr.update())
            updates_joined.append(gr.update())

    # 2. 내 모임 (My) 탭 데이터 채우기 (데모용으로 똑같이 처리)
    # 실제로는 내가 쓴 글만 필터링해야 하지만, 에러 방지를 위해 구조를 똑같이 맞춤
    updates_my = []
    for i in range(MAX_ITEMS):
        updates_my.append(gr.update(visible=False)) # 일단 다 숨김 처리
        updates_my.append(gr.update())
        updates_my.append(gr.update())
        updates_my.append(gr.update())
            
    # 두 리스트를 합쳐서 반환 (총 40 + 40 = 80개 요소)
    return updates_joined + updates_my

def save_event(title, start, end, addr, cap_v):
    if not title:
        return "제목 필요", gr.update(), gr.update()
    
    save_event_db(title, start, end, addr, cap_v)
    return "저장됨", gr.update(visible=False), gr.update(visible=False)

def open_modal(): return gr.update(visible=True), gr.update(visible=True)
def close_modal(): return gr.update(visible=False), gr.update(visible=False)

# =========================================================
# 4) Gradio 구성
# =========================================================
with gr.Blocks(css=CSS, title="오세요") as demo:
    
    with gr.Column(elem_classes=["app-container"]):
        # 헤더
        with gr.Row(elem_classes=["header-bar"]):
            gr.Markdown("### 오세요", elem_classes=["header-title"])

        # 출력 컴포넌트들을 담을 리스트
        all_components = [] 

        with gr.Tabs(elem_classes=["custom-tabs"]):
            # [탭 1] 모임 찾기
            with gr.TabItem("모임 찾기"):
                for i in range(MAX_ITEMS):
                    with gr.Group(visible=False, elem_classes=["event-card"]) as g:
                        with gr.Row(variant="compact"):
                            img = gr.Image(interactive=False, show_label=False, container=False, elem_classes=["card-img"])
                            info = gr.HTML(elem_classes=["card-info"])
                            eid = gr.Textbox(visible=False)
                        
                        # 리스트에 순서대로 추가 (Group -> Img -> Info -> Eid)
                        all_components.extend([g, img, info, eid])

            # [탭 2] 내 모임
            with gr.TabItem("내 모임"):
                for i in range(MAX_ITEMS):
                    with gr.Group(visible=False, elem_classes=["event-card"]) as g:
                        with gr.Row(variant="compact"):
                            img = gr.Image(interactive=False, show_label=False, container=False, elem_classes=["card-img"])
                            info = gr.HTML(elem_classes=["card-info"])
                            eid = gr.Textbox(visible=False)
                        
                        # 리스트에 순서대로 추가
                        all_components.extend([g, img, info, eid])

        # 플로팅 버튼
        btn_create = gr.Button("+", elem_classes=["fab-btn"])

    # 모달 (팝업)
    overlay = gr.Group(visible=False, elem_classes=["modal-overlay"])
    with overlay:
        with gr.Column(elem_classes=["modal-content"]):
            gr.Markdown("### 모임 만들기")
            in_title = gr.Textbox(label="모임 이름")
            with gr.Row():
                in_start = gr.Textbox(label="시작", value="19:00")
                in_end = gr.Textbox(label="종료", value="21:00")
            in_addr = gr.Textbox(label="장소")
            in_cap = gr.Slider(2, 100, value=4, label="정원")
            
            with gr.Row():
                btn_cancel = gr.Button("취소")
                btn_save = gr.Button("완료", variant="primary")
            msg_box = gr.Textbox(visible=False)

    # 이벤트 연결
    btn_create.click(fn=open_modal, outputs=[overlay, overlay])
    btn_cancel.click(fn=close_modal, outputs=[overlay, overlay])
    
    # 저장 -> 모달 닫기 -> 리스트 갱신
    btn_save.click(
        fn=save_event,
        inputs=[in_title, in_start, in_end, in_addr, in_cap],
        outputs=[msg_box, overlay, overlay]
    ).then(
        fn=refresh_view,
        outputs=all_components # 여기가 핵심: 위에서 만든 리스트 전체를 넣음
    )

    # 시작 시 로드
    demo.load(fn=refresh_view, outputs=all_components)

if __name__ == "__main__":
    demo.launch()
