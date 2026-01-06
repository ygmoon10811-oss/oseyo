# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import requests
from PIL import Image
import gradio as gr
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

# =====================
# 1. 초기 설정 및 DB
# =====================
KST = ZoneInfo("Asia/Seoul")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def now_kst(): return datetime.now(KST)

DATA_DIR = "/var/data" if os.path.isdir("/var/data") else os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "oseyo.db")

def db_conn(): return sqlite3.connect(DB_PATH, check_same_thread=False)

with db_conn() as con:
    con.execute("CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, title TEXT NOT NULL, photo_b64 TEXT DEFAULT '', start_iso TEXT NOT NULL, end_iso TEXT NOT NULL, address TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL, capacity_enabled INTEGER NOT NULL DEFAULT 0, capacity_max INTEGER, created_at TEXT NOT NULL);")
    con.execute("CREATE TABLE IF NOT EXISTS favorites (activity TEXT PRIMARY KEY, created_at TEXT NOT NULL);")
    con.commit()

# =====================
# 2. 강력한 레이아웃 CSS
# =====================
CSS = """
/* 모달 전체 구조 */
.modal-sheet {
    position: fixed !important; left: 50% !important; top: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: min(520px, 95vw) !important; height: 85vh !important;
    background: #fff !important; border-radius: 20px !important;
    z-index: 10001 !important; display: flex !important; flex-direction: column !important;
    box-shadow: 0 20px 50px rgba(0,0,0,0.3) !important; overflow: hidden !important;
}

/* ⚠️ 핵심: 모달 바디 스크롤 및 간격 확보 */
.modal-body {
    flex: 1 !important; overflow-y: auto !important; padding: 25px !important;
    display: flex !important; flex-direction: column !important; gap: 20px !important; /* 항목 간격 20px 확보 */
}

/* 즐겨찾기 2열 그리드 */
.fav-grid { display: grid !important; grid-template-columns: 1fr 1fr !important; gap: 10px !important; }

/* 하단 버튼 영역 고정 */
.modal-footer { padding: 15px; border-top: 1px solid #eee; display: flex !important; gap: 10px !important; background: #fdfdfd; }

/* 지도 및 카드 스타일 */
.map-frame { width: 100%; height: 550px; border: none; border-radius: 15px; }
.card { background: #fff; border: 1px solid #E5E3DD; border-radius: 15px; padding: 15px; margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center; }
.thumb { width: 70px; height: 70px; border-radius: 10px; object-fit: cover; }
#fab-btn { position: fixed !important; right: 20px !important; bottom: 20px !important; z-index: 2000 !important; }
#fab-btn button { width: 60px !important; height: 60px !important; border-radius: 50% !important; background: #ff6b00 !important; color: #fff !important; font-size: 30px !important; }
"""

# =====================
# 3. 로직 함수
# =====================
def get_home_html():
    with db_conn() as con: rows = con.execute("SELECT id, title, photo_b64, start_iso, address FROM spaces ORDER BY created_at DESC").fetchall()
    if not rows: return "<div style='text-align:center;padding:50px;color:#888;'>열린 공간이 없습니다.</div>"
    h = ""
    for r in rows:
        img = f"data:image/jpeg;base64,{r[2]}" if r[2] else ""
        h += f"<div class='card'><div><b>{r[1]}</b><br><small>{r[4]}</small><br><small style='color:#ff6b00;'>{r[3]}</small></div>"
        if img: h += f"<img src='{img}' class='thumb'>"
        h += f"</div>"
    return h

def get_map_html():
    return f'<iframe src="/kakao_map?ts={int(now_kst().timestamp())}" class="map-frame"></iframe>'

# =====================
# 4. Gradio UI
# =====================
with gr.Blocks(css=CSS, title="Oseyo") as demo:
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
    overlay = gr.HTML("<div style='position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:10000;'></div>", visible=False)

    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal:
        gr.HTML("<div style='text-align:center;padding:15px;font-weight:900;border-bottom:1px solid #eee;'>새 공간 열기</div>")
        
        # ⚠️ 모든 입력 항목을 담은 스크롤 바디
        with gr.Column(elem_classes=["modal-body"]):
            act_in = gr.Textbox(label="활동명", placeholder="무엇을 하시나요?")
            
            with gr.Row(elem_classes=["fav-grid"]):
                fav_btns = [gr.Button("", visible=False) for _ in range(10)]
            
            img_in = gr.Image(label="현장 사진", type="numpy", height=150)
            
            with gr.Row():
                st_in = gr.Textbox(label="시작", value=lambda: now_kst().strftime("%Y-%m-%dT%H:%M"))
                en_in = gr.Textbox(label="종료", value=lambda: (now_kst()+timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"))
            
            with gr.Row():
                unlim = gr.Checkbox(label="인원 제한 없음", value=True)
                cap = gr.Slider(label="인원수", minimum=1, maximum=10, value=4, step=1)
            
            # 주소 검색 (스크롤 바디 최하단에 배치하여 절대 가려지지 않게 함)
            loc_in = gr.Textbox(label="📍 어디인가요?", placeholder="장소 검색")
            loc_btn = gr.Button("🔍 장소 찾기")
            loc_sel = gr.Radio(label="장소 선택", choices=[], visible=False)
            status = gr.Markdown("")

        with gr.Row(elem_classes=["modal-footer"]):
            gr.Button("취소").click(lambda: [gr.update(visible=False)]*2, None, [overlay, modal])
            save_btn = gr.Button("✅ 공간 열기", variant="primary")

    # 이벤트 연결
    def open_m():
        with db_conn() as con: favs = [r[0] for r in con.execute("SELECT activity FROM favorites ORDER BY created_at DESC LIMIT 10").fetchall()]
        ups = [gr.update(visible=False, value="")] * 10
        for i, f in enumerate(favs): ups[i] = gr.update(visible=True, value=f)
        return [gr.update(visible=True)]*2 + ups

    fab_btn.click(open_m, None, [overlay, modal, *fav_btns])
    for b in fav_btns: b.click(lambda v: v, b, act_in)
    
    def search(q):
        try:
            r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}, params={"query": q, "size": 8})
            data = r.json().get("documents", [])
            cands = [{"label": f"{d['place_name']} ({d['address_name']})", "place": d['place_name'], "lat": d['y'], "lng": d['x']} for d in data]
            return cands, gr.update(choices=[x['label'] for x in cands], visible=True), f"{len(cands)}개 결과"
        except: return [], gr.update(visible=False), "검색 실패"

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

# =====================
# 5. FastAPI & 지도 서버
# =====================
app = FastAPI()
@app.get("/kakao_map")
def kakao_map():
    with db_conn() as con: rows = con.execute("SELECT title, lat, lng, address FROM spaces").fetchall()
    pts = [{"title": r[0], "lat": r[1], "lng": r[2], "addr": r[3]} for r in rows]
    center = pts[0] if pts else {"lat": 36.019, "lng": 129.343}
    html = f"""
    <html><head><meta name="viewport" content="width=device-width, initial-scale=1.0"><script src="//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}"></script></head>
    <body style="margin:0;"><div id="map" style="width:100%;height:100vh;"></div><script>
    var map = new kakao.maps.Map(document.getElementById('map'), {{center: new kakao.maps.LatLng({center['lat']}, {center['lng']}), level: 5}});
    {json.dumps(pts)}.forEach(function(p) {{
        new kakao.maps.Marker({{map: map, position: new kakao.maps.LatLng(p.lat, p.lng)}});
    }});
    </script></body></html>
    """
    return HTMLResponse(html)

app = gr.mount_gradio_app(app, demo, path="/")
