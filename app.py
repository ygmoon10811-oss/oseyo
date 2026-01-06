import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image
import gradio as gr

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, HTMLResponse

# 1. 설정 및 환경 변수
KST = ZoneInfo("Asia/Seoul")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def now_kst():
    return datetime.now(KST)

# 2. DB 설정 (Render 무료 티어는 재배포 시 DB가 초기화될 수 있음)
DB_PATH = "oseyo.db"

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def db_init():
    with db_conn() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS spaces (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, photo_b64 TEXT DEFAULT '',
            start_iso TEXT NOT NULL, end_iso TEXT NOT NULL, address TEXT NOT NULL,
            lat REAL NOT NULL, lng REAL NOT NULL, created_at TEXT NOT NULL
        );
        """)
        con.commit()

db_init()

# 3. 데이터 로직
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
                out.append({"id":r[0], "title":r[1], "photo_b64":r[2], "address":r[5], "lat":r[6], "lng":r[7]})
        except: continue
    return out

def kakao_keyword_search(q):
    if not q or not KAKAO_REST_API_KEY: return [], "⚠️ API 키를 확인하세요."
    try:
        r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", 
                         headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}, 
                         params={"query": q, "size": 8}, timeout=5)
        cands = [{"label": f"{d['place_name']} ({d.get('road_address_name') or d.get('address_name')})", 
                  "place": d['place_name'], "lat": float(d['y']), "lng": float(d['x'])} for d in r.json().get("documents", [])]
        return cands, f"✅ {len(cands)}개 검색됨"
    except: return [], "❌ API 오류"

# 4. 스타일링 (요청하신 UI 확장 반영)
CSS = """
:root{--primary:#FF6B00;}
.gradio-container{max-width:550px!important; background:#F9FAFB!important;}
.fab-container{position:fixed!important; right:20px!important; bottom:30px!important; z-index:999;}
.fab-container button{width:60px!important; height:60px!important; border-radius:30px!important; background:var(--primary)!important; color:white!important; font-size:35px!important; border:none!important; box-shadow:0 4px 15px rgba(0,0,0,0.3)!important;}
.modal-overlay{position:fixed; inset:0; background:rgba(0,0,0,0.6); z-index:1000; backdrop-filter:blur(5px);}
.modal-sheet{position:fixed!important; left:50%!important; top:50%!important; transform:translate(-50%,-50%)!important; width:92vw!important; max-width:500px!important; max-height:85vh!important; overflow-y:auto!important; background:white!important; border-radius:25px!important; padding:25px!important; z-index:1001!important;}

/* 입력 필드 확장 */
.modal-sheet .gr-text-input, .modal-sheet input{height:50px!important; font-size:16px!important; border-radius:12px!important;}
.card{background:white; border-radius:20px; padding:15px; margin-bottom:12px; border:1px solid #E5E7EB;}
.thumb{width:80px; height:80px; object-fit:cover; border-radius:15px;}
"""

with gr.Blocks(css=CSS, title="오세요") as demo:
    search_state = gr.State([])
    place_json = gr.Textbox(visible=False)

    # 상단 헤더 (엑스표 하신 아이콘 제거)
    gr.HTML("<h2 style='text-align:center; margin:20px 0;'>오세요 📍</h2>")
    
    with gr.Tabs():
        with gr.Tab("목록 보기"):
            def get_list():
                items = active_spaces()
                if not items: return "<p style='text-align:center; padding:50px;'>지금은 열린 공간이 없어요.</p>"
                res = ""
                for s in items:
                    img = f"<img class='thumb' src='data:image/jpeg;base64,{s['photo_b64']}' />" if s['photo_b64'] else ""
                    res += f"<div class='card'><div style='display:flex; justify-content:space-between; align-items:center;'><div><div style='font-weight:800; font-size:18px;'>{s['title']}</div><div style='color:#666; font-size:13px; margin-top:4px;'>{s['address']}</div></div>{img}</div><a href='/delete/{s['id']}' style='color:#EF4444; font-size:12px; text-decoration:none; font-weight:600; display:block; margin-top:10px;'>공간 닫기</a></div>"
                return res
            list_out = gr.HTML(get_list)
            gr.Button("🔄 리스트 새로고침").click(get_list, outputs=list_out)

        with gr.Tab("지도 보기"):
            def get_map_html():
                pts = active_spaces()
                center = [sum(p["lat"] for p in pts)/len(pts), sum(p["lng"] for p in pts)/len(pts)] if pts else [36.019, 129.343]
                return f"""
                <div style='width:100%; height:450px; border-radius:20px; overflow:hidden;' id='map'></div>
                <script src='//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}&autoload=false'></script>
                <script>
                    kakao.maps.load(function() {{
                        var map = new kakao.maps.Map(document.getElementById('map'), {{center: new kakao.maps.LatLng({center[0]}, {center[1]}), level: 5}});
                        {json.dumps(pts)}.forEach(p => new kakao.maps.Marker({{position: new kakao.maps.LatLng(p.lat, p.lng), map: map}}));
                    }});
                </script>"""
            map_out = gr.HTML(get_map_html)
            gr.Button("🔄 지도 새로고침").click(get_map_html, outputs=map_out)

    # FAB & 모달
    fab_btn = gr.Button("+", elem_classes=["fab-container"])
    modal_overlay = gr.HTML("<div class='modal-overlay'></div>", visible=False)
    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal_sheet:
        gr.HTML("<h3 style='margin-bottom:15px; font-size:20px;'>🏠 새로운 공간 열기</h3>")
        title_in = gr.Textbox(label="무엇을 하나요?", placeholder="예: 커피 한 잔 해요")
        img_in = gr.Image(label="사진 첨부 (선택)", type="numpy", height=180)
        
        with gr.Row():
            # 클릭형 캘린더/시각 선택기
            start_in = gr.DateTime(label="시작 일시", value=now_kst)
            end_in = gr.DateTime(label="종료 일시", value=lambda: now_kst()+timedelta(hours=2))
        
        with gr.Row():
            # 장소 검색란 가로로 길게 확장
            q_in = gr.Textbox(label="장소 검색", placeholder="어디서 만날까요?", scale=5)
            s_btn = gr.Button("🔍", scale=1)
            
        drop_in = gr.Dropdown(label="검색 결과에서 선택", choices=[])
        msg_in = gr.Markdown("")
        
        with gr.Row():
            c_btn = gr.Button("취소")
            ok_btn = gr.Button("공간 열기", variant="primary")

    # 인터랙션
    fab_btn.click(lambda: [gr.update(visible=True)]*2, outputs=[modal_overlay, modal_sheet])
    c_btn.click(lambda: [gr.update(visible=False)]*2, outputs=[modal_overlay, modal_sheet])
    
    s_btn.click(kakao_keyword_search, q_in, [drop_in, msg_in]).then(lambda r: r[0], drop_in, search_state)
    drop_in.change(lambda cands, lbl: next((json.dumps(c) for c in cands if c["label"] == lbl), "{}"), [search_state, drop_in], place_json)

    def save_event(title, img, s, e, p_js):
        if not title or p_js == "{}": return "⚠️ 활동명과 장소를 모두 입력해주세요.", get_list(), get_map_html(), gr.update(visible=True)
        try:
            p = json.loads(p_js)
            b64 = ""
            if img is not None:
                buf = io.BytesIO(); Image.fromarray(img).save(buf, format="JPEG"); b64 = base64.b64encode(buf.getvalue()).decode()
            
            with db_conn() as con:
                con.execute("INSERT INTO spaces VALUES (?,?,?,?,?,?,?,?,?)", 
                           (uuid.uuid4().hex[:8], title, b64, s, e, p["place"], p["lat"], p["lng"], now_kst().isoformat()))
            return "✅ 공간이 열렸습니다!", get_list(), get_map_html(), gr.update(visible=False)
        except Exception as err: return f"❌ 오류: {str(err)}", get_list(), get_map_html(), gr.update(visible=True)

    ok_btn.click(save_event, [title_in, img_in, start_in, end_in, place_json], [msg_in, list_out, map_out, modal_sheet]).then(
        lambda m: [gr.update(visible=False)]*2 if "✅" in m else [gr.update(visible=True)]*2, msg_area if 'msg_area' in locals() else msg_in, [modal_overlay, modal_sheet])

# 6. FastAPI 서버 및 포트 바인딩
app = FastAPI()
@app.get("/")
def go_home(): return RedirectResponse("/app")
@app.get("/delete/{sid}")
def del_item(sid):
    with db_conn() as con: con.execute("DELETE FROM spaces WHERE id=?", (sid,))
    return RedirectResponse("/app")

# mount_gradio_app에서 head 인자 제거 (오류 원인 해결)
app = gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    import uvicorn
    # Render 필수: PORT 환경변수 읽기
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
