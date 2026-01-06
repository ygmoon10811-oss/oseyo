# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
import requests
from PIL import Image
import gradio as gr
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# [1. 환경 설정 및 오류 방지]
# ⚠️ 중요: API 키가 없으면 기능을 못 쓰므로 빈 문자열 처리
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "")
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "")

# ⚠️ 수정됨: 윈도우/맥 호환을 위해 현재 폴더(os.getcwd)에 DB 생성
BASE_DIR = os.getcwd()
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "oseyo.db")

def now_str():
    """현재 시간을 문자열로 반환 (복잡한 타임존 제거)"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M")

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

# DB 초기화
try:
    with db_conn() as con:
        con.execute("CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, title TEXT, photo_b64 TEXT, start_iso TEXT, end_iso TEXT, address TEXT, lat REAL, lng REAL, created_at TEXT);")
        con.execute("CREATE TABLE IF NOT EXISTS favorites (activity TEXT PRIMARY KEY, created_at TEXT);")
        con.commit()
    print(f"✅ 데이터베이스 연결 성공: {DB_PATH}")
except Exception as e:
    print(f"❌ 데이터베이스 생성 실패: {e}")

# [2. CSS]
CSS = """
.main-modal {
    position: fixed !important; left: 50% !important; top: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: min(500px, 95vw) !important; height: 85vh !important;
    background: #fff !important; border-radius: 20px !important;
    z-index: 10001 !important; display: flex !important; flex-direction: column !important;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5) !important; border: 1px solid #ddd !important;
}
.sub-modal {
    position: fixed !important; left: 50% !important; top: 55% !important;
    transform: translate(-50%, -50%) !important;
    width: min(450px, 90vw) !important; height: 60vh !important;
    background: #f9f9f9 !important; border-radius: 15px !important;
    z-index: 10005 !important; display: flex !important; flex-direction: column !important;
    box-shadow: 0 10px 40px rgba(0,0,0,0.6) !important; border: 1px solid #aaa !important;
}
.scroll-body { flex: 1 !important; overflow-y: auto !important; padding: 20px !important; display: flex !important; flex-direction: column !important; gap: 15px !important; }
.fav-grid { display: grid !important; grid-template-columns: 1fr 1fr !important; gap: 8px !important; margin-bottom: 10px; }
#fab-btn { position: fixed !important; right: 20px !important; bottom: 20px !important; z-index: 2000 !important; }
#fab-btn button { width: 60px !important; height: 60px !important; border-radius: 50% !important; background: #ff6b00 !important; color: white !important; font-size: 30px !important; }
#over { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 10000; display: none; }
"""

# [3. Gradio 로직]
with gr.Blocks(css=CSS, title="오세요 - 모임 공간") as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    with gr.Tabs():
        with gr.Tab("탐색"):
            gr.Markdown("### 🏠 개설된 공간 목록")
            gr.HTML("새로고침하면 목록이 갱신됩니다.")
        with gr.Tab("지도"):
            gr.HTML('<iframe src="/map" style="width:100%;height:600px;border:none;"></iframe>')

    fab_btn = gr.Button("+", elem_id="fab-btn")
    overlay = gr.HTML("<div id='over'></div>", visible=False)

    # 모달 1: 입력창
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal:
        gr.HTML("<div style='padding:15px;text-align:center;font-weight:bold;border-bottom:1px solid #eee;'>새 공간 만들기</div>")
        with gr.Column(elem_classes=["scroll-body"]):
            act_in = gr.Textbox(label="활동명", placeholder="예: 독서 모임")
            
            gr.Markdown("💡 **최근 활동**")
            with gr.Row(elem_classes=["fav-grid"]):
                fav_btns = [gr.Button("", visible=False) for _ in range(4)] # 버튼 개수 줄임(안전)

            img_in = gr.Image(label="사진", type="numpy", height=150)
            
            with gr.Row():
                st_in = gr.Textbox(label="시작", value=now_str)
                en_in = gr.Textbox(label="종료", value=lambda: (datetime.now()+timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"))
            
            addr_display = gr.Textbox(label="장소", interactive=False, placeholder="검색 버튼을 누르세요")
            addr_open_btn = gr.Button("📍 장소 검색", variant="secondary")

        with gr.Row(style="padding:15px;"):
            cancel_btn = gr.Button("닫기")
            save_btn = gr.Button("✅ 생성", variant="primary")

    # 모달 2: 주소 검색
    with gr.Column(visible=False, elem_classes=["sub-modal"]) as sub_modal:
        gr.HTML("<div style='padding:10px;font-weight:bold;'>📍 장소 찾기 (Kakao)</div>")
        with gr.Column(elem_classes=["scroll-body"]):
            loc_in = gr.Textbox(label="검색어", placeholder="예: 강남역 카페")
            loc_btn = gr.Button("검색")
            loc_sel = gr.Radio(label="결과 선택", choices=[])
        
        with gr.Row(style="padding:10px;"):
            sub_close_btn = gr.Button("취소")
            addr_confirm_btn = gr.Button("선택 완료", variant="primary")

    # --- 이벤트 핸들러 ---
    
    # 메인 모달 열기
    def open_modal():
        favs = []
        try:
            with db_conn() as con:
                favs = [r[0] for r in con.execute("SELECT activity FROM favorites ORDER BY created_at DESC LIMIT 4").fetchall()]
        except: pass
        
        updates = [gr.update(visible=False)] * 4
        for i, f in enumerate(favs):
            updates[i] = gr.update(visible=True, value=f)
        return [gr.update(visible=True), gr.update(visible=True)] + updates

    fab_btn.click(open_modal, None, [overlay, modal, *fav_btns])
    
    # 모달 닫기
    def close_modal(): return [gr.update(visible=False)] * 2
    cancel_btn.click(close_modal, None, [overlay, modal])

    # 주소 검색창 열기/닫기
    addr_open_btn.click(lambda: gr.update(visible=True), None, sub_modal)
    sub_close_btn.click(lambda: gr.update(visible=False), None, sub_modal)

    # 즐겨찾기 입력
    for b in fav_btns:
        b.click(lambda x: x, b, act_in)

    # 카카오 검색
    def search_kakao(q):
        if not KAKAO_REST_API_KEY:
            return [], gr.update(choices=["API 키가 설정되지 않았습니다."])
        try:
            url = "https://dapi.kakao.com/v2/local/search/keyword.json"
            headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
            res = requests.get(url, headers=headers, params={"query": q, "size": 5})
            if res.status_code != 200:
                return [], gr.update(choices=[f"API 오류: {res.status_code}"])
            
            docs = res.json().get("documents", [])
            cands = [{"label": f"{d['place_name']} ({d['address_name']})", "name": d['place_name'], "y": d['y'], "x": d['x']} for d in docs]
            
            if not cands: return [], gr.update(choices=["검색 결과가 없습니다."])
            return cands, gr.update(choices=[x['label'] for x in cands])
        except Exception as e:
            return [], gr.update(choices=[f"에러 발생: {e}"])

    loc_btn.click(search_kakao, loc_in, [search_state, loc_sel])

    # 주소 선택 확정
    def select_addr(sel, cands):
        found = next((c for c in cands if c['label'] == sel), None)
        if not found: return gr.update(), {}, gr.update()
        return found['label'], found, gr.update(visible=False)

    addr_confirm_btn.click(select_addr, [loc_sel, search_state], [addr_display, selected_addr, sub_modal])

    # 저장 로직
    def save_data(act, st, en, img, addr_obj):
        if not act: return "⚠️ 활동명을 적어주세요!"
        # 주소 없으면 임시 좌표 (서울시청)
        lat, lng, addr_name = 37.5665, 126.9780, "장소 미지정"
        
        if addr_obj and 'name' in addr_obj:
            lat, lng, addr_name = addr_obj['y'], addr_obj['x'], addr_obj['name']
        
        # 이미지 처리
        pic_str = ""
        if img is not None:
            try:
                im = Image.fromarray(img)
                if im.mode == 'RGBA': im = im.convert('RGB')
                buf = io.BytesIO()
                im.save(buf, format='JPEG')
                pic_str = base64.b64encode(buf.getvalue()).decode()
            except: pass # 이미지 실패해도 진행

        try:
            with db_conn() as con:
                con.execute("INSERT INTO spaces VALUES (?,?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex[:8], act, pic_str, st, en, addr_name, lat, lng, datetime.now().isoformat()))
                con.execute("INSERT OR IGNORE INTO favorites VALUES (?,?)", (act, datetime.now().isoformat()))
                con.commit()
            return "✅ 등록 성공! 지도를 확인하세요."
        except Exception as e:
            return f"DB 저장 실패: {e}"

    save_btn.click(save_data, [act_in, st_in, en_in, img_in, selected_addr], None).then(
        lambda: [gr.update(visible=False)]*2, None, [overlay, modal]
    )

# [4. FastAPI 서버]
app = FastAPI()

@app.get("/map")
def map_view():
    rows = []
    try:
        with db_conn() as con:
            rows = con.execute("SELECT title, lat, lng FROM spaces").fetchall()
    except: pass
    
    # JS 키 없으면 경고 마커 없이 지도만
    sdk_script = f"<script src='//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}'></script>" if KAKAO_JAVASCRIPT_KEY else ""
    
    html = f"""
    <html>
    <body style='margin:0'>
        <div id='m' style='width:100%;height:100vh;background:#eee;display:flex;align-items:center;justify-content:center;'>
            { '지도가 로딩됩니다...' if KAKAO_JAVASCRIPT_KEY else '⚠️ KAKAO_JAVASCRIPT_KEY가 설정되지 않았습니다.' }
        </div>
        {sdk_script}
        <script>
            if (window.kakao) {{
                var map = new kakao.maps.Map(document.getElementById('m'), {{ center: new kakao.maps.LatLng(37.5665, 126.9780), level: 7 }});
                var data = {json.dumps(rows)};
                data.forEach(r => {{
                    new kakao.maps.Marker({{ map: map, position: new kakao.maps.LatLng(r[1], r[2]), title: r[0] }});
                }});
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(html)

app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    print("🚀 서버 시작: http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
