# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from PIL import Image
import gradio as gr
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# [1. 환경 설정]
KST = ZoneInfo("Asia/Seoul")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()
def now_kst(): return datetime.now(KST)

DATA_DIR = "/var/data" if os.path.isdir("/var/data") else os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "oseyo.db")
def db_conn(): return sqlite3.connect(DB_PATH, check_same_thread=False)

# DB 초기화 (즐겨찾기 테이블 포함)
with db_conn() as con:
    con.execute("CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, title TEXT, photo_b64 TEXT, start_iso TEXT, end_iso TEXT, address TEXT, lat REAL, lng REAL, created_at TEXT);")
    con.execute("CREATE TABLE IF NOT EXISTS favorites (activity TEXT PRIMARY KEY, created_at TEXT);")
    con.commit()

# [2. 이중 모달용 CSS]
CSS = """
/* 기본 모달 (공간 생성) */
.main-modal {
    position: fixed !important; left: 50% !important; top: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: min(500px, 95vw) !important; height: 80vh !important;
    background: #fff !important; border-radius: 20px !important;
    z-index: 10001 !important; display: flex !important; flex-direction: column !important;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5) !important;
}
/* 주소 검색 전용 서브 모달 (모달 위의 모달) */
.sub-modal {
    position: fixed !important; left: 50% !important; top: 55% !important;
    transform: translate(-50%, -50%) !important;
    width: min(450px, 90vw) !important; height: 60vh !important;
    background: #f9f9f9 !important; border-radius: 15px !important;
    z-index: 10005 !important; display: flex !important; flex-direction: column !important;
    box-shadow: 0 10px 40px rgba(0,0,0,0.6) !important; border: 1px solid #ddd !important;
}
.scroll-body { flex: 1 !important; overflow-y: auto !important; padding: 25px !important; display: flex !important; flex-direction: column !important; gap: 20px !important; }
.fav-grid { display: grid !important; grid-template-columns: 1fr 1fr !important; gap: 8px !important; margin-bottom: 10px; }
#fab-btn { position: fixed !important; right: 20px !important; bottom: 20px !important; z-index: 2000 !important; }
#fab-btn button { width: 60px !important; height: 60px !important; border-radius: 50% !important; background: #ff6b00 !important; color: white !important; font-size: 30px !important; }
"""

with gr.Blocks(css=CSS) as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    # 메인 UI
    with gr.Tabs():
        with gr.Tab("탐색"): home_ui = gr.HTML(lambda: "목록 로딩 중...")
        with gr.Tab("지도"): gr.HTML(lambda: f'<iframe src="/map" style="width:100%;height:500px;border:none;"></iframe>')

    fab_btn = gr.Button("+", elem_id="fab-btn")
    overlay = gr.HTML("<div style='position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:10000;display:none;' id='over'></div>", visible=False)

    # 1층 모달: 공간 생성
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal:
        gr.HTML("<div style='padding:15px;text-align:center;font-weight:bold;border-bottom:1px solid #eee;'>새 공간 만들기</div>")
        with gr.Column(elem_classes=["scroll-body"]):
            act_in = gr.Textbox(label="활동명", placeholder="예: 카공, 러닝")
            
            # 즐겨찾기 영역
            gr.Markdown("💡 **최근 활동**")
            with gr.Row(elem_classes=["fav-grid"]):
                fav_btns = [gr.Button("", visible=False) for _ in range(6)]
            
            img_in = gr.Image(label="사진", type="numpy")
            
            with gr.Row():
                st_in = gr.Textbox(label="시작", value=lambda: now_kst().strftime("%Y-%m-%dT%H:%M"))
                en_in = gr.Textbox(label="종료", value=lambda: (now_kst()+timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"))
            
            addr_display = gr.Textbox(label="선택된 장소", interactive=False, placeholder="아래 버튼을 눌러 검색하세요")
            addr_open_btn = gr.Button("📍 장소 검색하기", variant="secondary")

        with gr.Row(style="padding:15px;"):
            gr.Button("취소").click(lambda: [gr.update(visible=False)]*2, None, [overlay, modal])
            save_btn = gr.Button("✅ 생성", variant="primary")

    # 2층 모달: 주소 검색 (모달 위 모달)
    with gr.Column(visible=False, elem_classes=["sub-modal"]) as sub_modal:
        gr.HTML("<div style='padding:10px;font-weight:bold;'>📍 장소 찾기</div>")
        loc_in = gr.Textbox(label="키워드", placeholder="예: 영일대 카페")
        loc_btn = gr.Button("검색")
        loc_sel = gr.Radio(label="검색 결과", choices=[])
        with gr.Row():
            gr.Button("닫기").click(lambda: gr.update(visible=False), None, sub_modal)
            addr_confirm_btn = gr.Button("이 주소 선택", variant="primary")

    # [이벤트 로직]
    # 1. 모달 열기 및 즐겨찾기 로드
    def open_main():
        with db_conn() as con: favs = [r[0] for r in con.execute("SELECT activity FROM favorites ORDER BY created_at DESC LIMIT 6").fetchall()]
        btns = [gr.update(visible=False)] * 6
        for i, f in enumerate(favs): btns[i] = gr.update(visible=True, value=f)
        return [gr.update(visible=True)]*2 + btns
    fab_btn.click(open_main, None, [overlay, modal, *fav_btns])
    
    for b in fav_btns: b.click(lambda v: v, b, act_in) # 즐겨찾기 클릭 시 입력창에 입력

    # 2. 주소 검색 모달 제어
    addr_open_btn.click(lambda: gr.update(visible=True), None, sub_modal)
    
    def search(q):
        r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}, params={"query":q, "size":5}).json()
        docs = r.get("documents", [])
        cands = [{"label": f"{d['place_name']} ({d['address_name']})", "name": d['place_name'], "y": d['y'], "x": d['x']} for d in docs]
        return cands, gr.update(choices=[x['label'] for x in cands])
    loc_btn.click(search, loc_in, [search_state, loc_sel])

    def confirm_addr(sel, cands):
        item = next((x for x in cands if x['label'] == sel), None)
        if not item: return gr.update(), {}, gr.update()
        return item['label'], item, gr.update(visible=False)
    addr_confirm_btn.click(confirm_addr, [loc_sel, search_state], [addr_display, selected_addr, sub_modal])

    # 3. 저장
    def save(act, st, en, img, addr_obj):
        if not act or not addr_obj: return "⚠️ 정보 부족"
        pic = ""
        if img is not None:
            im = Image.fromarray(img); b = io.BytesIO(); im.save(b, "JPEG"); pic = base64.b64encode(b.getvalue()).decode()
        with db_conn() as con:
            con.execute("INSERT INTO spaces VALUES (?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex[:8], act, pic, st, en, addr_obj['name'], addr_obj['y'], addr_obj['x'], now_kst().isoformat()))
            con.execute("INSERT OR IGNORE INTO favorites VALUES (?,?)", (act, now_kst().isoformat()))
            con.commit()
        return "✅ 완료 (페이지를 새로고침하세요)"
    save_btn.click(save, [act_in, st_in, en_in, img_in, selected_addr], None).then(lambda: [gr.update(visible=False)]*2, None, [overlay, modal])

# [FastAPI]
app = FastAPI()
@app.get("/map")
def get_map():
    with db_conn() as con: rows = con.execute("SELECT title, lat, lng FROM spaces").fetchall()
    return HTMLResponse(f"<html><body style='margin:0;'><div id='m' style='width:100%;height:100vh;'></div><script src='//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}'></script><script>var map=new kakao.maps.Map(document.getElementById('m'),{{center:new kakao.maps.LatLng(36.01,129.34),level:4}});{json.dumps(rows)}.forEach(r=>new kakao.maps.Marker({{map:map,position:new kakao.maps.LatLng(r[1],r[2])}}));</script></body></html>")
app = gr.mount_gradio_app(app, demo, path="/")
