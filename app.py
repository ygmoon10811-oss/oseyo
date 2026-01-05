import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image
import gradio as gr

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, HTMLResponse

# -------------------------
# 1. 설정 및 환경 변수
# -------------------------
KST = ZoneInfo("Asia/Seoul")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def now_kst():
    return datetime.now(KST)

# -------------------------
# 2. DB 설정 및 로직
# -------------------------
def get_data_dir():
    # 저장 경로를 현재 폴더의 data 폴더로 고정 (권한 문제 방지)
    path = os.path.join(os.getcwd(), "data")
    os.makedirs(path, exist_ok=True)
    return path

DATA_DIR = get_data_dir()
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
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            capacity_enabled INTEGER NOT NULL DEFAULT 0,
            capacity_max INTEGER,
            created_at TEXT NOT NULL
        );
        """)
        con.commit()

db_init()

def db_insert_space(space: dict):
    with db_conn() as con:
        con.execute("""
        INSERT INTO spaces (id, title, photo_b64, start_iso, end_iso, address, lat, lng, capacity_enabled, capacity_max, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            space["id"], space["title"], space["photo_b64"], space["start_iso"], space["end_iso"], 
            space["address"], space["lat"], space["lng"], space["capacity_enabled"], space["capacity_max"], 
            now_kst().isoformat()
        ))
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
        try:
            st = datetime.fromisoformat(r[3]).replace(tzinfo=KST)
            en = datetime.fromisoformat(r[4]).replace(tzinfo=KST)
            if st <= t <= en:
                out.append({
                    "id": r[0], "title": r[1], "photo_b64": r[2], 
                    "start_iso": r[3], "end_iso": r[4], "address": r[5], 
                    "lat": r[6], "lng": r[7], "capacity_enabled": bool(r[8]), "capacity_max": r[9]
                })
        except: continue
    return out

# -------------------------
# 3. 유틸리티 함수
# -------------------------
def image_np_to_b64(img_np):
    if img_np is None: return ""
    try:
        im = Image.fromarray(img_np.astype("uint8"))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except: return ""

def kakao_keyword_search(q):
    if not q or not KAKAO_REST_API_KEY:
        return [], "검색어를 입력하거나 API 키를 설정해주세요."
    try:
        r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", 
                         headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}, 
                         params={"query": q, "size": 8}, timeout=5)
        data = r.json()
        cands = [{"label": f"{d['place_name']} ({d.get('road_address_name') or d.get('address_name')})", 
                  "place": d['place_name'], "lat": float(d['y']), "lng": float(d['x'])} for d in data.get("documents", [])]
        return cands, f"✅ {len(cands)}개의 장소를 찾았습니다."
    except:
        return [], "⚠️ 카카오 API 연결에 실패했습니다."

def render_home():
    items = active_spaces()
    if not items:
        return "<div class='card empty'>현재 진행 중인 공간이 없어요. + 버튼을 눌러보세요!</div>"
    out = []
    for s in items:
        img_html = f"<img class='thumb' src='data:image/jpeg;base64,{s['photo_b64']}' />" if s['photo_b64'] else "<div class='thumb placeholder'></div>"
        out.append(f"""
        <div class='card'>
            <div class='rowcard'>
                <div class='left'>
                    <div class='title'>{s['title']}</div>
                    <div class='muted'>📍 {s['address']}</div>
                    <div class='idline'>ID: {s['id']}</div>
                </div>
                <div class='right'>{img_html}</div>
            </div>
            <a class='btn-del' href='/delete/{s['id']}'>닫기</a>
        </div>
        """)
    return "\n".join(out)

# -------------------------
# 4. 스타일링 (CSS)
# -------------------------
CSS = """
:root{--primary:#FF6B00;--bg:#F9FAFB;}
body{background:var(--bg)!important;}
.gradio-container{max-width:550px!important; margin:0 auto!important;}

/* FAB 버튼 */
.fab-container{position:fixed!important; right:20px!important; bottom:30px!important; z-index:999;}
.fab-container button{width:56px!important; height:56px!important; border-radius:28px!important; background:var(--primary)!important; color:white!important; font-size:32px!important; border:none!important; box-shadow:0 4px 15px rgba(0,0,0,0.2)!important;}

/* 모달 디자인 */
.modal-overlay{position:fixed; inset:0; background:rgba(0,0,0,0.5); z-index:1000; backdrop-filter:blur(4px);}
.modal-sheet{position:fixed!important; left:50%!important; top:50%!important; transform:translate(-50%,-50%)!important; width:92vw!important; max-width:500px!important; max-height:85vh!important; overflow-y:auto!important; background:white!important; border-radius:24px!important; padding:20px!important; z-index:1001!important; box-shadow:0 20px 40px rgba(0,0,0,0.2)!important;}

/* 입력창 확장 */
.modal-sheet label{font-weight:700!important; margin-bottom:6px!important;}
.modal-sheet input, .modal-sheet .gr-text-input{padding:12px!important; border-radius:12px!important;}

/* 카드 UI */
.card{background:white; border-radius:18px; padding:16px; margin-bottom:12px; border:1px solid #E5E7EB; position:relative;}
.rowcard{display:flex; justify-content:space-between; align-items:center;}
.title{font-size:17px; font-weight:800; margin-bottom:4px;}
.muted{font-size:13px; color:#6B7280;}
.idline{font-size:11px; color:#9CA3AF; margin-top:8px;}
.thumb{width:70px; height:70px; object-fit:cover; border-radius:12px;}
.btn-del{color:#EF4444; font-size:12px; font-weight:600; text-decoration:none; margin-top:10px; display:inline-block;}
"""

# -------------------------
# 5. Gradio UI 구성
# -------------------------
with gr.Blocks(css=CSS, title="오세요") as demo:
    search_state = gr.State([])
    place_json_state = gr.Textbox(visible=False)

    gr.HTML("<h2 style='text-align:center; margin:20px 0;'>오세요 📍</h2>")
    
    with gr.Tabs():
        with gr.Tab("실시간 목록"):
            home_html = gr.HTML(render_home)
            gr.Button("🔄 리스트 새로고침").click(render_home, outputs=home_html)
        with gr.Tab("지도"):
            gr.HTML("<div style='padding:50px; text-align:center; color:#999;'>지도는 개발 중입니다.</div>")

    # FAB 버튼
    with gr.Row(elem_classes=["fab-container"]):
        fab_btn = gr.Button("+")

    # 모달 창
    modal_overlay = gr.HTML("<div class='modal-overlay'></div>", visible=False)
    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal_sheet:
        gr.HTML("<h3 style='margin-bottom:15px;'>새 공간 등록</h3>")
        
        act_name = gr.Textbox(label="무엇을 하나요?", placeholder="예: 영일대 밤바다 산책")
        act_img = gr.Image(label="공간 사진 (선택)", type="numpy", height=150)
        
        with gr.Row():
            # Gradio의 내장 달력/시간 선택기
            start_dt = gr.DateTime(label="시작 일시", value=now_kst)
            end_dt = gr.DateTime(label="종료 일시", value=lambda: now_kst() + timedelta(hours=2))
        
        with gr.Row():
            unlimit_cap = gr.Checkbox(label="인원 무제한", value=True)
            max_cap = gr.Slider(label="최대 인원", minimum=1, maximum=10, value=4, step=1)
            
        with gr.Row():
            place_q = gr.Textbox(label="장소 검색", placeholder="장소명 입력", scale=4)
            search_btn = gr.Button("🔍", scale=1)
            
        place_dropdown = gr.Dropdown(label="정확한 장소 선택", choices=[])
        msg_area = gr.Markdown("")
        
        with gr.Row():
            cancel_btn = gr.Button("취소")
            confirm_btn = gr.Button("공간 열기", variant="primary")

    # 인터랙션 설정
    def show_m(): return gr.update(visible=True), gr.update(visible=True)
    def hide_m(): return gr.update(visible=False), gr.update(visible=False)

    fab_btn.click(show_m, outputs=[modal_overlay, modal_sheet])
    cancel_btn.click(hide_m, outputs=[modal_overlay, modal_sheet])

    def search_place(q):
        cands, msg = kakao_keyword_search(q)
        return gr.update(choices=[c["label"] for c in cands]), msg, cands
    search_btn.click(search_place, place_q, [place_dropdown, msg_area, search_state])

    def pick_place(cands, label):
        for c in cands:
            if c["label"] == label: return json.dumps(c)
        return "{}"
    place_dropdown.change(pick_place, [search_state, place_dropdown], place_json_state)

    def save_space(title, img, s_dt, e_dt, unl, c_m, p_json):
        if not title or p_json == "{}": 
            return "⚠️ 활동명과 장소를 모두 입력해주세요.", render_home(), gr.update(visible=True)
        try:
            p_data = json.loads(p_json)
            db_insert_space({
                "id": uuid.uuid4().hex[:8], "title": title, "photo_b64": image_np_to_b64(img),
                "start_iso": s_dt, "end_iso": e_dt, "address": p_data["place"],
                "lat": p_data["lat"], "lng": p_data["lng"],
                "capacity_enabled": not unl, "capacity_max": c_m
            })
            return "✅ 등록되었습니다!", render_home(), gr.update(visible=False)
        except Exception as e:
            return f"⚠️ 오류 발생: {str(e)}", render_home(), gr.update(visible=True)

    confirm_btn.click(save_space, 
                      [act_name, act_img, start_dt, end_dt, unlimit_cap, max_cap, place_json_state], 
                      [msg_area, home_html, modal_sheet]).then(lambda x: hide_m() if "✅" in x else None, msg_area, [modal_overlay, modal_sheet])

# -------------------------
# 6. 서버 통합 (FastAPI + Gradio)
# -------------------------
app = FastAPI()

@app.get("/")
def home_redirect():
    return RedirectResponse(url="/app")

@app.get("/delete/{space_id}")
def delete_space_api(space_id: str):
    db_delete_space(space_id)
    return RedirectResponse(url="/app")

# Gradio 마운트
app = gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)
