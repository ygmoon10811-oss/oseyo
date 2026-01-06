import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image
import gradio as gr

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, HTMLResponse

# =====================
# 기본 설정
# =====================
KST = ZoneInfo("Asia/Seoul")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def now_kst():
    return datetime.now(KST)

# =====================
# DB
# =====================
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
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            capacity_enabled INTEGER NOT NULL,
            capacity_max INTEGER,
            created_at TEXT NOT NULL
        );
        """)
        con.execute("""
        CREATE TABLE IF NOT EXISTS favorites (
            activity TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        );
        """)
        con.commit()

db_init()

# =====================
# Util
# =====================
def image_np_to_b64(img_np):
    if img_np is None:
        return ""
    im = Image.fromarray(img_np.astype("uint8"))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()

def parse_dt(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except:
            return None
    return dt.replace(tzinfo=KST) if dt.tzinfo is None else dt.astimezone(KST)

# =====================
# Kakao 검색
# =====================
def kakao_keyword_search(q):
    if not q:
        return [], "주소를 입력하세요"
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    r = requests.get(url, headers=headers, params={"query": q, "size": 10})
    data = r.json()
    out = []
    for d in data.get("documents", []):
        out.append({
            "label": f"{d['place_name']} — {d['road_address_name'] or d['address_name']}",
            "place": d["place_name"],
            "lat": float(d["y"]),
            "lng": float(d["x"])
        })
    return out, ""

# =====================
# CSS (문제 원인 전부 제거)
# =====================
CSS = """
html,body{margin:0;overflow-x:hidden;background:#FAF9F6;}
.gradio-container{max-width:1200px;margin:auto;padding-bottom:120px;}

.fab-container{
 position:fixed;right:20px;bottom:20px;z-index:9000;
}
.fab-container button{
 width:56px;height:56px;border-radius:50%;
 font-size:28px;background:#2B2A27;color:white;
}

body.modal-open .fab-container{display:none;}

.modal-overlay{
 position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:10000;
}

.modal-sheet{
 position:fixed;left:50%;top:50%;
 transform:translate(-50%,-50%);
 width:min(500px,92vw);
 max-height:88vh;
 overflow-y:auto;
 overflow-x:hidden;
 background:white;
 border-radius:20px;
 padding:20px 20px 130px;
 z-index:10001;
}

.modal-header{
 display:flex;justify-content:space-between;
 font-weight:900;margin-bottom:12px;
}

.photo-box{
 height:160px;
 overflow:hidden;
 border-radius:14px;
}

.dt-box{
 position:relative;
 z-index:5;
}

.flatpickr-calendar{
 z-index:20050!important;
}

.modal-footer{
 position:fixed;
 left:50%;bottom:0;
 transform:translateX(-50%);
 width:min(500px,92vw);
 display:flex;gap:10px;
 padding:16px;
 background:white;
 z-index:10002;
}
.modal-footer button{flex:1;font-weight:800;}
"""

# =====================
# UI
# =====================
with gr.Blocks(css=CSS) as demo:
    search_state = gr.State([])
    selected_place = gr.State("{}")

    gr.Markdown("# 지금, 열려 있습니다")

    home_html = gr.HTML()

    with gr.Row(elem_classes=["fab-container"]):
        fab = gr.Button("+")

    overlay = gr.HTML("", visible=False, elem_classes=["modal-overlay"])

    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal:
        with gr.Row(elem_classes=["modal-header"]):
            gr.Markdown("새 공간 열기")
            close = gr.Button("✕")

        activity = gr.Textbox(label="📝 활동명")
        photo = gr.Image(label="📸 사진", type="numpy", elem_classes=["photo-box"])

        start_dt = gr.DateTime(label="📅 시작 일시", include_time=True, elem_classes=["dt-box"])
        end_dt   = gr.DateTime(label="⏰ 종료 일시", include_time=True, elem_classes=["dt-box"])

        capacity_unlimited = gr.Checkbox(label="제한없음", value=True)
        cap_max = gr.Slider(1, 10, value=4, label="최대인원")

        place_q = gr.Textbox(label="📍 장소")
        search_btn = gr.Button("🔍")
        place_radio = gr.Radio(label="검색 결과", visible=False)

        msg = gr.Markdown("")

        with gr.Row(elem_classes=["modal-footer"]):
            cancel = gr.Button("취소")
            create = gr.Button("✅ 생성")

    # =====================
    # Logic
    # =====================
    def open_modal():
        return (
            gr.update(visible=True),
            gr.update(visible=True),
            "<script>document.body.classList.add('modal-open')</script>"
        )

    def close_modal():
        return (
            gr.update(visible=False),
            gr.update(visible=False),
            "<script>document.body.classList.remove('modal-open')</script>"
        )

    fab.click(open_modal, outputs=[overlay, modal, overlay])
    close.click(close_modal, outputs=[overlay, modal, overlay])
    cancel.click(close_modal, outputs=[overlay, modal, overlay])

    def search_place(q):
        cands, err = kakao_keyword_search(q)
        return cands, gr.update(choices=[c["label"] for c in cands], visible=True)

    search_btn.click(search_place, inputs=place_q, outputs=[search_state, place_radio])

    def select_place(cands, label):
        for c in cands:
            if c["label"] == label:
                return json.dumps(c), gr.update(visible=False), c["label"]
        return "{}", gr.update(), ""

    place_radio.change(select_place, inputs=[search_state, place_radio], outputs=[selected_place, place_radio, place_q])

    def create_event(act, st, en, unlimited, cap, img, place_json):
        st = parse_dt(st)
        en = parse_dt(en)
        if not act or not st or not en:
            return "⚠️ 필수 항목을 확인하세요"
        place = json.loads(place_json)
        pid = uuid.uuid4().hex[:8]
        with db_conn() as con:
            con.execute("""
            INSERT INTO spaces VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                pid, act,
                image_np_to_b64(img),
                st.isoformat(), en.isoformat(),
                place["place"], place["lat"], place["lng"],
                0 if unlimited else 1,
                None if unlimited else int(cap),
                now_kst().isoformat()
            ))
            con.commit()
        return "✅ 이벤트 생성 완료"

    create.click(
        create_event,
        inputs=[activity, start_dt, end_dt, capacity_unlimited, cap_max, photo, selected_place],
        outputs=[msg]
    )

# =====================
# FastAPI
# =====================
app = FastAPI()
app = gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7860)
