# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from PIL import Image
import gradio as gr
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

# 1. 환경 설정
KST = ZoneInfo("Asia/Seoul")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()
def now_kst(): return datetime.now(KST)

DATA_DIR = "/var/data" if os.path.isdir("/var/data") else os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "oseyo.db")
def db_conn(): return sqlite3.connect(DB_PATH, check_same_thread=False)

# DB 초기화
with db_conn() as con:
    con.execute("CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, title TEXT NOT NULL, photo_b64 TEXT DEFAULT '', start_iso TEXT NOT NULL, end_iso TEXT NOT NULL, address TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL, capacity_enabled INTEGER NOT NULL DEFAULT 0, capacity_max INTEGER, created_at TEXT NOT NULL);")
    con.execute("CREATE TABLE IF NOT EXISTS favorites (activity TEXT PRIMARY KEY, created_at TEXT NOT NULL);")
    con.commit()

# 2. 강제 간격 확보 CSS
CSS = """
.modal-sheet {
    position: fixed !important; left: 50% !important; top: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: min(520px, 95vw) !important; height: 85vh !important;
    background: #fff !important; border-radius: 20px !important;
    z-index: 10001 !important; display: flex !important; flex-direction: column !important;
    box-shadow: 0 20px 50px rgba(0,0,0,0.3) !important;
}
/* ⚠️ image_fe5265.png 문제를 해결하는 핵심 스크롤 영역 */
.scroll-body {
    flex: 1 !important; overflow-y: auto !important; padding: 20px !important;
    display: flex !important; flex-direction: column !important; gap: 25px !important;
}
.fav-grid { display: grid !important; grid-template-columns: 1fr 1fr !important; gap: 8px !important; }
.footer { padding: 15px; border-top: 1px solid #eee; display: flex !important; gap: 10px !important; }
#fab-btn { position: fixed !important; right: 20px !important; bottom: 20px !important; z-index: 2000 !important; }
#fab-btn button { width: 60px !important; height: 60px !important; border-radius: 50% !important; background: #ff6b00 !important; color: #fff !important; font-size: 30px !important; }
.map-frame { width: 100%; height: 500px; border: none; border-radius: 15px; }
"""

# 3. 렌더링 함수
def get_home_html():
    with db_conn() as con: rows = con.execute("SELECT id, title, photo_b64, start_iso, address FROM spaces ORDER BY created_at DESC").fetchall()
    if not rows: return "<div style='text-align:center;padding:50px;'>공간이 없습니다.</div>"
    h = ""
    for r in rows:
        img = f"data:image/jpeg;base64,{r[2]}" if r[2] else ""
        h += f"<div style='border:1px solid #eee;padding:15px;margin-bottom:10px;border-radius:12px;display:flex;justify-content:space-between;'>"
        h += f"<div><b>{r[1]}</b><br><small>{r[4]}</small></div>"
        if img: h += f"<img src='{img}' style='width:60px;height:60px;border-radius:8px;object-fit:cover;'>"
        h += f"</div>"
    return h

def get_map_html():
    return f'<iframe src="/kakao_map?t={uuid.uuid4().hex}" class="map-frame"></iframe>'

# 4. UI 구성
with gr.Blocks(css=CSS) as demo:
    search_state = gr.State([])
    selected_json = gr.Textbox(visible=False)

    with gr.Tabs():
        with gr.Tab("탐색"):
            home_ui = gr.HTML(get_home_html)
            gr.Button("🔄 새로고침").click(get_home_html, None, home_ui)
        with gr.Tab("지도"):
            map_ui = gr.HTML(get_map_html)
            gr.Button("🔄 지도 새로고침").click(get_map_html, None, map_ui)

    fab_btn = gr.Button("+", elem_id="fab-btn")
    overlay = gr.HTML("<div style='position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:10000;display:none;' id='overlay'></div>", visible=False)

    # ⚠️ gr.Box 대신 gr.Column 사용 (오류 방지)
    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal:
        gr.HTML("<div style='text-align:center;padding:15px;font-weight:bold;border-bottom:1px solid #eee;'>새 공간 열기</div>")
        
        with gr.Column(elem_classes=["scroll-body"]):
            act_in = gr.Textbox(label="활동명", placeholder="커피, 산책 등")
            with gr.Row(elem_classes=["fav-grid"]):
                fav_btns = [gr.Button("", visible=False) for _ in range(10)]
            
            img_in = gr.Image(label="현장 사진", type="numpy")
            
            with gr.Row():
                st_in = gr.Textbox(label="시작", value=lambda: now_kst().strftime("%Y-%m-%dT%H:%M"))
                en_in = gr.Textbox(label="종료", value=lambda: (now_kst()+timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"))
            
            unlim = gr.Checkbox(label="인원 제한 없음", value=True)
            cap = gr.Slider(label="인원", minimum=1, maximum=10, value=4, step=1)
            
            # 주소 검색창 (가장 아래에 배치하여 가려짐 방지)
            loc_in = gr.Textbox(label="📍 장소 검색", placeholder="예: 영일대")
            loc_btn = gr.Button("🔍 장소 찾기")
            loc_sel = gr.Radio(label="결과 선택", choices=[], visible=False)
            status = gr.Markdown("")

        with gr.Row(elem_classes=["footer"]):
            gr.Button("취소").click(lambda: [gr.update(visible=False)]*2, None, [overlay, modal])
            save_btn = gr.Button("✅ 공간 열기", variant="primary")

    # 이벤트 로직
    def open_m():
        with db_conn() as con: favs = [r[0] for r in con.execute("SELECT activity FROM favorites ORDER BY created_at DESC LIMIT 10").fetchall()]
        ups = [gr.update(visible=False)] * 10
        for i, f in enumerate(favs): ups[i] = gr.update(visible=True, value=f)
        return [gr.update(visible=True)]*2 + ups

    fab_btn.click(open_m, None, [overlay, modal, *fav_btns])
    for b in fav_btns: b.click(lambda v: v, b, act_in)
    
    def search(q):
        r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}, params={"query": q, "size": 5})
        docs = r.json().get("documents", [])
        cands = [{"label": f"{d['place_name']} ({d['address_name']})", "place": d['place_name'], "lat": d['y'], "lng": d['x']} for d in docs]
        return cands, gr.update(choices=[x['label'] for x in cands], visible=True), f"{len(cands)}개 발견"
    
    loc_btn.click(search, loc_in, [search_state, loc_sel, status])
    loc_sel.change(lambda c, l: next((json.dumps(x, ensure_ascii=False) for x in c if x['label']==l), "{}"), [search_state, loc_sel], selected_json)

    def save(act, st, en, u, c, img, js):
        if not act or not js: return "⚠️ 정보 부족", get_home_html(), get_map_html(), gr.update(visible=True)
        loc = json.loads(js); pic = ""
        if img is not None:
            im = Image.fromarray(img); buf = io.BytesIO(); im.save(buf, format="JPEG", quality=70); pic = base64.b64encode(buf.getvalue()).decode("utf-8")
        with db_conn() as con:
            con.execute("INSERT INTO spaces (id,title,photo_b64,start_iso,end_iso,address,lat,lng,capacity_enabled,capacity_max,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex[:8], act, pic, st, en, loc['place'], float(loc['lat']), float(loc['lng']), 0 if u else 1, c, now_kst().isoformat()))
            con.execute("INSERT OR IGNORE INTO favorites (activity, created_at) VALUES (?,?)", (act, now_kst().isoformat()))
            con.commit()
        return "✅ 완료", get_home_html(), get_map_html(), gr.update(visible=False)

    save_btn.click(save, [act_in, st_in, en_in, unlim, cap, img_in, selected_json], [status, home_ui, map_ui, modal])

# 5. FastAPI 및 지도
app = FastAPI()
@app.get("/kakao_map")
def kakao_map():
    with db_conn() as con: rows = con.execute("SELECT title, lat, lng FROM spaces").fetchall()
    html = f"<html><head><script src='//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}'></script></head><body style='margin:0;'><div id='map' style='width:100%;height:100vh;'></div><script>var map=new kakao.maps.Map(document.getElementById('map'),{{center:new kakao.maps.LatLng(36.019,129.343),level:5}});{json.dumps(rows)}.forEach(r=>new kakao.maps.Marker({{map:map,position:new kakao.maps.LatLng(r[1],r[2])}}));</script></body></html>"
    return HTMLResponse(html)
app = gr.mount_gradio_app(app, demo, path="/")
