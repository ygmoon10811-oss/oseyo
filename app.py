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

def now_kst(): 
    return datetime.now(KST)

DATA_DIR = "/var/data" if os.path.isdir("/var/data") else os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "oseyo.db")

def db_conn(): 
    return sqlite3.connect(DB_PATH, check_same_thread=False)

# DB 초기화
with db_conn() as con:
    con.execute("CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, title TEXT, photo_b64 TEXT, start_iso TEXT, end_iso TEXT, address TEXT, lat REAL, lng REAL, created_at TEXT);")
    con.execute("CREATE TABLE IF NOT EXISTS favorites (activity TEXT PRIMARY KEY, created_at TEXT);")
    con.commit()

# [2. CSS 스타일]
CSS = """
.main-modal {
    position: fixed !important; left: 50% !important; top: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: min(500px, 95vw) !important; height: 80vh !important;
    background: #fff !important; border-radius: 20px !important;
    z-index: 10001 !important; display: flex !important; flex-direction: column !important;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5) !important;
}
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
#over { position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 10000; display: none; }
"""

# [3. Gradio UI 구성]
with gr.Blocks(css=CSS) as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    # 메인 탭 화면
    with gr.Tabs():
        with gr.Tab("탐색"): 
            gr.Markdown("### 🏠 개설된 공간 목록")
            # 목록을 보여줄 HTML (초기엔 로딩 텍스트)
            home_ui = gr.HTML("목록을 불러오는 중입니다...", elem_id="home-list")
        
        with gr.Tab("지도"): 
            gr.HTML(f'<iframe src="/map" style="width:100%;height:600px;border:none;"></iframe>')

    # 플로팅 버튼 및 오버레이
    fab_btn = gr.Button("+", elem_id="fab-btn")
    overlay = gr.HTML("<div id='over'></div>", visible=False)

    # [모달 1] 공간 생성
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal:
        gr.HTML("<div style='padding:15px;text-align:center;font-weight:bold;border-bottom:1px solid #eee;'>새 공간 만들기</div>")
        
        with gr.Column(elem_classes=["scroll-body"]):
            act_in = gr.Textbox(label="활동명", placeholder="예: 카공, 러닝, 산책")
            
            gr.Markdown("💡 **최근 활동**")
            with gr.Row(elem_classes=["fav-grid"]):
                # 즐겨찾기 버튼 6개 미리 생성
                fav_btns = [gr.Button("", visible=False) for _ in range(6)]
            
            img_in = gr.Image(label="사진 (선택)", type="numpy")
            
            with gr.Row():
                st_in = gr.Textbox(label="시작", value=lambda: now_kst().strftime("%Y-%m-%dT%H:%M"))
                en_in = gr.Textbox(label="종료", value=lambda: (now_kst()+timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"))
            
            addr_display = gr.Textbox(label="선택된 장소", interactive=False, placeholder="아래 버튼을 눌러 검색하세요")
            addr_open_btn = gr.Button("📍 장소 검색하기", variant="secondary")

        with gr.Row(style="padding:15px;"):
            cancel_btn = gr.Button("취소")
            save_btn = gr.Button("✅ 생성", variant="primary")

    # [모달 2] 주소 검색 (Sub Modal)
    with gr.Column(visible=False, elem_classes=["sub-modal"]) as sub_modal:
        gr.HTML("<div style='padding:10px;font-weight:bold;'>📍 장소 찾기</div>")
        with gr.Column(elem_classes=["scroll-body"]):
            loc_in = gr.Textbox(label="키워드", placeholder="예: 영일대 카페")
            loc_btn = gr.Button("검색")
            loc_sel = gr.Radio(label="검색 결과", choices=[])
        
        with gr.Row(style="padding:10px;"):
            sub_close_btn = gr.Button("닫기")
            addr_confirm_btn = gr.Button("이 주소 선택", variant="primary")

    # ---------------- EVENT LOGIC ----------------

    # 1. 모달 열기 & 즐겨찾기 로딩
    def open_main():
        with db_conn() as con: 
            favs = [r[0] for r in con.execute("SELECT activity FROM favorites ORDER BY created_at DESC LIMIT 6").fetchall()]
        
        updates = [gr.update(visible=False)] * 6
        for i, f in enumerate(favs):
            updates[i] = gr.update(visible=True, value=f)
        
        return [gr.update(visible=True), gr.update(visible=True)] + updates

    # fab_btn 클릭 시: 오버레이+모달 보임(2개) + 버튼업데이트(6개) = 총 8개 출력
    fab_btn.click(open_main, None, [overlay, modal, *fav_btns])

    # 2. 모달 닫기
    def close_all(): return [gr.update(visible=False)] * 2
    cancel_btn.click(close_all, None, [overlay, modal])

    # 3. 즐겨찾기 버튼 클릭 시 입력창 채우기
    for b in fav_btns:
        b.click(lambda v: v, b, act_in)

    # 4. 주소 검색 모달 제어
    addr_open_btn.click(lambda: gr.update(visible=True), None, sub_modal)
    sub_close_btn.click(lambda: gr.update(visible=False), None, sub_modal)

    # 5. 카카오 주소 검색
    def search(q):
        if not KAKAO_REST_API_KEY:
            return [], gr.update(choices=["API 키가 없습니다."])
        try:
            url = "https://dapi.kakao.com/v2/local/search/keyword.json"
            headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
            r = requests.get(url, headers=headers, params={"query": q, "size": 5}).json()
            docs = r.get("documents", [])
            # 검색 상태 저장을 위한 딕셔너리 리스트 생성
            cands = [{"label": f"{d['place_name']} ({d['address_name']})", "name": d['place_name'], "y": d['y'], "x": d['x']} for d in docs]
            return cands, gr.update(choices=[x['label'] for x in cands])
        except Exception as e:
            return [], gr.update(choices=[f"에러: {str(e)}"])

    loc_btn.click(search, loc_in, [search_state, loc_sel])

    # 6. 주소 확정
    def confirm_addr(sel, cands):
        # 선택된 라벨과 일치하는 객체 찾기
        item = next((x for x in cands if x['label'] == sel), None)
        if not item: 
            return gr.update(), {}, gr.update()
        return item['label'], item, gr.update(visible=False)

    addr_confirm_btn.click(confirm_addr, [loc_sel, search_state], [addr_display, selected_addr, sub_modal])

    # 7. 최종 저장
    def save(act, st, en, img, addr_obj):
        if not act: return "⚠️ 활동명을 입력해주세요."
        if not addr_obj or 'name' not in addr_obj: return "⚠️ 장소를 선택해주세요."

        pic = ""
        if img is not None:
            try:
                im = Image.fromarray(img)
                # RGBA(투명) 이미지는 JPEG 저장이 안되므로 RGB로 변환
                if im.mode == 'RGBA':
                    im = im.convert('RGB')
                b = io.BytesIO()
                im.save(b, "JPEG")
                pic = base64.b64encode(b.getvalue()).decode()
            except Exception as e:
                print(f"이미지 처리 오류: {e}")

        try:
            with db_conn() as con:
                con.execute("INSERT INTO spaces VALUES (?,?,?,?,?,?,?,?,?)", 
                           (uuid.uuid4().hex[:8], act, pic, st, en, addr_obj['name'], addr_obj['y'], addr_obj['x'], now_kst().isoformat()))
                con.execute("INSERT OR IGNORE INTO favorites VALUES (?,?)", 
                           (act, now_kst().isoformat()))
                con.commit()
            return "✅ 생성 완료! (지도를 새로고침 하세요)"
        except Exception as e:
            return f"DB 에러: {str(e)}"

    save_btn.click(save, [act_in, st_in, en_in, img_in, selected_addr], None).then(
        lambda: [gr.update(visible=False)]*2, None, [overlay, modal]
    )

# [4. FastAPI 앱 마운트]
app = FastAPI()

@app.get("/map")
def get_map():
    # 지도 HTML 렌더링
    with db_conn() as con: 
        rows = con.execute("SELECT title, lat, lng FROM spaces").fetchall()
    
    # 카카오 지도 JS SDK
    html = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
    </head>
    <body style='margin:0;'>
        <div id='m' style='width:100%;height:100vh;'></div>
        <script src='//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}'></script>
        <script>
            var container = document.getElementById('m');
            var options = {{ center: new kakao.maps.LatLng(36.0190, 129.3435), level: 5 }};
            var map = new kakao.maps.Map(container, options);
            
            var data = {json.dumps(rows)};
            data.forEach(r => {{
                var marker = new kakao.maps.Marker({{
                    map: map,
                    position: new kakao.maps.LatLng(r[1], r[2]),
                    title: r[0]
                }});
            }});
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
