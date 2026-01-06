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
# 설정 및 DB (데이터 보존 우선)
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
        con.execute("CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, title TEXT NOT NULL, photo_b64 TEXT DEFAULT '', start_iso TEXT NOT NULL, end_iso TEXT NOT NULL, address TEXT NOT NULL, lat REAL NOT NULL, lng REAL NOT NULL, capacity_enabled INTEGER NOT NULL DEFAULT 0, capacity_max INTEGER, created_at TEXT NOT NULL);")
        con.execute("CREATE TABLE IF NOT EXISTS favorites (activity TEXT PRIMARY KEY, created_at TEXT NOT NULL);")
        con.commit()
db_init()

# =====================
# 강력한 레이아웃 CSS
# =====================
CSS = """
:root{--bg:#FAF9F6;--line:#E5E3DD;--brand:#ff6b00;}
*{box-sizing:border-box!important;}

/* 모달 본체: 화면 크기에 맞춰 가변적이되 스크롤 보장 */
.modal-sheet {
    position: fixed !important; left: 50% !important; top: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: min(520px, 95vw) !important; height: 85vh !important;
    background: #fff !important; border-radius: 24px !important;
    z-index: 10001 !important; display: flex !important; flex-direction: column !important;
    box-shadow: 0 25px 50px rgba(0,0,0,0.3) !important; overflow: hidden !important;
}

/* 모달 내부 본문: 주소창까지 스크롤 가능하게 */
.modal-body {
    flex: 1 !important; overflow-y: auto !important; padding: 20px !important;
    display: flex !important; flex-direction: column !important; gap: 16px !important;
}

/* 2x5 즐겨찾기 버튼 그리드 */
.fav-grid { 
    display: grid !important; 
    grid-template-columns: 1fr 1fr !important; 
    gap: 8px !important; 
    margin: 5px 0 !important;
}
.fav-grid button { 
    min-height: 44px !important; border: 1px solid #eee !important; 
    background: #f9f9f9 !important; border-radius: 10px !important;
}

/* 폼 요소 겹침 방지 */
.modal-body .gr-form, .modal-body .gr-box { border: none !important; background: transparent !important; }
.modal-body .row, .modal-body .gr-row { display: flex !important; flex-direction: column !important; gap: 12px !important; }

/* 푸터 고정 */
.modal-footer {
    padding: 16px; border-top: 1px solid #eee; background: #fff;
    display: flex !important; flex-direction: row !important; gap: 10px !important; flex-shrink: 0;
}
.modal-footer button { flex: 1 !important; height: 50px !important; font-weight: bold !important; }

/* FAB 버튼 */
#fab-btn{position:fixed!important;right:25px!important;bottom:25px!important;z-index:2000!important;}
#fab-btn button{width:65px!important;height:65px!important;border-radius:50%!important;background:var(--brand)!important;color:#fff!important;font-size:35px!important;box-shadow:0 8px 20px rgba(255,107,0,0.4)!important;}

/* 카드 UI */
.card { background: #fff; border: 1px solid var(--line); border-radius: 18px; padding: 18px; margin-bottom: 12px; }
.thumb { width: 85px; height: 85px; border-radius: 12px; object-fit: cover; }
"""

# =====================
# 서버 로직
# =====================
def image_to_b64(np_img):
    if np_img is None: return ""
    im = Image.fromarray(np_img.astype("uint8")); buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=80); return base64.b64encode(buf.getvalue()).decode("utf-8")

def search_kakao(q):
    if not q or not KAKAO_REST_API_KEY: return [], gr.update(visible=False), "장소를 입력해 주세요."
    r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json", headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}, params={"query": q, "size": 8})
    data = r.json().get("documents", [])
    cands = [{"label": f"{d['place_name']} ({d['address_name']})", "place": d['place_name'], "lat": d['y'], "lng": d['x']} for d in data]
    if not cands: return [], gr.update(visible=False), "결과 없음"
    return cands, gr.update(choices=[x['label'] for x in cands], visible=True, value=None), f"{len(cands)}개 장소 찾음"

def update_home():
    with db_conn() as con: 
        rows = con.execute("SELECT id, title, photo_b64, start_iso, address FROM spaces ORDER BY created_at DESC").fetchall()
    if not rows: return "<div style='text-align:center;padding:50px;color:#aaa;'>현재 활성화된 공간이 없습니다.</div>"
    html = ""
    for r in rows:
        img = f"data:image/jpeg;base64,{r[2]}" if r[2] else ""
        html += f"<div class='card'><div style='display:flex;justify-content:space-between;gap:10px;'><div><b style='font-size:16px;'>{r[1]}</b><br><span style='color:#666;font-size:13px;'>{r[4]}</span><br><b style='color:var(--brand);font-size:13px;'>{r[3]}</b></div>"
        if img: html += f"<img src='{img}' class='thumb'>"
        html += f"</div><hr style='border:0;border-top:1px solid #eee;margin:10px 0;'><a href='/delete/{r[0]}' style='color:#ff4d4d;text-decoration:none;font-size:12px;'>내리기(삭제)</a></div>"
    return html

# =====================
# UI 설계
# =====================
with gr.Blocks(css=CSS, title="Oseyo") as demo:
    search_state = gr.State([])
    selected_json = gr.Textbox(visible=False, value="{}")

    with gr.Tab("탐색"):
        home_area = gr.HTML(update_home)
        refresh_btn = gr.Button("🔄 리스트 새로고침", size="sm")

    fab_btn = gr.Button("+", elem_id="fab-btn")
    overlay = gr.HTML("<div style='position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:10000;backdrop-filter:blur(2px);'></div>", visible=False)

    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal:
        gr.HTML("<div style='padding:18px;text-align:center;font-weight:900;font-size:18px;border-bottom:1px solid #eee;'>새로운 공간 만들기</div>")
        
        with gr.Column(elem_classes=["modal-body"]):
            act_in = gr.Textbox(label="📝 무엇을 하나요?", placeholder="활동 이름을 적어주세요")
            
            with gr.Row(elem_classes=["fav-grid"]):
                fav_btns = [gr.Button("", visible=False) for _ in range(10)]
            
            img_in = gr.Image(label="📸 현장 사진 (선택)", type="numpy", height=150)
            
            with gr.Row():
                st_in = gr.Textbox(label="📅 언제 시작하나요?", value=lambda: now_kst().strftime("%Y-%m-%dT%H:%M"))
                en_in = gr.Textbox(label="⏰ 언제 종료하나요?", value=lambda: (now_kst()+timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"))
            
            with gr.Row():
                unlim_check = gr.Checkbox(label="👥 인원 제한 없이 누구나", value=True)
                cap_slider = gr.Slider(label="최대 인원", minimum=1, maximum=10, value=4, step=1)
            
            with gr.Row():
                loc_in = gr.Textbox(label="📍 어디서 만나나요?", placeholder="장소명 검색 (예: 영일대 해수욕장)")
                loc_btn = gr.Button("🔍 장소 검색", variant="secondary")
            
            loc_sel = gr.Radio(label="아래에서 정확한 장소를 골라주세요", choices=[], visible=False)
            status = gr.Markdown("")

        with gr.Row(elem_classes=["modal-footer"]):
            close_btn = gr.Button("닫기")
            create_btn = gr.Button("✅ 공간 열기", variant="primary")

    # --- 인터랙션 ---
    def open_m():
        with db_conn() as con: 
            favs = [r[0] for r in con.execute("SELECT activity FROM favorites ORDER BY created_at DESC LIMIT 10").fetchall()]
        btns = [gr.update(visible=False, value="")] * 10
        for i, f in enumerate(favs): btns[i] = gr.update(visible=True, value=f)
        return [gr.update(visible=True)]*2 + btns

    fab_btn.click(open_m, None, [overlay, modal, *fav_btns])
    close_btn.click(lambda: [gr.update(visible=False)]*2, None, [overlay, modal])

    loc_btn.click(search_kakao, loc_in, [search_state, loc_sel, status])
    loc_sel.change(lambda c, l: next((json.dumps(x, ensure_ascii=False) for x in c if x['label']==l), "{}"), [search_state, loc_sel], selected_json)

    for b in fav_btns: b.click(lambda v: v, b, act_in)

    def save_sp(act, st, en, unlim, cap, img, loc_js):
        loc = json.loads(loc_js)
        if not act: return "⚠️ 활동명을 적어주세요.", update_home(), gr.update(visible=True)
        if 'lat' not in loc: return "⚠️ 장소를 검색하고 선택해 주세요.", update_home(), gr.update(visible=True)
        with db_conn() as con:
            con.execute("INSERT INTO spaces (id, title, photo_b64, start_iso, end_iso, address, lat, lng, capacity_enabled, capacity_max, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex[:8], act, image_to_b64(img), st, en, loc['place'], float(loc['lat']), float(loc['lng']), 0 if unlim else 1, cap, now_kst().isoformat()))
            con.execute("INSERT OR IGNORE INTO favorites (activity, created_at) VALUES (?,?)", (act, now_kst().isoformat()))
            con.commit()
        return "✅ 공간이 성공적으로 만들어졌습니다!", update_home(), gr.update(visible=False)

    create_btn.click(save_sp, [act_in, st_in, en_in, unlim_check, cap_slider, img_in, selected_json], [status, home_area, modal])
    refresh_btn.click(update_home, None, home_area)

# =====================
# 앱 실행
# =====================
app = FastAPI()
@app.get("/delete/{sid}")
def delete_sp(sid: str):
    with db_conn() as con: con.execute("DELETE FROM spaces WHERE id=?", (sid,)); con.commit()
    return RedirectResponse(url="/", status_code=302)

app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
