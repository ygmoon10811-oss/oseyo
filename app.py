# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
import requests
from PIL import Image
import gradio as gr
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# [1. 환경 설정 및 DB]
# Render 환경에서는 /tmp 경로가 가장 안전합니다.
DB_PATH = "/tmp/oseyo_final.db"
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

with db_conn() as con:
    con.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, title TEXT, photo TEXT, start TEXT, end TEXT, addr TEXT, lat REAL, lng REAL, created_at TEXT);")
    con.execute("CREATE TABLE IF NOT EXISTS favs (name TEXT PRIMARY KEY, count INTEGER DEFAULT 1);")
    con.commit()

# [2. CSS] - 가로 스크롤 차단 및 정교한 모달 레이아웃
CSS = """
/* 가로 스크롤 방지 */
body, .gradio-container { overflow-x: hidden !important; max-width: 100vw !important; margin: 0; }
.main-wrapper { height: 100vh; overflow-y: auto; overflow-x: hidden; }

/* 플로팅 버튼 */
#fab { position: fixed !important; right: 25px; bottom: 35px; z-index: 1000; }
#fab button { width: 65px; height: 65px; border-radius: 50%; background: #ff6b00; color: white; font-size: 35px; box-shadow: 0 4px 15px rgba(0,0,0,0.3); border: none; }

/* 메인 모달 (생성창) */
.main-modal {
    position: fixed !important; top: 50%; left: 50%; transform: translate(-50%, -50%);
    width: 92%; max-width: 480px; height: 85vh; background: white; z-index: 10001;
    border-radius: 24px; box-shadow: 0 20px 60px rgba(0,0,0,0.5); display: flex; flex-direction: column;
}
.modal-body { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 15px; }

/* 주소 검색 모달 (모달 안의 모달) */
.sub-modal {
    position: fixed !important; top: 50%; left: 50%; transform: translate(-50%, -50%);
    width: 85%; max-width: 400px; height: 60vh; background: #fff; z-index: 10005;
    border-radius: 20px; border: 1px solid #ddd; box-shadow: 0 10px 40px rgba(0,0,0,0.4);
}

/* 2x5 즐겨찾기 그리드 */
.fav-grid { display: grid !important; grid-template-columns: 1fr 1fr !important; gap: 10px !important; margin-bottom: 5px; }

/* 배경 오버레이 */
#overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 10000; }
"""

# [3. 로직 함수]
def get_list_html():
    with db_conn() as con:
        rows = con.execute("SELECT title, photo, start, addr FROM events ORDER BY created_at DESC").fetchall()
    if not rows: return "<p style='text-align:center; padding:40px; color:#999;'>등록된 이벤트가 없습니다.</p>"
    
    html = "<div style='padding:15px;'>"
    for r in rows:
        img_src = f"data:image/jpeg;base64,{r[1]}" if r[1] else ""
        img_html = f"<img src='{img_src}' style='width:100%; height:150px; object-fit:cover; border-radius:12px;'>" if r[1] else ""
        html += f"""
        <div style='background:#f9f9f9; border-radius:16px; margin-bottom:15px; padding:15px; border:1px solid #eee;'>
            {img_html}
            <div style='font-weight:bold; font-size:18px; margin-top:10px;'>{r[0]}</div>
            <div style='font-size:14px; color:#666;'>📅 {r[2]}</div>
            <div style='font-size:14px; color:#666;'>📍 {r[3]}</div>
        </div>
        """
    return html + "</div>"

def save_data(title, img, start, end, addr_obj):
    if not title: return "제목 누락"
    pic = ""
    if img is not None:
        im = Image.fromarray(img).convert("RGB")
        im.thumbnail((500, 500))
        buf = io.BytesIO()
        im.save(buf, "JPEG")
        pic = base64.b64encode(buf.getvalue()).decode()
    
    with db_conn() as con:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex[:6], title, pic, start, end, addr_obj.get('name',''), addr_obj.get('y',0), addr_obj.get('x',0), datetime.now().isoformat()))
        con.execute("INSERT INTO favs (name) VALUES (?) ON CONFLICT(name) DO UPDATE SET count=count+1", (title,))
        con.commit()
    return "SUCCESS"

# [4. Gradio UI]
with gr.Blocks(css=CSS) as demo:
    search_state = gr.State([])
    selected_addr = gr.State({})

    with gr.Column(elem_classes=["main-wrapper"]):
        with gr.Tabs():
            with gr.Tab("탐색"):
                explore_html = gr.HTML(get_list_html)
            with gr.Tab("지도"):
                gr.HTML(f'<iframe src="/map" style="width:100%;height:80vh;border:none;"></iframe>')

    fab = gr.Button("+", elem_id="fab")
    overlay = gr.HTML("<div id='overlay'></div>", visible=False)

    # [Main Modal]
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_m:
        gr.HTML("<div style='padding:20px 20px 0; font-weight:bold; font-size:20px;'>새 이벤트</div>")
        with gr.Column(elem_classes=["modal-body"]):
            t_in = gr.Textbox(label="이벤트명", placeholder="입력하세요")
            
            with gr.Column():
                gr.Markdown("⭐ **즐겨찾기 (추가 가능)**")
                with gr.Column(elem_classes=["fav-grid"]):
                    f_btns = [gr.Button("", visible=False) for _ in range(10)]
            
            img_in = gr.Image(label="사진", type="numpy")
            
            with gr.Row():
                s_in = gr.Textbox(label="시작", value=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
                e_in = gr.Textbox(label="종료", value=lambda: (datetime.now()+timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"))
            
            addr_v = gr.Textbox(label="장소", interactive=False)
            addr_btn = gr.Button("📍 장소 검색")
            
            with gr.Row():
                m_close = gr.Button("닫기")
                m_save = gr.Button("생성", variant="primary")

    # [Sub Modal]
    with gr.Column(visible=False, elem_classes=["sub-modal"]) as modal_s:
        with gr.Column(style="padding:20px; gap:10px;"):
            gr.Markdown("### 주소 검색")
            q_in = gr.Textbox(label="검색어", placeholder="강남역...")
            q_btn = gr.Button("찾기")
            q_res = gr.Radio(label="결과", choices=[])
            with gr.Row():
                s_close = gr.Button("뒤로")
                s_final = gr.Button("확정", variant="primary")

    # --- 핸들러 ---
    def open_m():
        with db_conn() as con:
            rows = con.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 10").fetchall()
        updates = [gr.update(visible=False)] * 10
        for i, r in enumerate(rows): updates[i] = gr.update(visible=True, value=r[0])
        return [gr.update(visible=True), gr.update(visible=True)] + updates

    fab.click(open_m, None, [overlay, modal_m, *f_btns])
    m_close.click(lambda: [gr.update(visible=False)]*2, None, [overlay, modal_m])
    
    for b in f_btns: b.click(lambda x: x, b, t_in)

    addr_btn.click(lambda: gr.update(visible=True), None, modal_s)
    s_close.click(lambda: gr.update(visible=False), None, modal_s)

    def search_k(q):
        headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
        res = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", headers=headers, params={"query":q, "size":6}).json()
        docs = res.get("documents", [])
        cands = [{"label": f"{d['place_name']} | {d['address_name']}", "name": d['place_name'], "y": d['y'], "x": d['x']} for d in docs]
        return cands, gr.update(choices=[x['label'] for x in cands])
    
    q_btn.click(search_k, q_in, [search_state, q_res])

    def confirm_k(sel, cands):
        item = next((x for x in cands if x['label'] == sel), {})
        return item['label'], item, gr.update(visible=False)
    
    s_final.click(confirm_k, [q_res, search_state], [addr_v, selected_addr, modal_s])

    m_save.click(save_data, [t_in, img_in, s_in, e_in, selected_addr], None).then(
        get_list_html, None, explore_html
    ).then(lambda: [gr.update(visible=False)]*2, None, [overlay, modal_m])

# [5. 서버]
app = FastAPI()
@app.get("/map")
def map_h():
    with db_conn() as con: rows = con.execute("SELECT title, lat, lng FROM events").fetchall()
    return HTMLResponse(f"""
        <div id='m' style='width:100%;height:100vh;'></div>
        <script src='//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}'></script>
        <script>
            var map=new kakao.maps.Map(document.getElementById('m'),{{center:new kakao.maps.LatLng(37.56,126.97),level:7}});
            {json.dumps(rows)}.forEach(r=>new kakao.maps.Marker({{map:map,position:new kakao.maps.LatLng(r[1],r[2]),title:r[0]}}));
        </script>
    """)
app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
