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
# 기본 설정 및 DB
# =====================
KST = ZoneInfo("Asia/Seoul")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def now_kst(): return datetime.now(KST)

DATA_DIR = "/var/data" if os.path.isdir("/var/data") else os.path.join(os.getcwd(), "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "oseyo.db")

def db_conn(): return sqlite3.connect(DB_PATH, check_same_thread=False)

def db_init():
    with db_conn() as con:
        con.execute("CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, title TEXT NOT NULL, photo_b64 TEXT DEFAULT '', start_iso TEXT NOT NULL, end_iso TEXT NOT NULL, address TEXT NOT NULL, address_detail TEXT DEFAULT '', lat REAL NOT NULL, lng REAL NOT NULL, capacity_enabled INTEGER NOT NULL DEFAULT 0, capacity_max INTEGER, hidden INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);")
        con.execute("CREATE TABLE IF NOT EXISTS favorites (activity TEXT PRIMARY KEY, created_at TEXT NOT NULL);")
        con.commit()
db_init()

# [중략: DB/유틸 함수는 이전과 동일]
def db_insert_space(space):
    with db_conn() as con:
        con.execute("INSERT INTO spaces (id, title, photo_b64, start_iso, end_iso, address, address_detail, lat, lng, capacity_enabled, capacity_max, hidden, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (space["id"], space["title"], space.get("photo_b64",""), space["start_iso"], space["end_iso"], space["address"], space.get("address_detail",""), float(space["lat"]), float(space["lng"]), 1 if space.get("capacityEnabled") else 0, space.get("capacityMax"), 0, now_kst().isoformat()))
        con.commit()

def db_delete_space(sid):
    with db_conn() as con: con.execute("DELETE FROM spaces WHERE id=?", (sid,)); con.commit()

def db_list_spaces():
    with db_conn() as con: rows = con.execute("SELECT id, title, photo_b64, start_iso, end_iso, address, address_detail, lat, lng, capacity_enabled, capacity_max, hidden, created_at FROM spaces ORDER BY created_at DESC").fetchall()
    return [{"id": r[0], "title": r[1], "photo_b64": r[2], "start_iso": r[3], "end_iso": r[4], "address": r[5], "lat": r[7], "lng": r[8], "capacityEnabled": bool(r[9]), "capacityMax": r[10]} for r in rows]

def db_list_favorites():
    with db_conn() as con: rows = con.execute("SELECT activity FROM favorites ORDER BY created_at DESC").fetchall()
    return [r[0] for r in rows]

def db_add_favorite(act):
    with db_conn() as con: con.execute("INSERT OR IGNORE INTO favorites (activity, created_at) VALUES (?, ?)", (act, now_kst().isoformat())); con.commit()

def image_np_to_b64(np_img):
    if np_img is None: return ""
    im = Image.fromarray(np_img.astype("uint8")); buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80); return base64.b64encode(buf.getvalue()).decode("utf-8")

def kakao_keyword_search(q):
    if not KAKAO_REST_API_KEY: return [], "API 키 누락"
    r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}, params={"query": q, "size": 5})
    cands = [{"label": f"{d['place_name']} ({d['address_name']})", "place": d['place_name'], "lat": d['y'], "lng": d['x']} for d in r.json().get("documents", [])]
    return cands, ""

# =====================
# CSS & JS (레이아웃 긴급 수정)
# =====================
CSS = """
:root{--bg:#FAF9F6;--ink:#1F2937;--line:#E5E3DD;}
*{box-sizing:border-box!important;}
body{overflow-x:hidden!important; background:var(--bg)!important;}

/* 모달 본체 */
.modal-sheet {
    position: fixed !important; left: 50% !important; top: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: min(500px, 95vw) !important; max-height: 90vh !important;
    background: #fff !important; border-radius: 20px !important;
    z-index: 10001 !important; display: flex !important; flex-direction: column !important;
    box-shadow: 0 20px 40px rgba(0,0,0,0.2) !important; overflow: hidden !important;
}

/* 모달 내부 스크롤 영역 */
.modal-sheet > .form, .modal-sheet > .contain {
    overflow-y: auto !important; padding: 16px !important; flex: 1 !important;
}

/* 요소 겹침 방지: 모든 행을 수직으로 */
.modal-sheet .gr-row, .modal-sheet .row {
    display: flex !important; flex-direction: column !important; 
    gap: 12px !important; margin-bottom: 12px !important;
}

/* 입력칸 높이 및 폰트 */
.modal-sheet input, .modal-sheet textarea { min-height: 44px !important; font-size: 16px !important; }

/* 즐겨찾기 버튼 그리드 */
.fav-grid { display: grid !important; grid-template-columns: 1fr 1fr !important; gap: 8px !important; }
.fav-grid button { padding: 10px !important; background: #f3f4f6 !important; border: 1px solid #e5e7eb !important; border-radius: 8px !important; }

/* 푸터 고정 */
.modal-footer {
    padding: 12px 16px !important; background: #fff !important; 
    border-top: 1px solid var(--line) !important; 
    display: flex !important; flex-direction: row !important; gap: 10px !important;
}
.modal-footer button { flex: 1 !important; height: 48px !important; font-weight: 900 !important; }

/* FAB & Banner */
#fab-btn{position:fixed!important;right:20px!important;bottom:20px!important;z-index:2000!important;}
#fab-btn button{width:60px!important;height:60px!important;border-radius:50%!important;background:#ff6b00!important;color:#fff!important;font-size:30px!important;}
.banner{padding:10px; border-radius:10px; margin-bottom:10px; font-size:13px; text-align:center;}
.banner.ok{background:#dcfce7; color:#166534;}
.banner.warn{background:#fee2e2; color:#991b1b;}

/* 카드 스타일 */
.card { background: #fff; border: 1px solid var(--line); border-radius: 15px; padding: 15px; margin-bottom: 12px; position: relative; }
.rowcard { display: flex; justify-content: space-between; align-items: center; }
.thumb { width: 80px; height: 80px; border-radius: 10px; object-fit: cover; background: #eee; }
"""

JS_BOOT = """
function apply(){
    const inputs = document.querySelectorAll("#start_dt_box input, #end_dt_box input");
    inputs.forEach(i => { i.type="datetime-local"; i.style.width="100%"; });
}
setTimeout(apply, 500);
"""

# =====================
# Gradio UI
# =====================
with gr.Blocks(css=CSS, title="Oseyo") as demo:
    search_results = gr.State([])
    selected_place = gr.Textbox(visible=False, value="{}")

    with gr.Tabs():
        with gr.Tab("탐색"):
            home_html = gr.HTML()
            ref_btn = gr.Button("🔄 리스트 새로고침")
        with gr.Tab("지도"):
            map_html = gr.HTML()

    fab_btn = gr.Button("+", elem_id="fab-btn")
    modal_overlay = gr.HTML("<div style='position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:10000;'></div>", visible=False)

    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal_sheet:
        gr.HTML("<div style='text-align:center;padding:10px;font-weight:900;border-bottom:1px solid #eee;'>새 공간 열기</div>")
        
        with gr.Column():
            act_txt = gr.Textbox(label="📝 활동명", placeholder="예: 스터디, 산책, 커피")
            with gr.Row(elem_classes=["fav-grid"]):
                fav_btns = [gr.Button("", visible=False) for _ in range(6)]
            
            img_input = gr.Image(label="📸 사진 (선택)", type="numpy", height=150)
            
            with gr.Row():
                st_txt = gr.Textbox(label="📅 시작 일시", elem_id="start_dt_box")
                en_txt = gr.Textbox(label="⏰ 종료 일시", elem_id="end_dt_box")
            
            with gr.Row():
                cap_unlim = gr.Checkbox(label="👥 인원 제한 없음", value=True)
                cap_num = gr.Slider(label="최대 인원", minimum=1, maximum=10, value=4, step=1)
            
            with gr.Row():
                loc_q = gr.Textbox(label="📍 장소 검색", placeholder="장소명을 입력하세요")
                loc_btn = gr.Button("🔍 검색", size="sm")
            
            loc_res = gr.Radio(label="검색 결과 선택", choices=[], visible=False)
            status_msg = gr.Markdown("")

        with gr.Row(elem_classes=["modal-footer"]):
            close_btn = gr.Button("취소", variant="secondary")
            save_btn = gr.Button("✅ 공간 만들기", variant="primary")

    # 이벤트 정의
    def update_list():
        items = db_list_spaces()
        if not items: return "<div class='banner warn'>현재 열린 공간이 없습니다.</div>"
        html = "<div class='banner ok'>영구 저장 모드 활성화됨</div>"
        for i in items:
            img = f"data:image/jpeg;base64,{i['photo_b64']}" if i['photo_b64'] else ""
            html += f"<div class='card'><div class='rowcard'><div><b>{i['title']}</b><br><small>{i['address']}</small></div>"
            if img: html += f"<img src='{img}' class='thumb'>"
            html += f"</div><a href='/delete/{i['id']}' style='color:red;font-size:12px;'>[삭제]</a></div>"
        return html

    def open_modal():
        now = now_kst(); end = now + timedelta(hours=2)
        favs = db_list_favorites()
        fav_updates = [gr.update(visible=False)] * 6
        for i, f in enumerate(favs[:6]): fav_updates[i] = gr.update(value=f, visible=True)
        return gr.update(visible=True), gr.update(visible=True), now.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M"), *fav_updates

    fab_btn.click(open_modal, None, [modal_overlay, modal_sheet, st_txt, en_txt, *fav_btns], js=JS_BOOT)
    close_btn.click(lambda: (gr.update(visible=False), gr.update(visible=False)), None, [modal_overlay, modal_sheet])
    
    def search(q):
        c, err = kakao_keyword_search(q)
        if err: return [], gr.update(visible=False), err
        return c, gr.update(choices=[x['label'] for x in c], visible=True), "검색 완료"
    
    loc_btn.click(search, loc_q, [search_results, loc_res, status_msg])
    loc_res.change(lambda c, l: next((json.dumps(x, ensure_ascii=False) for x in c if x['label']==l), "{}"), [search_results, loc_res], selected_place)

    def save(act, st, en, cap_u, cap_n, img, loc_json):
        loc = json.loads(loc_json)
        if not act or 'lat' not in loc: return "활동명과 장소를 확인해주세요.", update_list(), gr.update(visible=True)
        db_insert_space({"id": uuid.uuid4().hex[:8], "title": act, "photo_b64": image_np_to_b64(img), "start_iso": st, "end_iso": en, "address": loc['place'], "lat": loc['lat'], "lng": loc['lng'], "capacityEnabled": not cap_u, "capacityMax": cap_n})
        db_add_favorite(act)
        return "✅ 저장 완료!", update_list(), gr.update(visible=False)

    save_btn.click(save, [act_txt, st_txt, en_txt, cap_unlim, cap_num, img_input, selected_place], [status_msg, home_html, modal_sheet])
    demo.load(update_list, None, home_html)
    ref_btn.click(update_list, None, home_html)

# =====================
# 서버 실행
# =====================
app = FastAPI()
@app.get("/delete/{sid}")
def delete(sid: str): db_delete_space(sid); return RedirectResponse(url="/", status_code=302)
@app.get("/kakao_map")
def kakao_map():
    pts = db_list_spaces()
    html = f"<html><body style='margin:0;'><div id='map' style='width:100%;height:100vh;'></div><script src='//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}'></script><script>const map=new kakao.maps.Map(document.getElementById('map'),{{center:new kakao.maps.LatLng(36.019,129.343),level:7}}); pts={json.dumps(pts)}.forEach(p=>new kakao.maps.Marker({{map,position:new kakao.maps.LatLng(p.lat,p.lng)}}));</script></body></html>"
    return HTMLResponse(html)

app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
