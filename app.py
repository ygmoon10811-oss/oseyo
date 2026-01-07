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
# 1) 환경/DB
# -----------------------------
def pick_db_path():
    candidates = ["/var/data", "/tmp"]
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            test = os.path.join(d, ".writetest")
            with open(test, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test)
            return os.path.join(d, "oseyo_final.db")
        except Exception:
            continue
    return "/tmp/oseyo_final.db"

DB_PATH = pick_db_path()
print(f"[DB] Using: {DB_PATH}")

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
# 2) CSS
# -----------------------------
CSS = """
html, body { margin: 0 !important; padding: 0 !important; overflow-x: hidden !important; }
.gradio-container { max-width: 100% !important; padding-bottom: 100px !important; }

/* FAB 버튼 - 작은 원형 */
.fab-wrapper { position: fixed !important; right: 20px; bottom: 20px; z-index: 999; width: 0; height: 0; }
.fab-wrapper button {
  width: 56px !important; height: 56px !important;
  min-width: 56px !important; min-height: 56px !important;
  border-radius: 50% !important;
  background: #ff6b00 !important;
  color: white !important;
  font-size: 32px !important;
  border: none !important;
  box-shadow: 0 4px 12px rgba(0,0,0,0.3) !important;
  padding: 0 !important;
  line-height: 56px !important;
  cursor: pointer !important;
}

/* 오버레이 */
.overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 10000; }

/* 메인 모달 */
.main-modal {
  position: fixed !important; top: 50%; left: 50%; transform: translate(-50%, -50%);
  width: 92vw; max-width: 500px; height: 86vh;
  background: white; z-index: 10001; border-radius: 24px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.4);
  display: flex; flex-direction: column; overflow: hidden;
}
.modal-header { 
  padding: 20px; border-bottom: 2px solid #eee; 
  font-weight: 800; font-size: 20px; 
  flex-shrink: 0;
}
.modal-body {
  flex: 1; 
  overflow-y: auto; 
  overflow-x: hidden;
  padding: 20px;
  display: flex; 
  flex-direction: column; 
  gap: 16px;
}
.modal-body::-webkit-scrollbar { width: 8px; }
.modal-body::-webkit-scrollbar-track { background: #f1f1f1; }
.modal-body::-webkit-scrollbar-thumb { background: #ccc; border-radius: 4px; }
.modal-body::-webkit-scrollbar-thumb:hover { background: #999; }

.modal-footer {
  padding: 16px 20px; 
  border-top: 2px solid #eee; 
  background: #f9f9f9;
  display: flex; 
  gap: 10px;
  flex-shrink: 0;
}
.modal-footer button { flex: 1; padding: 12px; border-radius: 12px; font-weight: 700; }

/* 서브 모달 */
.sub-modal {
  position: fixed !important; top: 50%; left: 50%; transform: translate(-50%, -50%);
  width: 88vw; max-width: 420px; max-height: 70vh;
  background: white; z-index: 10005; border-radius: 20px;
  box-shadow: 0 12px 40px rgba(0,0,0,0.4);
  overflow: hidden;
}
.sub-body { height: 100%; overflow-y: auto; padding: 20px; }

/* 즐겨찾기 그리드 */
.fav-grid { 
  display: grid; 
  grid-template-columns: 1fr 1fr; 
  gap: 8px; 
  max-height: none;
  overflow: visible;
}
.fav-grid button { 
  font-size: 13px; 
  padding: 10px 8px; 
  border-radius: 10px; 
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* 이벤트 카드 */
.event-card {
  background: #f9f9f9; border-radius: 16px; padding: 16px;
  margin-bottom: 14px; border: 1px solid #e5e5e5;
  display: grid; grid-template-columns: 1fr 120px; gap: 14px; align-items: center;
}
.event-info { display: flex; flex-direction: column; gap: 6px; }
.event-title { font-weight: 800; font-size: 18px; color: #111; }
.event-meta { font-size: 13px; color: #666; }
.event-photo {
  width: 120px; height: 120px; object-fit: cover;
  border-radius: 12px; border: 1px solid #ddd;
}
"""

# -----------------------------
# 3) 로직
# -----------------------------
def get_list_html():
    with db_conn() as con:
        rows = con.execute(
            "SELECT title, photo, start, addr FROM events ORDER BY created_at DESC"
        ).fetchall()

    if not rows:
        return "<div style='text-align:center; padding:60px; color:#999;'>등록된 이벤트가 없습니다.</div>"

    html_out = "<div style='padding:16px;'>"
    for title, photo, start, addr in rows:
        img_html = ""
        if photo:
            img_html = f"<img class='event-photo' src='data:image/jpeg;base64,{photo}' />"
        else:
            img_html = "<div class='event-photo' style='background:#e0e0e0;'></div>"
        
        html_out += f"""
        <div class='event-card'>
            <div class='event-info'>
                <div class='event-title'>{html.escape(title or "")}</div>
                <div class='event-meta'>📅 {html.escape(start or "")}</div>
                <div class='event-meta'>📍 {html.escape(addr or "")}</div>
            </div>
            {img_html}
        </div>
        """
    return html_out + "</div>"

def save_data(title, img, start, end, addr_obj):
    title = (title or "").strip()
    if not title:
        return "제목을 입력해 주세요"

    if addr_obj is None:
        addr_obj = {}

    pic_b64 = ""
    if img is not None:
        try:
            im = Image.fromarray(img).convert("RGB")
            im.thumbnail((600, 600))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            pic_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception:
            pass

    addr_name = (addr_obj.get("name") or "").strip()
    lat = addr_obj.get("y") or 0
    lng = addr_obj.get("x") or 0
    try:
        lat = float(lat)
        lng = float(lng)
    except Exception:
        lat, lng = 0.0, 0.0

    with db_conn() as con:
        con.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex[:8],
                title,
                pic_b64,
                start or "",
                end or "",
                addr_name,
                lat,
                lng,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        con.execute(
            "INSERT INTO favs (name, count) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET count = count + 1",
            (title,),
        )
        con.commit()

    print(f"[SAVE] Event created: {title}")
    return "✅ 이벤트가 생성되었습니다"


# -----------------------------
# 4) Gradio UI
# -----------------------------
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

    # FAB
    with gr.Row(elem_classes=["fab-wrapper"]):
        fab = gr.Button("+")
    
    overlay = gr.HTML("<div class='overlay'></div>", visible=False)

    # 메인 모달
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_m:
        gr.HTML("<div class='modal-header'>새 이벤트 만들기</div>")
        
        with gr.Column(elem_classes=["modal-body"]):
            with gr.Row():
                t_in = gr.Textbox(label="📝 이벤트명", placeholder="예: 산책, 커피", scale=3)
                add_fav_btn = gr.Button("⭐", scale=1, size="sm")
            
            fav_msg = gr.Markdown("")
            
            gr.Markdown("**⭐ 즐겨찾기 (최근 사용 순)**")
            with gr.Column(elem_classes=["fav-grid"]):
                f_btns = [gr.Button("", visible=False, size="sm") for _ in range(10)]

            img_in = gr.Image(label="📸 사진 (선택)", type="numpy", height=150)

            with gr.Row():
                s_in = gr.Textbox(label="📅 시작일시", value=now_dt.strftime("%Y-%m-%d %H:%M"), placeholder="YYYY-MM-DD HH:MM")
                e_in = gr.Textbox(label="⏰ 종료일시", value=later_dt.strftime("%Y-%m-%d %H:%M"), placeholder="YYYY-MM-DD HH:MM")

            addr_v = gr.Textbox(label="📍 장소", interactive=False, value="")
            addr_btn = gr.Button("🔍 장소 검색하기")
            
            msg_out = gr.Markdown("")
        
        with gr.Row(elem_classes=["modal-footer"]):
            m_close = gr.Button("취소", variant="secondary")
            m_save = gr.Button("✅ 생성", variant="primary")

    # 서브 모달 (주소 검색)
    with gr.Column(visible=False, elem_classes=["sub-modal"]) as modal_s:
        with gr.Column(elem_classes=["sub-body"]):
            gr.Markdown("### 📍 장소 검색")
            q_in = gr.Textbox(label="검색어", placeholder="예: 포항시청, 영일대")
            q_btn = gr.Button("검색")
            q_res = gr.Radio(label="결과 (클릭하면 선택)", choices=[], interactive=True)
            with gr.Row():
                s_close = gr.Button("뒤로", variant="secondary")
                s_final = gr.Button("✅ 확정", variant="primary")

    # ------- 핸들러 -------
    
    # 새로고침
    refresh_btn.click(fn=get_list_html, outputs=explore_html)
    
    # FAB 클릭
    def open_m():
        with db_conn() as con:
            rows = con.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 10").fetchall()
        updates = [gr.update(visible=False, value="")] * 10
        for i, r in enumerate(rows):
            updates[i] = gr.update(visible=True, value=r[0])
        return [gr.update(visible=True), gr.update(visible=True)] + updates

    fab.click(open_m, None, [overlay, modal_m, *f_btns])

    # 모달 닫기
    def close_main():
        return gr.update(visible=False), gr.update(visible=False)
    
    m_close.click(close_main, None, [overlay, modal_m])

    # 즐겨찾기 추가
    def add_fav(title):
        title = (title or "").strip()
        if not title:
            msg = "⚠️ 이벤트명을 입력해 주세요"
            updates = [gr.update()] * 10
            return [msg] + updates
        
        with db_conn() as con:
            con.execute(
                "INSERT INTO favs (name, count) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET count = count + 1",
                (title,)
            )
            con.commit()
            rows = con.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 10").fetchall()
        
        updates = [gr.update(visible=False, value="")] * 10
        for i, r in enumerate(rows):
            updates[i] = gr.update(visible=True, value=r[0])
        
        msg = f"✅ '{title}'를 즐겨찾기에 추가했습니다"
        return [msg] + updates

    add_fav_btn.click(add_fav, t_in, [fav_msg] + f_btns)

    # 즐겨찾기 버튼 클릭
    for b in f_btns:
        b.click(lambda x: x, b, t_in)

    # 주소 검색 모달
    addr_btn.click(lambda: gr.update(visible=True), None, modal_s)
    s_close.click(lambda: gr.update(visible=False), None, modal_s)

    def search_k(q):
        q = (q or "").strip()
        if not q:
            return [], gr.update(choices=[])

        if not KAKAO_REST_API_KEY:
            return [], gr.update(choices=["⚠️ KAKAO_REST_API_KEY 환경변수 필요"])

        try:
            headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
            res = requests.get(
                "https://dapi.kakao.com/v2/local/search/keyword.json",
                headers=headers,
                params={"query": q, "size": 8},
                timeout=10,
            )
            data = res.json()
            docs = data.get("documents", []) or []
            cands = []
            for d in docs:
                label = f"{d.get('place_name','')} | {d.get('address_name','')}"
                cands.append({
                    "label": label,
                    "name": d.get("place_name", ""),
                    "y": d.get("y", 0),
                    "x": d.get("x", 0),
                })
            return cands, gr.update(choices=[x["label"] for x in cands], value=None)
        except Exception as e:
            return [], gr.update(choices=[f"⚠️ 검색 오류: {str(e)}"])

    q_btn.click(search_k, q_in, [search_state, q_res])

    def confirm_k(sel, cands):
        if not sel or not cands:
            return "", {}, gr.update(visible=False)
        item = next((x for x in cands if x.get("label") == sel), None)
        if not item:
            return "", {}, gr.update(visible=False)
        return item["label"], item, gr.update(visible=False)

    s_final.click(confirm_k, [q_res, search_state], [addr_v, selected_addr, modal_s])

    # 저장
    def save_and_close(title, img, start, end, addr):
        msg = save_data(title, img, start, end, addr)
        html = get_list_html()
        return msg, html, gr.update(visible=False), gr.update(visible=False)

    m_save.click(
        save_and_close,
        [t_in, img_in, s_in, e_in, selected_addr],
        [msg_out, explore_html, overlay, modal_m]
    )


# -----------------------------
# 5) FastAPI + Kakao Map
# -----------------------------
app = FastAPI()

@app.get("/map")
def map_h():
    with db_conn() as con:
        rows = con.execute("SELECT title, photo, lat, lng, addr, start FROM events ORDER BY created_at DESC").fetchall()

    payload = []
    for title, photo, lat, lng, addr, start in rows:
        try:
            lat = float(lat) if lat is not None else 0.0
            lng = float(lng) if lng is not None else 0.0
        except Exception:
            lat, lng = 0.0, 0.0
        payload.append({
            "title": title or "",
            "photo": photo or "",
            "lat": lat,
            "lng": lng,
            "addr": addr or "",
            "start": start or "",
        })

    center_lat, center_lng = 37.56, 126.97
    if payload and payload[0]["lat"] and payload[0]["lng"]:
        center_lat, center_lng = payload[0]["lat"], payload[0]["lng"]

    if not KAKAO_JAVASCRIPT_KEY:
        return HTMLResponse("<div style='padding:24px;'>⚠️ KAKAO_JAVASCRIPT_KEY 환경변수 필요</div>")

    return HTMLResponse(f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    html, body, #m {{ width:100%; height:100%; margin:0; padding:0; }}
    .iw-wrap {{ width:240px; padding:12px; }}
    .iw-title {{ font-weight:800; font-size:14px; margin:0 0 8px 0; }}
    .iw-meta {{ font-size:12px; color:#666; margin:4px 0; }}
    .iw-img {{ width:100%; height:120px; object-fit:cover; border-radius:10px; margin:8px 0; }}
  </style>
</head>
<body>
  <div id="m"></div>
  <script src="//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}"></script>
  <script>
    const data = {json.dumps(payload)};
    const map = new kakao.maps.Map(document.getElementById('m'), {{
      center: new kakao.maps.LatLng({center_lat}, {center_lng}),
      level: 7
    }});

    let openInfo = null;

    data.forEach(ev => {{
      if (!ev.lat || !ev.lng) return;

      const marker = new kakao.maps.Marker({{
        map: map,
        position: new kakao.maps.LatLng(ev.lat, ev.lng),
        title: ev.title
      }});

      let imgHtml = "";
      if (ev.photo) {{
        imgHtml = `<img class="iw-img" src="data:image/jpeg;base64,${{ev.photo}}" />`;
      }}

      const content = `
        <div class="iw-wrap">
          <div class="iw-title">${{ev.title}}</div>
          ${{imgHtml}}
          <div class="iw-meta">📅 ${{ev.start}}</div>
          <div class="iw-meta">📍 ${{ev.addr}}</div>
        </div>
      `;

      const infowindow = new kakao.maps.InfoWindow({{
        content: content,
        removable: true
      }});

      kakao.maps.event.addListener(marker, 'click', function() {{
        if (openInfo) openInfo.close();
        infowindow.open(map, marker);
        openInfo = infowindow;
      }});
    }});
  </script>
</body>
</html>
    """)

app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
