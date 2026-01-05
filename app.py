import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image
import gradio as gr

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, HTMLResponse

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
        con.execute("DROP TABLE IF EXISTS spaces")
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
            space["id"],
            space["title"],
            space.get("photo_b64",""),
            space["start_iso"],
            space["end_iso"],
            space["address"],
            space.get("address_detail",""),
            float(space["lat"]),
            float(space["lng"]),
            1 if space.get("capacityEnabled") else 0,
            space.get("capacityMax"),
            0,
            now_kst().isoformat(),
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
            FROM spaces
            ORDER BY created_at DESC
        """).fetchall()

    out=[]
    for r in rows:
        out.append({
            "id": r[0],
            "title": r[1],
            "photo_b64": r[2] or "",
            "start_iso": r[3] or "",
            "end_iso": r[4] or "",
            "address": r[5] or "",
            "address_detail": r[6] or "",
            "lat": float(r[7]) if r[7] is not None else None,
            "lng": float(r[8]) if r[8] is not None else None,
            "capacityEnabled": bool(r[9]),
            "capacityMax": r[10],
            "hidden": bool(r[11]),
            "created_at": r[12] or "",
        })
    return out

def active_spaces():
    spaces = db_list_spaces()
    t = now_kst()
    out=[]
    for s in spaces:
        if s.get("hidden"):
            continue
        try:
            st = datetime.fromisoformat(s["start_iso"])
            en = datetime.fromisoformat(s["end_iso"])
            if st.tzinfo is None: st = st.replace(tzinfo=KST)
            if en.tzinfo is None: en = en.replace(tzinfo=KST)
            if st <= t <= en:
                out.append(s)
        except:
            pass
    return out

def image_np_to_b64(img_np):
    if img_np is None:
        return ""
    try:
        im = Image.fromarray(img_np.astype("uint8"))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except:
        return ""

def b64_to_data_uri(b64_str):
    return f"data:image/jpeg;base64,{b64_str}" if b64_str else ""

def kakao_keyword_search(q: str, size=10):
    q = (q or "").strip()
    if not q:
        return [], "주소를 입력해 주세요"
    if not KAKAO_REST_API_KEY:
        return [], "⚠️ KAKAO_REST_API_KEY 환경변수가 필요합니다"

    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params = {"query": q, "size": size}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code == 429:
            return [], "⚠️ 검색 제한. 잠시 후 다시 시도해 주세요"
        if r.status_code >= 400:
            return [], f"⚠️ 검색 실패 (HTTP {r.status_code})"
        data = r.json()
    except Exception as e:
        return [], f"⚠️ 네트워크 오류: {str(e)}"

    cands=[]
    for d in (data.get("documents") or []):
        place = (d.get("place_name") or "").strip()
        road = (d.get("road_address_name") or "").strip()
        addr = (d.get("address_name") or "").strip()
        lat = d.get("y")
        lng = d.get("x")
        if not place or lat is None or lng is None:
            continue
        best_addr = road or addr
        label = f"{place} — {best_addr}" if best_addr else place
        try:
            cands.append({
                "label": label,
                "place": place,
                "lat": float(lat),
                "lng": float(lng)
            })
        except:
            pass

    if not cands:
        return [], "⚠️ 검색 결과가 없습니다"
    return cands, ""

def fmt_period(st_iso: str, en_iso: str) -> str:
    try:
        st = datetime.fromisoformat(st_iso)
        en = datetime.fromisoformat(en_iso)
        if st.tzinfo is None: st = st.replace(tzinfo=KST)
        if en.tzinfo is None: en = en.replace(tzinfo=KST)
        if st.date() == en.date():
            return f"{st:%m/%d} {st:%H:%M}–{en:%H:%M}"
        return f"{st:%m/%d %H:%M}–{en:%m/%d %H:%M}"
    except:
        return "-"

def render_home():
    items = active_spaces()
    
    persistent = os.path.isdir("/var/data")
    banner = (
        f"<div class='banner ok'>✅ 영구저장 모드 (DB: {DB_PATH})</div>"
        if persistent else
        f"<div class='banner warn'>⚠️ 임시저장 모드 (DB: {DB_PATH})</div>"
    )

    if not items:
        return banner + """
        <div class="card empty">
          <div class="h">아직 열린 공간이 없습니다</div>
          <div class="p">오른쪽 아래 + 버튼으로 공간을 열어보세요</div>
        </div>
        """

    out = [banner]
    for s in items:
        period = fmt_period(s["start_iso"], s["end_iso"])
        cap = f"최대 {s['capacityMax']}명" if s.get("capacityEnabled") else "제한 없음"
        detail = (s.get("address_detail") or "").strip()
        detail_line = f"<div class='muted'>상세: {detail}</div>" if detail else ""
        photo_uri = b64_to_data_uri(s.get("photo_b64", ""))
        img = f"<img class='thumb' src='{photo_uri}' />" if photo_uri else "<div class='thumb placeholder'></div>"

        out.append(f"""
        <div class="card">
          <div class="rowcard">
            <div class="left">
              <div class="title">{s['title']}</div>
              <div class="period">{period}</div>
              <div class='muted'>{s['address']}</div>
              {detail_line}
              <div class='muted'>{cap}</div>
              <div class="idline">ID: {s['id']}</div>
            </div>
            <div class="right">{img}</div>
          </div>
          <a class="btn-del" href="/delete/{s['id']}">삭제</a>
        </div>
        """)

    return "\n".join(out)

def map_points_payload():
    items = active_spaces()
    points = []
    for s in items:
        points.append({
            "title": s["title"],
            "lat": s["lat"],
            "lng": s["lng"],
            "addr": s.get("address",""),
            "detail": s.get("address_detail",""),
            "period": fmt_period(s.get("start_iso",""), s.get("end_iso","")),
            "id": s["id"],
        })
    return points

def draw_map():
    ts = int(now_kst().timestamp())
    return f"""
    <div class="mapWrap">
      <iframe class="mapFrame" src="/kakao_map?ts={ts}" loading="lazy"></iframe>
    </div>
    """

def open_modal():
    return (
        gr.update(visible=True),
        gr.update(visible=True),
        now_kst().strftime("%Y-%m-%d"),
        now_kst().strftime("%H:%M"),
        ""
    )

def close_modal():
    return (
        gr.update(visible=False),
        gr.update(visible=False),
    )

def create_event(activity_text, date_str, start_time_str, duration_mins,
                 capacity_unlimited, cap_max, photo_np, selected_place_json):
    
    print(f"[CREATE] activity={activity_text}, date={date_str}, time={start_time_str}, place={selected_place_json}")
    
    act = (activity_text or "").strip()
    if not act:
        return "⚠️ 활동명을 입력해 주세요", render_home(), draw_map()
    
    try:
        place_data = json.loads(selected_place_json) if selected_place_json else None
    except:
        place_data = None
    
    if not place_data:
        return "⚠️ 장소를 검색하고 선택해 주세요", render_home(), draw_map()
    
    try:
        date_part = (date_str or "").strip()
        time_part = (start_time_str or "").strip()
        
        if not date_part or not time_part:
            return "⚠️ 날짜와 시간을 모두 입력해 주세요", render_home(), draw_map()
        
        dt_str = f"{date_part} {time_part}"
        st = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
        st = st.replace(tzinfo=KST)
        
    except Exception as e:
        print(f"[ERROR] 시간 파싱 실패: {e}")
        return f"⚠️ 날짜/시간 형식이 잘못되었습니다", render_home(), draw_map()
    
    try:
        duration = int(duration_mins)
    except:
        duration = 30
    
    en = st + timedelta(minutes=duration)
    
    capacityEnabled = not bool(capacity_unlimited)
    cap_max_val = None
    if capacityEnabled:
        try:
            cap_max_val = int(cap_max)
            cap_max_val = max(1, min(cap_max_val, 10))
        except:
            cap_max_val = 4
    
    photo_b64 = image_np_to_b64(photo_np)
    title = act if len(act) <= 30 else act[:30] + "…"
    new_id = uuid.uuid4().hex[:8]
    
    try:
        db_insert_space({
            "id": new_id,
            "title": title,
            "photo_b64": photo_b64,
            "start_iso": st.isoformat(),
            "end_iso": en.isoformat(),
            "address": place_data.get("place", ""),
            "address_detail": "",
            "lat": float(place_data["lat"]),
            "lng": float(place_data["lng"]),
            "capacityEnabled": capacityEnabled,
            "capacityMax": cap_max_val,
        })
        print(f"[SUCCESS] 이벤트 생성: {new_id}")
        return f"✅ '{title}' 이벤트가 생성되었습니다!", render_home(), draw_map()
    except Exception as e:
        print(f"[ERROR] DB 저장 실패: {e}")
        return f"⚠️ 저장 실패: {str(e)}", render_home(), draw_map()

CSS = """
:root{--bg:#FAF9F6;--ink:#1F2937;--muted:#6B7280;--line:#E5E3DD;--card:#ffffffcc;--danger:#ef4444;}
*{box-sizing:border-box!important;}
html,body{width:100%;overflow-x:hidden!important;background:var(--bg)!important;}
.gradio-container{background:var(--bg)!important;max-width:1200px!important;margin:0 auto!important;}

.banner{margin:10px auto 6px;padding:10px 12px;border-radius:14px;font-size:13px;}
.banner.ok{background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46;}
.banner.warn{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;}

.card{position:relative;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px;margin:12px 0;}
.card.empty{text-align:center;padding:40px;}
.h{font-size:18px;font-weight:900;margin-bottom:8px;}
.p{font-size:14px;color:var(--muted);}
.rowcard{display:grid;grid-template-columns:1fr 320px;gap:18px;padding-right:86px;}
.title{font-size:16px;font-weight:900;color:var(--ink);margin-bottom:6px;}
.period{font-size:14px;font-weight:900;color:#111827;margin:2px 0 8px;}
.muted{font-size:13px;color:var(--muted);line-height:1.55;margin:2px 0;}
.idline{margin-top:8px;font-size:12px;color:#9CA3AF;}
.thumb{width:100%;height:180px;object-fit:cover;border-radius:14px;}
.thumb.placeholder{background:rgba(0,0,0,0.05);}
.btn-del{position:absolute;right:14px;bottom:14px;background:var(--danger);color:#fff!important;font-weight:900;font-size:13px;padding:10px 14px;border-radius:12px;text-decoration:none;}

.mapWrap{width:100%;margin:0;padding:0;}
.mapFrame{width:100%;height:600px;border:0;border-radius:18px;}

.fab-container{position:fixed!important;right:20px!important;bottom:20px!important;z-index:9999!important;height:0!important;overflow:visible!important;}
.fab-container button{width:50px!important;height:50px!important;min-width:50px!important;min-height:50px!important;border-radius:50%!important;border:0!important;background:#2B2A27!important;color:#FAF9F6!important;font-size:24px!important;font-weight:300!important;line-height:50px!important;padding:0!important;box-shadow:0 4px 12px rgba(0,0,0,0.15)!important;cursor:pointer!important;transition:all 0.2s ease!important;}
.fab-container button:hover{transform:scale(1.1)!important;box-shadow:0 6px 16px rgba(0,0,0,0.25)!important;}

.modal-overlay{position:fixed!important;inset:0!important;background:rgba(0,0,0,0.4)!important;z-index:10000!important;backdrop-filter:blur(4px)!important;}

.modal-sheet{position:fixed!important;left:50%!important;top:50%!important;transform:translate(-50%,-50%)!important;width:min(540px,90vw)!important;max-height:82vh!important;overflow-y:auto!important;overflow-x:hidden!important;background:var(--bg)!important;border:1px solid var(--line)!important;border-radius:18px!important;padding:18px!important;z-index:10001!important;box-shadow:0 16px 32px rgba(0,0,0,0.12)!important;}
.modal-sheet::-webkit-scrollbar{width:6px;}
.modal-sheet::-webkit-scrollbar-track{background:transparent;}
.modal-sheet::-webkit-scrollbar-thumb{background:#D1D5DB;border-radius:3px;}
.modal-sheet::-webkit-scrollbar-thumb:hover{background:#9CA3AF;}

.modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--line);}
.modal-title{font-size:18px;font-weight:900;color:var(--ink);}

.modal-sheet label{font-size:13px!important;font-weight:600!important;margin-bottom:4px!important;}
.modal-sheet input[type="text"],.modal-sheet textarea,.modal-sheet select{font-size:14px!important;padding:8px 10px!important;border-radius:8px!important;}
.modal-sheet .gr-form{gap:10px!important;}

.modal-footer{display:flex;gap:8px;margin-top:16px;padding-top:14px;border-top:1px solid var(--line);position:sticky;bottom:0;background:var(--bg);}
.modal-footer button{flex:1;padding:10px!important;border-radius:10px!important;font-weight:700!important;font-size:13px!important;}

@media (max-width:768px){
  .rowcard{grid-template-columns:1fr;padding-right:14px;}
  .thumb{height:200px;}
  .modal-sheet{width:94vw!important;max-height:86vh!important;padding:14px!important;}
  .modal-title{font-size:16px!important;}
  .fab-container{right:16px!important;bottom:16px!important;}
  .fab-container button{width:48px!important;height:48px!important;min-width:48px!important;min-height:48px!important;font-size:22px!important;}
}
"""

with gr.Blocks(css=CSS, title="Oseyo") as demo:
    search_results_state = gr.State([])
    selected_place_state = gr.Textbox(visible=False, value="{}")
    
    gr.Markdown("# 지금, 열려 있습니다\n원하시면 오세요")
    
    with gr.Tabs():
        with gr.Tab("탐색"):
            home_html = gr.HTML()
            refresh_btn = gr.Button("🔄 새로고침", size="sm")
        
        with gr.Tab("지도"):
            map_html = gr.HTML()
            map_refresh_btn = gr.Button("🔄 지도 새로고침", size="sm")
    
    with gr.Row(elem_classes=["fab-container"]):
        fab_btn = gr.Button("+", elem_id="fab")
    
    modal_overlay = gr.HTML("<div></div>", visible=False, elem_classes=["modal-overlay"])
    
    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal_sheet:
        with gr.Row(elem_classes=["modal-header"]):
            gr.HTML("<div class='modal-title'>새 공간 열기</div>")
            close_btn = gr.Button("✕", size="sm")
        
        activity_text = gr.Textbox(label="📝 활동명", placeholder="예: 산책, 커피, 스터디…")
        photo_np = gr.Image(label="📸 사진", type="numpy", height=160)
        
        with gr.Row():
            date_input = gr.Textbox(label="📅 날짜", placeholder="YYYY-MM-DD", value=now_kst().strftime("%Y-%m-%d"), scale=1)
            start_time_input = gr.Textbox(label="⏰ 시간", placeholder="HH:MM", value=now_kst().strftime("%H:%M"), scale=1)
        
        duration_input = gr.Dropdown(label="⏱️ 지속시간", choices=[15,30,45,60,90,120], value=30)
        
        with gr.Row():
            capacity_unlimited = gr.Checkbox(label="👥 제한없음", value=True, scale=1)
            cap_max = gr.Slider(label="최대인원", minimum=1, maximum=10, value=4, step=1, scale=2)
        
        with gr.Row():
            place_query = gr.Textbox(label="📍 장소", placeholder="예: 포항시청, 영일대", scale=4)
            search_btn = gr.Button("🔍", scale=1, size="sm")
        
        search_msg = gr.Markdown("")
        place_dropdown = gr.Dropdown(label="검색결과", choices=[], value=None)
        msg_output = gr.Markdown("")
        
        with gr.Row(elem_classes=["modal-footer"]):
            cancel_btn = gr.Button("취소", variant="secondary")
            create_btn = gr.Button("✅ 생성", variant="primary")
    
    demo.load(fn=render_home, outputs=home_html)
    demo.load(fn=draw_map, outputs=map_html)
    
    refresh_btn.click(fn=render_home, outputs=home_html)
    map_refresh_btn.click(fn=draw_map, outputs=map_html)
    
    fab_btn.click(fn=open_modal, outputs=[modal_overlay, modal_sheet, date_input, start_time_input, msg_output])
    close_btn.click(fn=close_modal, outputs=[modal_overlay, modal_sheet])
    cancel_btn.click(fn=close_modal, outputs=[modal_overlay, modal_sheet])
    
    def search_and_store(query):
        cands, err = kakao_keyword_search(query, size=10)
        if err:
            return cands, gr.update(choices=[], value=None), err, "{}"
        labels = [c["label"] for c in cands]
        first_json = json.dumps(cands[0]) if cands else "{}"
        return (cands, gr.update(choices=labels, value=labels[0] if labels else None), f"✅ {len(cands)}개", first_json)
    
    search_btn.click(fn=search_and_store, inputs=[place_query], outputs=[search_results_state, place_dropdown, search_msg, selected_place_state])
    
    def update_selected(cands, label):
        if not label or not cands:
            return "{}"
        for c in cands:
            if c["label"] == label:
                return json.dumps(c)
        return "{}"
    
    place_dropdown.change(fn=update_selected, inputs=[search_results_state, place_dropdown], outputs=[selected_place_state])
    
    def create_and_close(activity_text, date_str, start_time_str, duration_mins, capacity_unlimited, cap_max, photo_np, selected_place_json):
        msg, home, mapv = create_event(activity_text, date_str, start_time_str, duration_mins, capacity_unlimited, cap_max, photo_np, selected_place_json)
        if msg.startswith("✅"):
            return (msg, home, mapv, gr.update(visible=False), gr.update(visible=False))
        else:
            return (msg, home, mapv, gr.update(visible=True), gr.update(visible=True))
    
    create_btn.click(fn=create_and_close, inputs=[activity_text, date_input, start_time_input, duration_input, capacity_unlimited, cap_max, photo_np, selected_place_state], outputs=[msg_output, home_html, map_html, modal_overlay, modal_sheet])

app = FastAPI()

@app.get("/")
def root():
    return RedirectResponse(url="/app", status_code=302)

@app.get("/delete/{space_id}")
def delete(space_id: str):
    try:
        db_delete_space(space_id)
    except:
        pass
    return RedirectResponse(url="/app", status_code=302)

@app.get("/kakao_map")
def kakao_map():
    if not KAKAO_JAVASCRIPT_KEY:
        return HTMLResponse("<html><body><h3>KAKAO_JAVASCRIPT_KEY 필요</h3></body></html>")
    
    points = map_points_payload()
    if points:
        center_lat = sum(p["lat"] for p in points) / len(points)
        center_lng = sum(p["lng"] for p in points) / len(points)
    else:
        center_lat, center_lng = 36.0190, 129.3435
    
    html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
html,body{{margin:0;height:100%;}}
#map{{width:100%;height:100%;}}
.custom-info{{padding:10px;font-family:system-ui;font-size:12px;line-height:1.4;min-width:180px;}}
.info-title{{font-weight:900;margin-bottom:4px;font-size:13px;}}
.info-text{{color:#6B7280;margin:2px 0;font-size:11px;}}
</style>
<script src="//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}"></script>
</head>
<body>
<div id="map"></div>
<script>
const map=new kakao.maps.Map(document.getElementById('map'),{{center:new kakao.maps.LatLng({center_lat},{center_lng}),level:6}});
const points={json.dumps(points,ensure_ascii=False)};
if(points.length===0){{new kakao.maps.Marker({{position:new kakao.maps.LatLng({center_lat},{center_lng})}}).setMap(map);}}else{{
const bounds=new kakao.maps.LatLngBounds();
points.forEach(p=>{{
const pos=new kakao.maps.LatLng(p.lat,p.lng);
bounds.extend(pos);
const marker=new kakao.maps.Marker({{position:pos,map:map}});
const content=`<div class="custom-info"><div class="info-title">${{p.title}}</div><div class="info-text">${{p.period}}</div><div class="info-text">${{p.addr}}</div>${{p.detail?`<div class="info-text">상세:${{p.detail}}</div>`:''}}
<div class="info-text" style="margin-top:4px;color:#9CA3AF;">ID:${{p.id}}</div></div>`;
const infowindow=new kakao.maps.InfoWindow({{content:content}});
kakao.maps.event.addListener(marker,'click',function(){{infowindow.open(map,marker);}});
}});
map.setBounds(bounds);
}}
</script>
</body>
</html>
"""
    return HTMLResponse(html)

app = gr.mount_gradio_app(app, demo, path="/app")
