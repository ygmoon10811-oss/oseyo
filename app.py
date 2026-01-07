# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json, html
from datetime import datetime, timedelta

import requests
from PIL import Image

import gradio as gr
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# -----------------------------
# 1) 환경 및 DB 설정
# -----------------------------
def pick_db_path():
    # Render 및 클라우드 환경에서는 현재 실행 디렉토리에 생성하는 것이 가장 확실함
    return "oseyo_final.db"

DB_PATH = pick_db_path()
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

with db_conn() as con:
    con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            title TEXT,
            photo TEXT,
            start TEXT,
            end TEXT,
            addr TEXT,
            lat REAL,
            lng REAL,
            created_at TEXT
        );
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS favs (
            name TEXT PRIMARY KEY,
            count INTEGER DEFAULT 1
        );
    """)
    con.commit()

# -----------------------------
# 2) CSS (가림 현상 해결 핵심)
# -----------------------------
CSS = """
html, body { margin: 0 !important; padding: 0 !important; overflow-x: hidden !important; }
.gradio-container { max-width: 100% !important; padding-bottom: 100px !important; }

.fab-wrapper { 
  position: fixed !important; 
  right: 30px !important; 
  bottom: 30px !important; 
  z-index: 9999 !important; 
  width: auto !important; 
}

.fab-wrapper button {
  width: 65px !important; height: 65px !important;
  border-radius: 50% !important;
  background: #ff6b00 !important;
  color: white !important; font-size: 40px !important;
  border: none !important; box-shadow: 0 4px 15px rgba(0,0,0,0.4) !important;
  cursor: pointer !important;
}

.overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 10000; }

.main-modal {
  position: fixed !important; top: 50%; left: 50%; transform: translate(-50%, -50%);
  width: 92vw; max-width: 500px; height: 86vh;
  background: white; z-index: 10001; border-radius: 24px;
  display: flex; flex-direction: column; overflow: hidden;
}

.modal-header { padding: 20px; border-bottom: 2px solid #eee; font-weight: 800; font-size: 20px; flex-shrink: 0; }

.modal-body {
  flex: 1; overflow-y: auto; padding: 20px;
  display: flex; flex-direction: column; gap: 16px;
}

/* 입력창이 길어져도 다른 요소를 밀어내지 않게 제한 */
.modal-body .gradio-textbox textarea {
  max-height: 100px !important;
  overflow-y: auto !important;
}

/* 이미지가 찌그러지지 않게 최소 높이 보장 */
.modal-body .gradio-image {
  min-height: 160px !important;
  flex-shrink: 0 !important;
}

.modal-footer {
  padding: 16px 20px; border-top: 2px solid #eee; background: #f9f9f9;
  display: flex; gap: 10px; flex-shrink: 0;
}
.modal-footer button { flex: 1; padding: 12px; border-radius: 12px; font-weight: 700; }

.fav-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; flex-shrink: 0; }
.fav-grid button { font-size: 13px; padding: 10px 8px; border-radius: 10px; overflow: hidden; text-overflow: ellipsis; }

.event-card {
  background: #f9f9f9; border-radius: 16px; padding: 16px; margin-bottom: 14px;
  border: 1px solid #e5e5e5; display: grid; grid-template-columns: 1fr 120px; gap: 14px; align-items: center;
}
.event-photo { width: 120px; height: 120px; object-fit: cover; border-radius: 12px; }
"""

# -----------------------------
# 3) 로직 함수
# -----------------------------
def get_list_html():
    with db_conn() as con:
        rows = con.execute("SELECT title, photo, start, addr FROM events ORDER BY created_at DESC").fetchall()
    if not rows:
        return "<div style='text-align:center; padding:60px; color:#999;'>등록된 이벤트가 없습니다.</div>"
    
    html_out = "<div style='padding:16px;'>"
    for title, photo, start, addr in rows:
        img_html = f"<img class='event-photo' src='data:image/jpeg;base64,{photo}' />" if photo else "<div class='event-photo' style='background:#e0e0e0;'></div>"
        html_out += f"""
        <div class='event-card'>
            <div class='event-info'>
                <div style='font-weight:800; font-size:18px;'>{html.escape(title or "")}</div>
                <div style='font-size:13px; color:#666;'>📅 {html.escape(start or "")}</div>
                <div style='font-size:13px; color:#666;'>📍 {html.escape(addr or "")}</div>
            </div>
            {img_html}
        </div>
        """
    return html_out + "</div>"

def save_data(title, img, start, end, addr_obj):
    title = (title or "").strip()
    if not title: return "제목을 입력해 주세요"
    
    pic_b64 = ""
    if img is not None:
        try:
            im = Image.fromarray(img).convert("RGB")
            im.thumbnail((600, 600))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            pic_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        except: pass

    addr_name = (addr_obj or {}).get("name", "").strip()
    lat, lng = (addr_obj or {}).get("y", 0), (addr_obj or {}).get("x", 0)

    with db_conn() as con:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex[:8], title, pic_b64, start or "", end or "", addr_name, lat, lng, datetime.now().isoformat()))
        con.execute("INSERT INTO favs (name, count) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET count = count + 1", (title,))
        con.commit()
    return "✅ 생성 완료"

# -----------------------------
# 4) FastAPI 및 Gradio 앱 구성
# -----------------------------
app = FastAPI()  # uvicorn app:app 호출을 위한 선언

now_dt = datetime.now()
later_dt = now_dt + timedelta(hours=2)

with gr.Blocks(css=CSS, title="오세요") as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    gr.Markdown("# 지금, 열려 있습니다\n원하시면 오세요")
    with gr.Tabs():
        with gr.Tab("탐색"):
            explore_html = gr.HTML(get_list_html())
            refresh_btn = gr.Button("🔄 새로고침", size="sm")
        with gr.Tab("지도"):
            gr.HTML('<iframe src="/map" style="width:100%;height:70vh;border:none;border-radius:16px;"></iframe>')

    fab = gr.Button("+", elem_classes=["fab-wrapper"])
    overlay = gr.HTML("<div class='overlay'></div>", visible=False)

    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_m:
        gr.HTML("<div class='modal-header'>새 이벤트 만들기</div>")
        with gr.Column(elem_classes=["modal-body"]):
            with gr.Row():
                # lines=1, max_lines=3 로 설정하여 가림 현상 해결
                t_in = gr.Textbox(label="📝 이벤트명", lines=1, max_lines=3, scale=3)
                add_fav_btn = gr.Button("⭐", scale=1, size="sm")
            
            fav_msg = gr.Markdown("")
            with gr.Column(elem_classes=["fav-grid"]):
                f_btns = [gr.Button("", visible=False, size="sm") for _ in range(10)]

            img_in = gr.Image(label="📸 사진 (선택)", type="numpy", height=150)
            with gr.Row():
                s_in = gr.Textbox(label="📅 시작", value=now_dt.strftime("%Y-%m-%d %H:%M"))
                e_in = gr.Textbox(label="⏰ 종료", value=later_dt.strftime("%Y-%m-%d %H:%M"))

            addr_v = gr.Textbox(label="📍 장소", interactive=False)
            addr_btn = gr.Button("🔍 장소 검색")
            msg_out = gr.Markdown("")
        
        with gr.Row(elem_classes=["modal-footer"]):
            m_close = gr.Button("취소")
            m_save = gr.Button("✅ 생성", variant="primary")

    # 서브 모달 (검색)
    with gr.Column(visible=False, elem_classes=["sub-modal"]) as modal_s:
        with gr.Column(elem_classes=["sub-body"]):
            gr.Markdown("### 📍 장소 검색")
            q_in = gr.Textbox(label="검색어")
            q_btn = gr.Button("검색")
            q_res = gr.Radio(label="결과", choices=[], interactive=True)
            with gr.Row():
                s_close = gr.Button("뒤로")
                s_final = gr.Button("확정")

    # 핸들러
    def open_m():
        with db_conn() as con:
            rows = con.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 10").fetchall()
        updates = [gr.update(visible=False, value="")] * 10
        for i, r in enumerate(rows): updates[i] = gr.update(visible=True, value=r[0])
        return [gr.update(visible=True), gr.update(visible=True)] + updates

    fab.click(open_m, None, [overlay, modal_m, *f_btns])
    m_close.click(lambda: [gr.update(visible=False), gr.update(visible=False)], None, [overlay, modal_m])

    def search_k(q):
        if not KAKAO_REST_API_KEY: return [], gr.update(choices=["키 설정 필요"])
        res = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", 
                           headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"},
                           params={"query": q, "size": 8}).json()
        docs = res.get("documents", [])
        cands = [{"label": f"{d['place_name']} | {d['address_name']}", "name": d['place_name'], "y": d['y'], "x": d['x']} for d in docs]
        return cands, gr.update(choices=[x["label"] for x in cands])

    q_btn.click(search_k, q_in, [search_state, q_res])
    addr_btn.click(lambda: gr.update(visible=True), None, modal_s)
    s_close.click(lambda: gr.update(visible=False), None, modal_s)

    def confirm_k(sel, cands):
        item = next((x for x in cands if x["label"] == sel), None)
        return (item["label"], item, gr.update(visible=False)) if item else ("", {}, gr.update(visible=False))

    s_final.click(confirm_k, [q_res, search_state], [addr_v, selected_addr, modal_s])

    def save_and_close(t, i, s, e, a):
        msg = save_data(t, i, s, e, a)
        return msg, get_list_html(), gr.update(visible=False), gr.update(visible=False)

    m_save.click(save_and_close, [t_in, img_in, s_in, e_in, selected_addr], [msg_out, explore_html, overlay, modal_m])
    refresh_btn.click(get_list_html, None, explore_html)

# -----------------------------
# 5) 지도 API 및 실행
# -----------------------------
@app.get("/map")
def map_h():
    with db_conn() as con:
        rows = con.execute("SELECT title, photo, lat, lng, addr, start FROM events").fetchall()
    # (지도 HTML 로직 생략 - 기존과 동일하게 유지)
    return HTMLResponse(f"<html>...Kakao Map 로직... {json.dumps(rows)}</html>") # 실제 배포시 기존 지도 코드 삽입

app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
