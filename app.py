# -*- coding: utf-8 -*-
import os
import io
import uuid
import json
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

# DB 파일명
DB_PATH = "events.db"

# =========================================================
# 1) 데이터베이스 초기화 (없으면 생성)
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
            lat_lng TEXT,
            cap_type TEXT,
            cap_val INTEGER,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# =========================================================
# 2) DB 핸들링 함수
# =========================================================
def save_event_db(title, img_path, start, end, addr, latlng, cap_t, cap_v):
    eid = str(uuid.uuid4())
    created = now_kst().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO events (eid, title, img_path, start_time, end_time, addr_text, lat_lng, cap_type, cap_val, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (eid, title, img_path, start, end, addr, latlng, cap_t, cap_v, created))
    conn.commit()
    conn.close()
    return "저장 완료"

def get_events_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # 최신순 정렬
    cur.execute("SELECT * FROM events ORDER BY created_at DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

def delete_event_db(eid):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM events WHERE eid=?", (eid,))
    conn.commit()
    conn.close()
    return "삭제 완료"

# =========================================================
# 3) UI 로직
# =========================================================

# CSS: 모바일 뷰와 카드 디자인을 위한 스타일
CSS = """
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+KR:wght@400;700&display=swap');

body, gradio-app {
    font-family: 'Noto Sans KR', sans-serif !important;
    background-color: #f0f2f5;
}

/* 모바일 화면 시뮬레이션 컨테이너 */
.app-container {
    max-width: 420px !important;
    margin: 0 auto !important;
    background-color: white;
    min-height: 100vh;
    box-shadow: 0 0 15px rgba(0,0,0,0.1);
    position: relative;
    padding-bottom: 80px; /* 버튼 공간 확보 */
}

/* 헤더 */
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

/* 탭 스타일 커스텀 */
.custom-tabs button {
    font-weight: bold;
}
.custom-tabs button.selected {
    color: #ff6f0f !important; /* 당근색 */
    border-bottom: 2px solid #ff6f0f !important;
}

/* 리스트 아이템 (카드) */
.event-card {
    border-bottom: 1px solid #f0f0f0;
    padding: 15px;
    display: flex;
    gap: 12px;
    background: white;
    transition: background 0.2s;
    cursor: pointer;
}
.event-card:hover {
    background-color: #f9f9f9;
}

/* 카드 내부 이미지 */
.card-img {
    width: 90px !important;
    height: 90px !important;
    border-radius: 8px !important;
    object-fit: cover;
    flex-shrink: 0;
    overflow: hidden;
    background-color: #eee;
}
.card-img img {
    width: 100%;
    height: 100%;
    object-fit: cover;
}

/* 카드 텍스트 정보 */
.card-info {
    flex-grow: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 4px;
}
.card-title {
    font-size: 16px;
    font-weight: bold;
    color: #222;
    line-height: 1.3;
}
.card-meta {
    font-size: 13px;
    color: #888;
}
.card-tag {
    font-size: 12px;
    color: #ff6f0f;
    font-weight: 500;
}

/* 플로팅 버튼 (글쓰기) */
.fab-btn {
    position: fixed !important;
    bottom: 25px;
    left: 50%;
    transform: translateX(140px); /* 컨테이너 기준 우측 배치 */
    width: 56px !important;
    height: 56px !important;
    border-radius: 50% !important;
    background: #ff6f0f !important;
    box-shadow: 0 4px 10px rgba(255, 111, 15, 0.4) !important;
    border: none !important;
    color: white !important;
    font-size: 24px !important;
    display: flex !important;
    align-items: center;
    justify-content: center;
    z-index: 999;
}
.fab-btn:hover {
    background: #e65c00 !important;
}

/* 모달 (팝업) 스타일 */
.modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100%;
    height: 100%;
    background: rgba(0,0,0,0.5);
    z-index: 2000;
    display: flex;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(2px);
}
.modal-content {
    background: white;
    width: 90%;
    max-width: 400px;
    max-height: 90vh;
    border-radius: 16px;
    padding: 20px;
    overflow-y: auto;
    box-shadow: 0 10px 25px rgba(0,0,0,0.2);
}

/* 숨김 유틸리티 */
.hidden { display: none !important; }
"""

# HTML 생성 헬퍼
def make_card_html(title, start_time, addr):
    # 날짜 포맷팅 등은 생략하고 단순 표시
    return f"""
    <div class='card-title'>{html.escape(title)}</div>
    <div class='card-meta'>{html.escape(addr)}</div>
    <div class='card-meta'>{html.escape(start_time)}</div>
    """

# 최대 표시 개수
MAX_ITEMS = 10

def refresh_view():
    rows = get_events_db() # [(eid, title, img, ...), ...]
    
    # 1. 참여 가능 목록 (Joined Items) 업데이트 데이터 생성
    # 실제로는 '모든 이벤트'를 보여줌
    joined_updates = []
    
    for i in range(MAX_ITEMS):
        if i < len(rows):
            row = rows[i]
            # row: 0=eid, 1=title, 2=img, 3=start, 4=end, 5=addr...
            eid, title, img_p, start, end, addr = row[0], row[1], row[2], row[3], row[4], row[5]
            
            # 카드 보이기, 이미지 설정, HTML 내용 설정, EID 설정
            joined_updates.append(gr.update(visible=True))       # Group visible
            joined_updates.append(gr.update(value=img_p))        # Image
            joined_updates.append(gr.update(value=make_card_html(title, start, addr))) # HTML
            joined_updates.append(gr.update(value=eid))          # Hidden EID
        else:
            # 데이터 없으면 숨기기
            joined_updates.append(gr.update(visible=False))
            joined_updates.append(gr.update())
            joined_updates.append(gr.update())
            joined_updates.append(gr.update())

    # 2. 내 이벤트 (My Items) - 여기서는 단순히 동일한 DB를 쓴다고 가정 (실제 구현 시 필터링 필요)
    # 편의상 동일하게 처리하거나 비워둠. 여기서는 숨김 처리 예시.
    my_updates = []
    for i in range(MAX_ITEMS):
         my_updates.append(gr.update(visible=False)) # 일단 다 숨김 (데모용)
         my_updates.append(gr.update())
         my_updates.append(gr.update())
         my_updates.append(gr.update())
         my_updates.append(gr.update())

    return joined_updates + my_updates

# 이벤트 저장 로직
def save_event(title, img, start, end, addr, cap_v):
    if not title:
        return gr.update(), gr.update(), gr.update() # 에러 처리 생략
        
    # 이미지 저장 (임시)
    img_path = None
    if img is not None:
        # Gradio Image는 numpy array이거나 filepath일 수 있음
        # 여기서는 간단히 경로가 넘어온다고 가정하거나 처리 필요.
        # 편의상 None 처리 (실제 구현시 PIL 저장 필요)
        pass 
        
    save_event_db(title, "https://dummyimage.com/100x100/eee/999&text=IMG", start, end, addr, "", "unlimited", cap_v)
    
    # 저장 후 모달 닫고 리스트 갱신은 then()으로 처리
    return "저장됨", gr.update(visible=False), gr.update(visible=False) # msg, overlay, modal

# 모달 제어
def open_modal():
    return gr.update(visible=True), gr.update(visible=True)

def close_modal():
    return gr.update(visible=False), gr.update(visible=False)


# =========================================================
# 4) Gradio 화면 구성
# =========================================================
with gr.Blocks(css=CSS, title="오세요 - 모임") as demo:
    
    # 전체를 감싸는 모바일 뷰 컨테이너
    with gr.Column(elem_classes=["app-container"]):
        
        # [헤더]
        with gr.Row(elem_classes=["header-bar"]):
            gr.Markdown("### 오세요", elem_classes=["header-title"])

        # [탭 메뉴]
        with gr.Tabs(elem_classes=["custom-tabs"]):
            
            # [탭 1: 모임 찾기]
            with gr.TabItem("모임 찾기"):
                gr.Markdown("지금 핫한 모임들을 확인해보세요!")
                
                # 리스트 생성 (동적 아이템)
                joined_wraps = []
                joined_imgs = []
                joined_infos = []
                joined_eids = []
                
                for i in range(MAX_ITEMS):
                    # 중요: elem_classes="event-card"를 줘서 CSS 적용
                    with gr.Group(visible=False, elem_classes=["event-card"]) as wrap:
                        with gr.Row(variant="compact"):
                            # 좌측 이미지
                            # interactive=False여야 업로드 버튼이 안 뜸
                            img = gr.Image(
                                show_label=False, 
                                interactive=False, 
                                show_download_button=False,
                                elem_classes=["card-img"],
                                container=False
                            )
                            # 우측 정보
                            info = gr.HTML(elem_classes=["card-info"])
                            # 숨겨진 ID
                            eid = gr.Textbox(visible=False)
                            
                        joined_wraps.append(wrap)
                        joined_imgs.append(img)
                        joined_infos.append(info)
                        joined_eids.append(eid)
            
            # [탭 2: 내 모임]
            with gr.TabItem("내 모임"):
                gr.Markdown("내가 만든 모임 관리")
                my_wraps = []
                # (구조 동일, 생략 가능하지만 오류 방지위해 변수만 선언)
                for i in range(MAX_ITEMS):
                     with gr.Group(visible=False):
                         gr.Markdown("내 모임 아이템")
                         my_wraps.append(gr.Group())
                         # ... 리스트 채우기 (생략)
                
                # refresh_view 반환 개수 맞추기 위해 더미 생성 로직 필요
                # (이 코드는 데모용으로 위 refresh_view에서 처리함)

        # [플로팅 버튼 (글쓰기)]
        # elem_classes="fab-btn" 필수
        btn_create = gr.Button("+", elem_classes=["fab-btn"])


    # =========================================================
    # [모달 창] (화면 밖/위에 띄움)
    # =========================================================
    overlay = gr.Group(visible=False, elem_classes=["modal-overlay"])
    with overlay:
        with gr.Column(elem_classes=["modal-content"]):
            gr.Markdown("### 모임 만들기")
            
            in_title = gr.Textbox(label="모임 이름", placeholder="예: 한강 러닝 하실 분")
            in_img = gr.Image(label="대표 사진", type="pil", height=150)
            
            with gr.Row():
                in_start = gr.Textbox(label="시작 시간", placeholder="2024-01-01 19:00")
                in_end = gr.Textbox(label="종료 시간", placeholder="2024-01-01 21:00")
            
            in_addr = gr.Textbox(label="장소", placeholder="강남역 11번 출구")
            in_cap = gr.Slider(minimum=2, maximum=100, value=4, label="정원")
            
            with gr.Row():
                btn_cancel = gr.Button("취소", variant="secondary")
                btn_save = gr.Button("완료", variant="primary")
                
            # 상태 메시지
            msg_box = gr.Textbox(visible=False)

    # =========================================================
    # 5) 이벤트 연결
    # =========================================================
    
    # 1. 초기 로딩 및 저장 후 리스트 갱신
    # 리턴 순서: joined_wraps(10개) + joined_imgs(10개)... -> 너무 많으므로
    # 위 refresh_view는 [wrap, img, info, eid] * 10 형태로 플랫하게 리턴함.
    
    # 출력 리스트 만들기 (Flatten)
    all_outputs = []
    # Joined Items Output
    for i in range(MAX_ITEMS):
        all_outputs.append(joined_wraps[i])
        all_outputs.append(joined_imgs[i])
        all_outputs.append(joined_infos[i])
        all_outputs.append(joined_eids[i])
    # My Items Output (개수만 맞춤)
    # 실제로는 컴포넌트 객체를 넣어야 함. 여기서는 데모용 더미 처리
    # (실제 사용시에는 my_wraps 등도 위와 똑같이 flatten해서 넣어야 함)
    # 에러 방지를 위해 단순화:
    # refresh_view가 반환하는 개수와 아래 outputs 개수가 정확히 일치해야 함.
    
    # 앱 실행 시 자동 로드 (데모를 위해 리스트 갱신 로직 연결은 생략하고 UI 구조만 잡음)
    # 실제 연결 시: demo.load(fn=refresh_view, outputs=all_outputs)

    # 2. 글쓰기 버튼 -> 모달 열기
    btn_create.click(fn=open_modal, outputs=[overlay, overlay]) # overlay를 두 번 쓴 건 visible=True 두 개 리턴 받기 위함
    
    # 3. 취소 버튼 -> 모달 닫기
    btn_cancel.click(fn=close_modal, outputs=[overlay, overlay])
    
    # 4. 저장 버튼 -> 저장 및 닫기
    btn_save.click(
        fn=save_event,
        inputs=[in_title, in_img, in_start, in_end, in_addr, in_cap],
        outputs=[msg_box, overlay, overlay]
    ).then(
        fn=refresh_view,
        outputs=all_outputs
    )
    
    # 5. 첫 실행 시 리스트 불러오기
    demo.load(fn=refresh_view, outputs=all_outputs)

if __name__ == "__main__":
    demo.launch()
