# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
import requests
from PIL import Image
import gradio as gr
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# [1. 설정 및 DB]
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()
DB_PATH = "/tmp/oseyo_pro.db" 

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

with db_conn() as con:
    con.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, title TEXT, photo TEXT, start TEXT, end TEXT, addr TEXT, lat REAL, lng REAL, created_at TEXT);")
    con.execute("CREATE TABLE IF NOT EXISTS favs (name TEXT PRIMARY KEY, count INTEGER DEFAULT 1);")
    con.commit()

# [2. CSS] - 가로 스크롤 절대 방지 및 섬세한 UI 디자인
CSS = """
/* 기본 레이아웃: 가로 스크롤 원천 봉쇄 */
body, .gradio-container { 
    overflow-x: hidden !important; 
    max-width: 100vw !important; 
    margin: 0 !important; 
    padding: 0 !important;
}

/* 메인 컨테이너 스크롤 설정 */
.main-scroller {
    height: 100vh;
    overflow-y: auto !important;
    overflow-x: hidden !important;
}

/* 플로팅 버튼 (+) */
#fab-btn {
    position: fixed !important; 
    right: 25px !important; 
    bottom: 35px !important; 
    z-index: 1000;
}
#fab-btn button {
    width: 65px !important; height: 65px !important; 
    border-radius: 50% !important; 
    background: linear-gradient(135deg, #ff6b00, #ff8e3c) !important;
    color: white !important; font-size: 32px !important;
    box-shadow: 0 8px 20px rgba(255,107,0,0.4) !important;
    border: none !important;
}

/* 메인 모달 (생성창) */
.main-modal {
    position: fixed !important; top: 50% !important; left: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: 92% !important; max-width: 480px !important; height: 85vh !important;
    background: white !important; z-index: 10001 !important;
    border-radius: 24px !important; display: flex !important; flex-direction: column !important;
    box-shadow: 0 25px 50px rgba(0,0,0,0.3) !important;
}
.modal-content { flex: 1; overflow-y: auto; padding: 20px; gap: 15px; display: flex; flex-direction: column; }

/* 주소 검색 모달 (모달 위 모달) */
.sub-modal {
    position: fixed !important; top: 52% !important; left: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: 88% !important; max-width: 420px !important; height: 65vh !important;
    background: #fdfdfd !important; z-index: 10005 !important;
    border-radius: 20px !important; border: 1px solid #eee !important;
    box-shadow: 0 15px 40px rgba(0,0,0,0.4) !important;
}

/* 2x5 즐겨찾기 버튼 그리드 */
.fav-grid { 
    display: grid !important; 
    grid-template-columns: 1fr 1fr !important; 
    gap: 10px !important; 
    padding: 5px 0;
}
.fav-btn { border-radius: 12px !important; background: #f0f2f5 !important; border: none !important; transition: all 0.2s; }
.fav-btn:hover { background: #e4e6e9 !important; }

/* 탐색 탭 카드 디자인 */
.event-card {
    background: white; border-radius: 16px; margin-bottom: 15px;
    box-shadow: 0 2px 10px rgba(0,0,0,0.05); border: 1px solid #eee; overflow: hidden;
}
.event-info { padding: 15px; }

#overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 10000; backdrop-filter: blur(2px); }
"""

# [3. 로직 함수]
def get_event_list_html():
    try:
        with db_conn() as con:
            rows = con.execute("SELECT title, photo, start, addr FROM events ORDER BY created_at DESC").fetchall()
        if not rows: return "<div style='text-align:center; padding:50px; color:#999;'>아직 생성된 이벤트가 없습니다.</div>"
        
        html = "<div style='padding:15px;'>"
        for r in rows:
            img_tag = f"<img src='data:image/jpeg;base64,{r[1]}' style='width:100%; height:180px; object-fit:cover;'>" if r[1] else ""
            html += f"""
            <div class='event-card'>
                {img_tag}
                <div class='event-info'>
                    <div style='font-weight:bold; font-size:18px; margin-bottom:5px;'>{r[0]}</div>
                    <div style='color:#666; font-size:14px;'>📅 {r[2]}</div>
                    <div style='color:#666; font-size:14px;'>📍 {r[3]}</div>
                </div>
            </div>
            """
        html += "</div>"
        return html
    except: return "목록을 불러오는 중 오류가 발생했습니다."

def save_event(title, img, start, end, addr_obj):
    if not title: return "제목을 입력해주세요."
    pic = ""
    if img is not None:
        try:
            im = Image.fromarray(img).convert("RGB")
            im.thumbnail((500, 500))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            pic = base64.b64encode(buf.getvalue()).decode()
        except: pass
    
    with db_conn() as con:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", 
                   (uuid.uuid4().hex[:8], title, pic, start, end, addr_obj.get('name',''), addr_obj.get('y',0), addr_obj.get('x',0), datetime.now().isoformat()))
        con.execute("INSERT INTO favs (name) VALUES (?) ON CONFLICT(name) DO UPDATE SET count=count+1", (title,))
        con.commit()
    return "SUCCESS"

# [4. UI 구성]
with gr.Blocks(css=CSS, title="오세요 PRO") as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    with gr.Column(elem_classes=["main-scroller"]):
        with gr.Tabs() as main_tabs:
            with gr.Tab("탐색", id="tab_exp"):
                list_html = gr.HTML(get_event_list_html())
            
            with gr.Tab("지도", id="tab_map"):
                gr.HTML(f'<iframe src="/map" style="width:100%;height:80vh;border:none;"></iframe>')

    # 플로팅 버튼 & 오버레이
    fab = gr.Button("+", elem_id="fab-btn")
    overlay = gr.HTML("<div id='overlay'></div>", visible=False)

    # [Main Modal]
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_main:
        gr.HTML("<div style='padding:20px 20px 10px; font-size:20px; font-weight:bold;'>새 이벤트 등록</div>")
        with gr.Column(elem_classes=["modal-content"]):
            in_title = gr.Textbox(label="이벤트명", placeholder="어떤 활동인가요?")
            
            with gr.Column():
                gr.HTML("<span style='font-size:13px; color:#777;'>자주 생성하는 이벤트 (2x5)</span>")
                with gr.Column(elem_classes=["fav-grid"]):
                    f_btns = [gr.Button("", visible=False, elem_classes=["fav-btn"]) for _ in range(10)]
            
            in_img = gr.Image(label="이미지 (선택)", type="numpy")
            
            with gr.Row():
                in_start = gr.Textbox(label="시작 일시", value=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
                in_end = gr.Textbox(label="종료 일시", value=lambda: (datetime.now()+timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"))
            
            with gr.Group():
                addr_display = gr.Textbox(label="장소 정보", placeholder="주소를 검색하세요", interactive=False)
                addr_btn = gr.Button("📍 주소 찾기", variant="secondary")

            with gr.Row():
                btn_close = gr.Button("취소")
                btn_save = gr.Button("이벤트 생성", variant="primary")

    # [Sub Modal: Address]
    with gr.Column(visible=False, elem_classes=["sub-modal"]) as modal_sub:
        gr.HTML("<div style='padding:15px; font-weight:bold; border-bottom:1px solid #eee;'>주소 검색</div>")
        with gr.Column(style="padding:15px; gap:10px;"):
            q_in = gr.Textbox(label="검색어", placeholder="장소명이나 주소")
            q_btn = gr.Button("검색")
            q_results = gr.Radio(label="검색 결과", choices=[])
            with gr.Row():
                q_close = gr.Button("뒤로")
                q_final = gr.Button("선택 확정", variant="primary")

    # --- 이벤트 핸들링 ---
    def open_modal():
        with db_conn() as con:
            fav_data = con.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 10").fetchall()
        updates = [gr.update(visible=False)] * 10
        for i, f in enumerate(fav_data): updates[i] = gr.update(visible=True, value=f[0])
        return [gr.update(visible=True), gr.update(visible=True)] + updates

    fab.click(open_modal, None, [overlay, modal_main, *f_btns])
    btn_close.click(lambda: [gr.update(visible=False)]*2, None, [overlay, modal_main])
    
    for b in f_btns: b.click(lambda x: x, b, in_title)

    # 주소 검색 로직
    addr_btn.click(lambda: gr.update(visible=True), None, modal_sub)
    
    def do_search(q):
        if not KAKAO_REST_API_KEY: return [], gr.update(choices=["REST API 키를 확인하세요."])
        res = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", 
                           headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"},
                           params={"query": q, "size": 6}).json()
        docs = res.get("documents", [])
        cands = [{"label": f"{d['place_name']} | {d['address_name']}", "name": d['place_name'], "y": d['y'], "x": d['x']} for d in docs]
        return cands, gr.update(choices=[x['label'] for x in cands])

    q_btn.click(do_search, q_in, [search_state, q_results])

    def select_addr(sel, cands):
        found = next((x for x in cands if x['label'] == sel), None)
        if not found: return gr.update(), {}, gr.update()
        # 선택하면 주소 옵션(라디오박스)을 비우고 쏙 들어가게 처리
        return found['label'], found, gr.update(visible=False, choices=[])

    q_final.click(select_addr, [q_results, search_state], [addr_display, selected_addr, modal_sub])
    q_close.click(lambda: gr.update(visible=False), None, modal_sub)

    # 저장 및 자동 갱신
    btn_save.click(save_event, [in_title, in_img, in_start, in_end, selected_addr], None).then(
        get_event_list_html, None, list_html
    ).then(
        lambda: [gr.update(visible=False)]*2, None, [overlay, modal_main]
    )

# [5. FastAPI 서버]
app = FastAPI()

@app.get("/map")
def draw_map():
    with db_conn() as con: rows = con.execute("SELECT title, lat, lng FROM events").fetchall()
    sdk = f"//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}" if KAKAO_JAVASCRIPT_KEY else ""
    return HTMLResponse(f"""
        <div id='m' style='width:100%;height:100vh;'></div>
        <script src='{sdk}'></script>
        <script>
            if(window.kakao){{
                var m=new kakao.maps.Map(document.getElementById('m'),{{center:new kakao.maps.LatLng(37.56,126.97),level:7}});
                {json.dumps(rows)}.forEach(r=>new kakao.maps.Marker({{map:m,position:new kakao.maps.LatLng(r[1],r[2]),title:r[0]}}));
            }}
        </script>
    """)

app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
