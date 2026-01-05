import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image
import gradio as gr

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, HTMLResponse

# -------------------------
# 설정 및 환경 변수
# -------------------------
KST = ZoneInfo("Asia/Seoul")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def now_kst():
    return datetime.now(KST)

# -------------------------
# DB 설정
# -------------------------
def get_data_dir():
    return "/var/data" if os.path.isdir("/var/data") else os.path.join(os.getcwd(), "data")

DATA_DIR = get_data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "oseyo.db")

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def db_init():
    with db_conn() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS spaces (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            photo_b64 TEXT DEFAULT '',
            start_iso TEXT NOT NULL,
            end_iso TEXT NOT NULL,
            address TEXT NOT NULL,
            address_detail TEXT DEFAULT '',
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            capacity_enabled INTEGER NOT NULL DEFAULT 0,
            capacity_max INTEGER,
            hidden INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """)
        con.commit()

db_init()

def db_insert_space(space: dict):
    with db_conn() as con:
        con.execute("""
        INSERT INTO spaces (
            id, title, photo_b64, start_iso, end_iso,
            address, address_detail, lat, lng,
            capacity_enabled, capacity_max, hidden, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            space["id"], space["title"], space.get("photo_b64",""),
            space["start_iso"], space["end_iso"], space["address"],
            space.get("address_detail",""), float(space["lat"]), float(space["lng"]),
            1 if space.get("capacityEnabled") else 0, space.get("capacityMax"),
            0, now_kst().isoformat(),
        ))
        con.commit()

def db_delete_space(space_id: str):
    with db_conn() as con:
        con.execute("DELETE FROM spaces WHERE id=?", (space_id,))
        con.commit()

def db_list_spaces():
    with db_conn() as con:
        rows = con.execute("""
            SELECT id, title, photo_b64, start_iso, end_iso,
                   address, address_detail, lat, lng,
                   capacity_enabled, capacity_max, hidden, created_at
            FROM spaces ORDER BY created_at DESC
        """).fetchall()
    
    out=[]
    for r in rows:
        out.append({
            "id": r[0], "title": r[1], "photo_b64": r[2] or "",
            "start_iso": r[3] or "", "end_iso": r[4] or "",
            "address": r[5] or "", "address_detail": r[6] or "",
            "lat": float(r[7]) if r[7] is not None else None,
            "lng": float(r[8]) if r[8] is not None else None,
            "capacityEnabled": bool(r[9]), "capacityMax": r[10],
            "hidden": bool(r[11]), "created_at": r[12] or "",
        })
    return out

def active_spaces():
    spaces = db_list_spaces()
    t = now_kst()
    out=[]
    for s in spaces:
        if s.get("hidden"): continue
        try:
            st = datetime.fromisoformat(s["start_iso"]).replace(tzinfo=KST)
            en = datetime.fromisoformat(s["end_iso"]).replace(tzinfo=KST)
            if st <= t <= en: out.append(s)
        except: pass
    return out

# -------------------------
# 헬퍼 함수
# -------------------------
def image_np_to_b64(img_np):
    if img_np is None: return ""
    try:
        im = Image.fromarray(img_np.astype("uint8"))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except: return ""

def b64_to_data_uri(b64_str):
    return f"data:image/jpeg;base64,{b64_str}" if b64_str else ""

def kakao_keyword_search(q: str, size=10):
    q = (q or "").strip()
    if not q or not KAKAO_REST_API_KEY: return [], "검색어가 없거나 API 키가 설정되지 않았습니다."
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    try:
        r = requests.get(url, headers=headers, params={"query": q, "size": size}, timeout=10)
        data = r.json()
        cands = []
        for d in data.get("documents", []):
            cands.append({
                "label": f"{d['place_name']} ({d.get('road_address_name') or d.get('address_name')})",
                "place": d['place_name'], "lat": float(d['y']), "lng": float(d['x'])
            })
        return cands, ""
    except Exception as e: return [], str(e)

def fmt_period(st_iso: str, en_iso: str) -> str:
    try:
        st = datetime.fromisoformat(st_iso)
        en = datetime.fromisoformat(en_iso)
        return f"{st:%y/%m/%d %H:%M} ~ {en:%m/%d %H:%M}"
    except: return "-"

# -------------------------
# UI 렌더링 및 로직
# -------------------------
def render_home():
    items = active_spaces()
    if not items:
        return "<div class='card empty'><div class='h'>현재 열린 공간이 없습니다</div><div class='p'>하단의 + 버튼으로 첫 공간을 열어보세요!</div></div>"
    
    out = []
    for s in items:
        photo_uri = b64_to_data_uri(s.get("photo_b64", ""))
        img_tag = f"<img class='thumb' src='{photo_uri}' />" if photo_uri else "<div class='thumb placeholder'></div>"
        period = fmt_period(s["start_iso"], s["end_iso"])
        
        out.append(f"""
        <div class="card">
          <div class="rowcard">
            <div class="left">
              <div class="title">{s['title']}</div>
              <div class="period">🕒 {period}</div>
              <div class="muted">📍 {s['address']}</div>
              <div class="idline">ID: {s['id']} | {'제한 없음' if not s['capacityEnabled'] else f'최대 {s["capacityMax"]}명'}</div>
            </div>
            <div class="right">{img_tag}</div>
          </div>
          <a class="btn-del" href="/delete/{s['id']}">공간 닫기</a>
        </div>
        """)
    return "\n".join(out)

def draw_map():
    ts = int(now_kst().timestamp())
    return f"<iframe class='mapFrame' src='/kakao_map?ts={ts}' loading='lazy'></iframe>"

def create_event_refined(title, s_date, s_time, e_date, e_time, unlimit, c_max, photo_np, place_json):
    if not title.strip(): return "⚠️ 활동명을 입력해주세요.", render_home(), draw_map()
    if not place_json or place_json == "{}": return "⚠️ 장소를 선택해주세요.", render_home(), draw_map()
    
    try:
        st = datetime.strptime(f"{s_date} {s_time}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        en = datetime.strptime(f"{e_date} {e_time}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        if en <= st: return "⚠️ 종료 시간이 시작보다 빠를 수 없습니다.", render_home(), draw_map()
        
        place_data = json.loads(place_json)
        db_insert_space({
            "id": uuid.uuid4().hex[:8],
            "title": title[:30],
            "photo_b64": image_np_to_b64(photo_np),
            "start_iso": st.isoformat(),
            "end_iso": en.isoformat(),
            "address": place_data["place"],
            "lat": place_data["lat"],
            "lng": place_data["lng"],
            "capacityEnabled": not unlimit,
            "capacityMax": int(c_max)
        })
        return "✅ 새로운 공간이 열렸습니다!", render_home(), draw_map()
    except Exception as e:
        return f"⚠️ 오류: {str(e)}", render_home(), draw_map()

# -------------------------
# 스타일 시트 (CSS)
# -------------------------
CSS = """
:root{--bg:#FAF9F6;--ink:#1F2937;--muted:#6B7280;--line:#E5E3DD;--primary:#2B2A27;--danger:#ef4444;}
body{background:var(--bg)!important; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;}

/* FAB 버튼 스타일 최적화 */
.fab-container{position:fixed!important;right:20px!important;bottom:25px!important;z-index:999!important;}
.fab-container button{
    width:56px!important;height:56px!important;min-width:56px!important;
    border-radius:28px!important;background:var(--primary)!important;
    color:white!important;font-size:30px!important;font-weight:300!important;
    box-shadow:0 4px 12px rgba(0,0,0,0.2)!important;border:none!important;
}

/* 모달 및 컨테이너 가독성 */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;backdrop-filter:blur(4px);}
.modal-sheet{
    position:fixed!important;left:50%!important;top:50%!important;transform:translate(-50%,-50%)!important;
    width:min(500px, 92vw)!important;max-height:85vh!important;overflow-y:auto!important;
    background:white!important;border-radius:24px!important;padding:20px!important;
    z-index:1001!important;box-shadow:0 12px 30px rgba(0,0,0,0.2)!important;
}

.card{background:white;border:1px solid var(--line);border-radius:20px;padding:16px;margin-bottom:12px;position:relative;}
.rowcard{display:flex;gap:12px;}
.left{flex:1;}
.title{font-size:18px;font-weight:800;color:var(--ink);margin-bottom:4px;}
.period{font-size:14px;font-weight:600;color:#059669;margin-bottom:4px;}
.muted{font-size:13px;color:var(--muted);margin-bottom:2px;}
.thumb{width:80px;height:80px;object-fit:cover;border-radius:12px;}
.btn-del{display:inline-block;margin-top:10px;font-size:12px;color:var(--danger);text-decoration:none;font-weight:600;}

.mapFrame{width:100%;height:500px;border:0;border-radius:20px;}

/* 입력 필드 간격 조정 */
.modal-sheet .gr-form { gap: 12px !important; }
.modal-sheet label { font-size: 13px !important; font-weight: 700 !important; margin-bottom: 4px !important; }
"""

# -------------------------
# Gradio 앱 구성
# -------------------------
with gr.Blocks(css=CSS, title="오세요 - 지금 열린 공간") as demo:
    search_results_state = gr.State([])
    selected_place_state = gr.Textbox(visible=False, value="{}")

    gr.Markdown("# 📍 지금, 열려 있습니다\n가까운 곳에서 진행 중인 활동을 확인해보세요.")

    with gr.Tabs():
        with gr.Tab("목록 보기"):
            home_html = gr.HTML()
            gr.Button("🔄 리스트 새로고침").click(render_home, outputs=home_html)
        with gr.Tab("지도 보기"):
            map_html = gr.HTML()
            gr.Button("🔄 지도 새로고침").click(draw_map, outputs=map_html)

    # FAB 버튼
    with gr.Row(elem_classes=["fab-container"]):
        fab_btn = gr.Button("+")

    # 모달 레이어
    modal_overlay = gr.HTML("<div class='modal-overlay'></div>", visible=False)
    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal_sheet:
        gr.Markdown("### 🏠 새로운 공간 열기")
        
        act_input = gr.Textbox(label="무엇을 하나요?", placeholder="예: 커피 한 잔, 가벼운 산책")
        img_input = gr.Image(label="사진 첨부 (선택)", type="numpy", height=140)
        
        with gr.Row():
            sd = gr.Textbox(label="시작 날짜", value=lambda: now_kst().strftime("%Y-%m-%d"), scale=2)
            st = gr.Textbox(label="시작 시간", value=lambda: now_kst().strftime("%H:%M"), scale=1)
        
        with gr.Row():
            ed = gr.Textbox(label="종료 날짜", value=lambda: now_kst().strftime("%Y-%m-%d"), scale=2)
            et = gr.Textbox(label="종료 시간", value=lambda: (now_kst()+timedelta(hours=1)).strftime("%H:%M"), scale=1)
            
        with gr.Row():
            unlimit = gr.Checkbox(label="인원 제한 없음", value=True)
            c_max = gr.Slider(label="최대 인원", minimum=1, maximum=10, value=4, step=1)
            
        with gr.Row():
            p_query = gr.Textbox(label="어디서 하나요?", placeholder="장소 검색", scale=3)
            s_btn = gr.Button("🔍", scale=1)
            
        p_drop = gr.Dropdown(label="검색 결과에서 선택", choices=[])
        msg_out = gr.Markdown("")
        
        with gr.Row():
            close_btn = gr.Button("취소")
            save_btn = gr.Button("공간 열기", variant="primary")

    # 이벤트 바인딩
    def open_m(): return gr.update(visible=True), gr.update(visible=True)
    def close_m(): return gr.update(visible=False), gr.update(visible=False)

    fab_btn.click(open_m, outputs=[modal_overlay, modal_sheet])
    close_btn.click(close_m, outputs=[modal_overlay, modal_sheet])

    def search_proc(q):
        cands, err = kakao_keyword_search(q)
        if err: return gr.update(choices=[]), f"⚠️ {err}", []
        labels = [c["label"] for c in cands]
        return gr.update(choices=labels, value=labels[0] if labels else None), "✅ 장소를 선택하세요", cands

    s_btn.click(search_proc, inputs=p_query, outputs=[p_drop, msg_out, search_results_state])

    def select_proc(cands, label):
        for c in cands:
            if c["label"] == label: return json.dumps(c)
        return "{}"
    p_drop.change(select_proc, inputs=[search_results_state, p_drop], outputs=selected_place_state)

    def save_proc(*args):
        res = create_event_refined(*args)
        if res[0].startswith("✅"):
            return res[0], res[1], res[2], gr.update(visible=False), gr.update(visible=False)
        return res[0], res[1], res[2], gr.update(visible=True), gr.update(visible=True)

    save_btn.click(save_proc, 
                   inputs=[act_input, sd, st, ed, et, unlimit, c_max, img_input, selected_place_state],
                   outputs=[msg_out, home_html, map_html, modal_overlay, modal_sheet])

    demo.load(render_home, outputs=home_html)
    demo.load(draw_map, outputs=map_html)

# -------------------------
# FastAPI 서버 설정
# -------------------------
app = FastAPI()

@app.get("/")
def root(): return RedirectResponse(url="/app")

@app.get("/delete/{space_id}")
def delete(space_id: str):
    db_delete_space(space_id)
    return RedirectResponse(url="/app")

@app.get("/kakao_map")
def kakao_map():
    points = [{ "title": s["title"], "lat": s["lat"], "lng": s["lng"], "addr": s["address"], "period": fmt_period(s["start_iso"], s["end_iso"]), "id": s["id"] } for s in active_spaces()]
    center = [sum(p["lat"] for p in points)/len(points), sum(p["lng"] for p in points)/len(points)] if points else [36.019, 129.343]
    
    html = f"""
    <!doctype html><html><head><meta charset="utf-8"/><script src="//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}"></script>
    <style>html,body,#map{{width:100%;height:100%;margin:0;}} .info{{padding:10px;font-size:12px;}}</style></head>
    <body><div id="map"></div><script>
    const map=new kakao.maps.Map(document.getElementById('map'),{{center:new kakao.maps.LatLng({center[0]},{center[1]}),level:5}});
    const pts={json.dumps(points)};
    pts.forEach(p=>{{
        const marker=new kakao.maps.Marker({{position:new kakao.maps.LatLng(p.lat,p.lng),map:map}});
        const iw=new kakao.maps.InfoWindow({{content:`<div class="info"><b>${{p.title}}</b><br/>${{p.period}}<br/>${{p.addr}}</div>`}});
        kakao.maps.event.addListener(marker,'click',()=>iw.open(map,marker));
    }});
    </script></body></html>
    """
    return HTMLResponse(html)

app = gr.mount_gradio_app(app, demo, path="/app")
