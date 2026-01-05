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
# DB 설정 및 로직 (기존 동일)
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
            id TEXT PRIMARY KEY, title TEXT NOT NULL, photo_b64 TEXT DEFAULT '',
            start_iso TEXT NOT NULL, end_iso TEXT NOT NULL, address TEXT NOT NULL,
            address_detail TEXT DEFAULT '', lat REAL NOT NULL, lng REAL NOT NULL,
            capacity_enabled INTEGER NOT NULL DEFAULT 0, capacity_max INTEGER,
            hidden INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        """)
        con.commit()

db_init()

def db_insert_space(space: dict):
    with db_conn() as con:
        con.execute("""
        INSERT INTO spaces (id, title, photo_b64, start_iso, end_iso, address, lat, lng, capacity_enabled, capacity_max, hidden, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (space["id"], space["title"], space["photo_b64"], space["start_iso"], space["end_iso"], 
              space["address"], space["lat"], space["lng"], space["capacity_enabled"], space["capacity_max"], 0, now_kst().isoformat()))
        con.commit()

def db_delete_space(space_id: str):
    with db_conn() as con:
        con.execute("DELETE FROM spaces WHERE id=?", (space_id,))
        con.commit()

def active_spaces():
    with db_conn() as con:
        rows = con.execute("SELECT * FROM spaces ORDER BY created_at DESC").fetchall()
    t = now_kst()
    out = []
    for r in rows:
        st = datetime.fromisoformat(r[3]).replace(tzinfo=KST)
        en = datetime.fromisoformat(r[4]).replace(tzinfo=KST)
        if st <= t <= en:
            out.append({"id":r[0], "title":r[1], "photo_b64":r[2], "start_iso":r[3], "end_iso":r[4], "address":r[5], "lat":r[7], "lng":r[8], "capacityEnabled":bool(r[9]), "capacityMax":r[10]})
    return out

# -------------------------
# UI Helper
# -------------------------
def image_np_to_b64(img_np):
    if img_np is None: return ""
    im = Image.fromarray(img_np.astype("uint8"))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def kakao_keyword_search(q):
    if not q or not KAKAO_REST_API_KEY: return [], "검색어를 입력하세요."
    r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", 
                     headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}, params={"query": q, "size": 8})
    cands = [{"label": f"{d['place_name']} ({d.get('road_address_name') or d.get('address_name')})", 
              "place": d['place_name'], "lat": float(d['y']), "lng": float(d['x'])} for d in r.json().get("documents", [])]
    return cands, f"✅ {len(cands)}개 결과 발견" if cands else "⚠️ 결과 없음"

def render_home():
    items = active_spaces()
    if not items: return "<div class='card empty'>현재 활성화된 공간이 없습니다.</div>"
    out = []
    for s in items:
        img = f"<img class='thumb' src='data:image/jpeg;base64,{s['photo_b64']}' />" if s['photo_b64'] else "<div class='thumb placeholder'></div>"
        out.append(f"<div class='card'><div class='rowcard'><div class='left'><div class='title'>{s['title']}</div><div class='muted'>📍 {s['address']}</div></div><div class='right'>{img}</div></div><a class='btn-del' href='/delete/{s['id']}'>닫기</a></div>")
    return "\n".join(out)

# -------------------------
# CSS (UI 공간 및 가시성 개선)
# -------------------------
CSS = """
:root{--bg:#F3F4F6;--primary:#FF6B00;--dark:#1F2937;}
.gradio-container{max-width:600px!important; background:white!important;}

/* FAB */
.fab-container{position:fixed!important;right:20px!important;bottom:30px!important;z-index:999;}
.fab-container button{width:60px!important;height:60px!important;border-radius:30px!important;background:var(--primary)!important;color:white!important;font-size:32px!important;box-shadow:0 4px 12px rgba(0,0,0,0.3)!important;border:none!important;}

/* 모달 레이아웃 확장 */
.modal-sheet{
    position:fixed!important;left:50%!important;top:50%!important;transform:translate(-50%,-50%)!important;
    width:min(540px, 95vw)!important;max-height:90vh!important;overflow-y:auto!important;
    background:white!important;border-radius:24px!important;padding:24px!important;
    z-index:1001!important;box-shadow:0 20px 50px rgba(0,0,0,0.3)!important;
}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:1000;backdrop-filter:blur(5px);}

/* 입력란 높이 및 폰트 개선 */
.modal-sheet label{font-weight:700!important; font-size:15px!important; margin-bottom:8px!important; display:block;}
.modal-sheet input, .modal-sheet select, .modal-sheet textarea{padding:12px!important; font-size:15px!important; border-radius:10px!important;}

/* 리스트 카드 */
.card{border:1px solid #E5E7EB; border-radius:16px; padding:16px; margin-bottom:12px; background:#fff;}
.title{font-size:18px; font-weight:800; margin-bottom:4px;}
.thumb{width:80px; height:80px; object-fit:cover; border-radius:12px;}
.btn-del{color:#EF4444; font-size:13px; font-weight:600; text-decoration:none;}
"""

# -------------------------
# Gradio 앱
# -------------------------
with gr.Blocks(css=CSS, title="오세요") as demo:
    search_results = gr.State([])
    selected_place = gr.Textbox(visible=False)

    gr.HTML("<h2 style='text-align:center; padding:20px 0;'>지금 오세요 📍</h2>")
    
    with gr.Tabs():
        with gr.Tab("전체 보기"):
            home_html = gr.HTML(render_home)
            gr.Button("🔄 새로고침").click(render_home, outputs=home_html)
        with gr.Tab("지도"):
            gr.HTML("지도 기능 준비 중")

    # FAB 버튼
    with gr.Row(elem_classes=["fab-container"]):
        fab_btn = gr.Button("+")

    # 모달 창
    modal_overlay = gr.HTML("<div class='modal-overlay'></div>", visible=False)
    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal_sheet:
        gr.HTML("<h3 style='margin-bottom:20px; font-size:20px;'>새로운 공간 열기</h3>")
        
        act_input = gr.Textbox(label="활동명", placeholder="무엇을 하나요?", lines=1)
        img_input = gr.Image(label="사진", type="numpy", height=180)
        
        with gr.Row():
            # DateTime 컴포넌트로 변경하여 캘린더와 시간 선택 UI 제공
            start_dt = gr.DateTime(label="시작 일시", value=now_kst)
            end_dt = gr.DateTime(label="종료 일시", value=lambda: now_kst() + timedelta(hours=2))
        
        with gr.Row():
            unlimit = gr.Checkbox(label="인원 무제한", value=True)
            c_max = gr.Slider(label="최대 인원", minimum=1, maximum=20, value=4, step=1)
            
        with gr.Row():
            # 장소 검색란 가로 길이 확장 (scale 4)
            p_query = gr.Textbox(label="장소 검색", placeholder="장소명이나 주소 입력", scale=4)
            s_btn = gr.Button("🔍", scale=1)
            
        p_drop = gr.Dropdown(label="검색 결과에서 선택", choices=[])
        msg_out = gr.Markdown("")
        
        with gr.Row():
            close_btn = gr.Button("취소")
            save_btn = gr.Button("공간 열기", variant="primary")

    # 이벤트 바인딩
    def toggle_m(v): return gr.update(visible=v), gr.update(visible=v)
    fab_btn.click(lambda: toggle_m(True), outputs=[modal_overlay, modal_sheet])
    close_btn.click(lambda: toggle_m(False), outputs=[modal_overlay, modal_sheet])

    def do_search(q):
        cands, msg = kakao_keyword_search(q)
        return gr.update(choices=[c["label"] for c in cands]), msg, cands
    s_btn.click(do_search, p_query, [p_drop, msg_out, search_results])

    p_drop.change(lambda cands, lbl: next((json.dumps(c) for c in cands if c["label"] == lbl), "{}"), 
                  [search_results, p_drop], selected_place)

    def do_save(title, img, s_dt, e_dt, unl, c_m, p_json):
        if not title or p_json == "{}": return "⚠️ 활동명과 장소를 확인하세요.", render_home(), gr.update(visible=True)
        db_insert_space({
            "id": uuid.uuid4().hex[:8], "title": title, "photo_b64": image_np_to_b64(img),
            "start_iso": s_dt, "end_iso": e_dt, "address": json.loads(p_json)["place"],
            "lat": json.loads(p_json)["lat"], "lng": json.loads(p_json)["lng"],
            "capacity_enabled": not unl, "capacity_max": c_m
        })
        return "✅ 등록 완료!", render_home(), gr.update(visible=False)

    save_btn.click(do_save, [act_input, img_input, start_dt, end_dt, unlimit, c_max, selected_place], 
                   [msg_out, home_html, modal_sheet]).then(lambda: toggle_m(False), outputs=[modal_overlay, modal_sheet])

# -------------------------
# FastAPI
# -------------------------
app = FastAPI()
@app.get("/")
def root(): return RedirectResponse(url="/app")
@app.get("/delete/{space_id}")
def delete(space_id: str):
    db_delete_space(space_id)
    return RedirectResponse(url="/app")

app = gr.mount_gradio_app(app, demo, path="/app")
