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
# 2. 통합 CSS (가시성 확보)
# =====================
INTEGRATED_CSS = """
:root { --brand: #ff6b00; }
.modal-sheet {
    position: fixed !important; left: 50% !important; top: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: min(520px, 95vw) !important; height: 85vh !important;
    background: #fff !important; border-radius: 20px !important;
    z-index: 10001 !important; display: flex !important; flex-direction: column !important;
    box-shadow: 0 20px 50px rgba(0,0,0,0.3) !important;
}
.modal-scroll {
    flex: 1 !important; overflow-y: auto !important; padding: 20px !important;
    display: flex !important; flex-direction: column !important; gap: 15px !important;
}
.fav-grid { display: grid !important; grid-template-columns: 1fr 1fr !important; gap: 8px !important; }
.modal-footer { padding: 15px; border-top: 1px solid #eee; display: flex !important; gap: 10px !important; }
#fab-btn { position: fixed !important; right: 20px !important; bottom: 20px !important; z-index: 2000 !important; }
#fab-btn button { width: 60px !important; height: 60px !important; border-radius: 50% !important; background: var(--brand) !important; color: #fff !important; font-size: 30px !important; }
.card { background: #fff; border: 1px solid #E5E3DD; border-radius: 15px; padding: 15px; margin-bottom: 10px; }
.thumb { width: 80px; height: 80px; border-radius: 10px; object-fit: cover; }
.map-frame { width: 100%; height: 600px; border: none; border-radius: 15px; }
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
        h += f"<div class='card'><div style='display:flex;justify-content:space-between;'><div><b>{r[1]}</b><br><small>{r[4]}</small><br><small style='color:var(--brand);'>{r[3]}</small></div>"
        if img: h += f"<img src='{img}' class='thumb'>"
        h += f"</div><a href='/delete/{r[0]}' style='color:red;font-size:11px;text-decoration:none;'>[삭제]</a></div>"
    return h

def get_map_html():
    return f'<iframe src="/kakao_map?ts={int(now_kst().timestamp())}" class="map-frame"></iframe>'

# =====================
# 4. UI 설계
# =====================
with gr.Blocks(css=INTEGRATED_CSS, title="Oseyo") as demo:
    search_state = gr.State([])
    selected_json = gr.Textbox(visible=False)

    with gr.Tabs():
        with gr.Tab("탐색"):
            home_ui = gr.HTML(get_home_html)
            ref_btn = gr.Button("🔄 리스트 새로고침")
        with gr.Tab("지도"):
            map_ui = gr.HTML(get_map_html)
            map_ref_btn = gr.Button("🔄 지도 새로고침")

    fab_btn = gr.Button("+", elem_id="fab-btn")
    overlay = gr.HTML("<div style='position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:10000;'></div>", visible=False)

    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal:
        gr.HTML("<div style='text-align:center;padding:15px;font-weight:900;'>새 공간 열기</div>")
        with gr.Column(elem_classes=["modal-scroll"]):
            act_in = gr.Textbox(label="활동명", placeholder="예: 커피, 산책")
            with gr.Row(elem_classes=["fav-grid"]):
                fav_btns = [gr.Button("", visible=False) for _ in range(10)]
            img_in = gr.Image(label="현장 사진", type="numpy", height=120)
            st_in = gr.Textbox(label="시작 시간", value=lambda: now_kst().strftime("%Y-%m-%dT%H:%M"))
            en_in = gr.Textbox(label="종료 시간", value=lambda: (now_kst()+timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"))
            unlim = gr.Checkbox(label="인원 제한 없음", value=True)
            cap = gr.Slider(label="인원수", minimum=1, maximum=10, value=4, step=1)
            
            # 주소 검색 (이미지 fcfbb0.png 해결책: 최하단 배치 및 스크롤 보장)
            loc_in = gr.Textbox(label="📍 장소 검색", placeholder="장소명을 입력하세요")
            loc_btn = gr.Button("🔍 장소 찾기", variant="secondary")
            loc_sel = gr.Radio(label="결과 선택", choices=[], visible=False)
            status = gr.Markdown("")

        with gr.Row(elem_classes=["modal-footer"]):
            close_btn = gr.Button("취소")
            save_btn = gr.Button("✅ 공간 열기", variant="primary")

    # 이벤트 설정
    def open_m():
        with db_conn() as con: favs = [r[0] for r in con.execute("SELECT activity FROM favorites ORDER BY created_at DESC LIMIT 10").fetchall()]
        ups = [gr.update(visible=False)] * 10
        for i, f in enumerate(favs): ups[i] = gr.update(visible=True, value=f)
        return [gr.update(visible=True)]*2 + ups

    fab_btn.click(open_m, None, [overlay, modal, *fav_btns])
    close_btn.click(lambda: [gr.update(visible=False)]*2, None, [overlay, modal])
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
        if not act or not js: return "⚠️ 정보가 부족합니다.", get_home_html(), get_map_html(), gr.update(visible=True)
        loc = json.loads(js); pic = ""
        if img is not None:
            im = Image.fromarray(img); buf = io.BytesIO(); im.save(buf, format="JPEG", quality=70); pic = base64.b64encode(buf.getvalue()).decode("utf-8")
        with db_conn() as con:
            con.execute("INSERT INTO spaces (id,title,photo_b64,start_iso,end_iso,address,lat,lng,capacity_enabled,capacity_max,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex[:8], act, pic, st, en, loc['place'], float(loc['lat']), float(loc['lng']), 0 if u else 1, c, now_kst().isoformat()))
            con.execute("INSERT OR IGNORE INTO favorites (activity, created_at) VALUES (?,?)", (act, now_kst().isoformat()))
            con.commit()
        return "✅ 성공!", get_home_html(), get_map_html(), gr.update(visible=False)

    save_btn.click(save, [act_in, st_in, en_in, unlim, cap, img_in, selected_json], [status, home_ui, map_ui, modal])
    ref_btn.click(get_home_html, None, home_ui)
    map_ref_btn.click(get_map_html, None, map_ui)

# =====================
# 5. 서버 실행
# =====================
app = FastAPI()
@app.get("/delete/{sid}")
def del_sp(sid: str):
    with db_conn() as con: con.execute("DELETE FROM spaces WHERE id=?", (sid,)); con.commit()
    return RedirectResponse(url="/", status_code=302)

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
        var m = new kakao.maps.Marker({{map: map, position: new kakao.maps.LatLng(p.lat, p.lng)}});
        var iw = new kakao.maps.InfoWindow({{content: '<div style="padding:5px;font-size:12px;">'+p.title+'</div>'}});
        kakao.maps.event.addListener(m, 'click', function() {{ iw.open(map, m); }});
    }});
    </script></body></html>
    """
    return HTMLResponse(html)

app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
