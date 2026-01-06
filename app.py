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
    # Render에서 Persistent Disk를 붙였다면 /var/data가 보통 쓰기 가능
    # 안 되면 /tmp로 폴백
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
/* 가로 스크롤 방지 */
html, body, .gradio-container {
  overflow-x: hidden !important;
  max-width: 100vw !important;
  margin: 0 !important;
}

/* 메인 영역: 웹에서 꽉 차게 */
.main-wrapper {
  height: 100vh;
  overflow-y: auto;
  overflow-x: hidden;
}

/* 플로팅 + 버튼 */
#fab {
  position: fixed !important;
  right: 25px;
  bottom: 35px;
  z-index: 1000;
}
#fab button {
  width: 65px !important;
  height: 65px !important;
  border-radius: 50% !important;
  background: #ff6b00 !important;
  color: white !important;
  font-size: 35px !important;
  box-shadow: 0 4px 15px rgba(0,0,0,0.3) !important;
  border: none !important;
}

/* 오버레이 */
#overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.7);
  z-index: 10000;
}

/* 메인 모달 */
.main-modal {
  position: fixed !important;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 92%;
  max-width: 480px;
  height: 85vh;
  background: white;
  z-index: 10001;
  border-radius: 24px;
  box-shadow: 0 20px 60px rgba(0,0,0,0.5);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 모달 바디: 여기만 세로 스크롤 */
.modal-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

/* 서브 모달 */
.sub-modal {
  position: fixed !important;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: 85%;
  max-width: 400px;
  height: 60vh;
  background: #fff;
  z-index: 10005;
  border-radius: 20px;
  border: 1px solid #ddd;
  box-shadow: 0 10px 40px rgba(0,0,0,0.4);
  overflow: hidden;
}
.sub-body {
  height: 100%;
  overflow-y: auto;
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

/* 2x5 즐겨찾기 */
.fav-grid {
  display: grid !important;
  grid-template-columns: 1fr 1fr !important;
  gap: 10px !important;
  margin-bottom: 5px;
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
        return "<p style='text-align:center; padding:40px; color:#999;'>등록된 이벤트가 없습니다.</p>"

    html_out = "<div style='padding:15px;'>"
    for title, photo, start, addr in rows:
        img_html = ""
        if photo:
            img_src = f"data:image/jpeg;base64,{photo}"
            img_html = (
                f"<img src='{img_src}' style='width:100%; height:150px; object-fit:cover; border-radius:12px;'>"
            )
        html_out += f"""
        <div style='background:#f9f9f9; border-radius:16px; margin-bottom:15px; padding:15px; border:1px solid #eee;'>
            {img_html}
            <div style='font-weight:700; font-size:18px; margin-top:10px;'>{html.escape(title or "")}</div>
            <div style='font-size:14px; color:#666;'>📅 {html.escape(start or "")}</div>
            <div style='font-size:14px; color:#666;'>📍 {html.escape(addr or "")}</div>
        </div>
        """
    return html_out + "</div>"

def save_data(title, img, start, end, addr_obj):
    title = (title or "").strip()
    if not title:
        return "제목 누락"

    if addr_obj is None:
        addr_obj = {}

    pic_b64 = ""
    if img is not None:
        try:
            im = Image.fromarray(img).convert("RGB")
            im.thumbnail((700, 700))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            pic_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception:
            pic_b64 = ""

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
        # 즐겨찾기 카운트는 "이벤트 제목"이 아니라 실제로는 "활동명"으로 쓰려면 title 대신 활동 입력값을 넣는 게 맞지만,
        # 지금 구조상 title로 유지
        con.execute(
            "INSERT INTO favs (name, count) VALUES (?, 1) "
            "ON CONFLICT(name) DO UPDATE SET count = count + 1",
            (title,),
        )
        con.commit()

    return "SUCCESS"


# -----------------------------
# 4) Gradio UI
# -----------------------------
now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
later_str = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")

with gr.Blocks(css=CSS, title="oseyo") as demo:
    search_state = gr.State([])     # 후보 리스트
    selected_addr = gr.State({})    # 선택된 주소 dict

    with gr.Column(elem_classes=["main-wrapper"]):
        with gr.Tabs():
            with gr.Tab("탐색"):
                explore_html = gr.HTML(get_list_html())
            with gr.Tab("지도"):
                gr.HTML('<iframe src="/map" style="width:100%;height:80vh;border:none;"></iframe>')

    fab = gr.Button("+", elem_id="fab")
    overlay = gr.HTML("<div id='overlay'></div>", visible=False)

    # 메인 모달
    with gr.Column(visible=False, elem_classes=["main-modal"]) as modal_m:
        gr.HTML("<div style='padding:20px 20px 0; font-weight:800; font-size:20px;'>새 이벤트</div>")
        with gr.Column(elem_classes=["modal-body"]):
            t_in = gr.Textbox(label="이벤트명", placeholder="입력하세요")

            gr.Markdown("⭐ **즐겨찾기 (2×5, 추가 가능)**")
            with gr.Column(elem_classes=["fav-grid"]):
                f_btns = [gr.Button("", visible=False) for _ in range(10)]

            img_in = gr.Image(label="사진", type="numpy")

            with gr.Row():
                s_in = gr.Textbox(label="시작", value=now_str)
                e_in = gr.Textbox(label="종료", value=later_str)

            addr_v = gr.Textbox(label="장소", interactive=False)
            addr_btn = gr.Button("📍 장소 검색")

            with gr.Row():
                m_close = gr.Button("닫기")
                m_save = gr.Button("생성", variant="primary")

    # 서브 모달(주소 검색)
    with gr.Column(visible=False, elem_classes=["sub-modal"]) as modal_s:
        with gr.Column(elem_classes=["sub-body"]):
            gr.Markdown("### 주소 검색")
            q_in = gr.Textbox(label="검색어", placeholder="강남역…")
            q_btn = gr.Button("찾기")
            q_res = gr.Radio(label="결과", choices=[])
            with gr.Row():
                s_close = gr.Button("뒤로")
                s_final = gr.Button("확정", variant="primary")

    # ------- 핸들러 -------
    def open_m():
        with db_conn() as con:
            rows = con.execute("SELECT name FROM favs ORDER BY count DESC LIMIT 10").fetchall()
        updates = [gr.update(visible=False, value="")] * 10
        for i, r in enumerate(rows):
            updates[i] = gr.update(visible=True, value=r[0])
        return [gr.update(visible=True), gr.update(visible=True)] + updates

    fab.click(open_m, None, [overlay, modal_m, *f_btns])

    def close_main():
        return [gr.update(visible=False), gr.update(visible=False)]
    m_close.click(close_main, None, [overlay, modal_m])

    # 즐겨찾기 버튼 클릭 시 제목 입력에 채워넣기
    for b in f_btns:
        b.click(lambda x: x, b, t_in)

    # 주소 검색 모달 열고/닫기
    addr_btn.click(lambda: gr.update(visible=True), None, modal_s)
    s_close.click(lambda: gr.update(visible=False), None, modal_s)

    def search_k(q):
        q = (q or "").strip()
        if not q:
            return [], gr.update(choices=[])

        if not KAKAO_REST_API_KEY:
            return [], gr.update(choices=["(서버 환경변수 KAKAO_REST_API_KEY 없음)"])

        headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
        res = requests.get(
            "https://dapi.kakao.com/v2/local/search/keyword.json",
            headers=headers,
            params={"query": q, "size": 6},
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

    q_btn.click(search_k, q_in, [search_state, q_res])

    def confirm_k(sel, cands):
        sel = sel or ""
        item = next((x for x in (cands or []) if x.get("label") == sel), None)
        if not item:
            return "", {}, gr.update(visible=False)
        return item["label"], item, gr.update(visible=False)

    s_final.click(confirm_k, [q_res, search_state], [addr_v, selected_addr, modal_s])

    def after_save(_):
        # 저장 후 목록 갱신 + 모달 닫기
        return get_list_html(), gr.update(visible=False), gr.update(visible=False)

    m_save.click(
        save_data,
        inputs=[t_in, img_in, s_in, e_in, selected_addr],
        outputs=None,
    ).then(
        after_save,
        inputs=None,
        outputs=[explore_html, overlay, modal_m],
    )


# -----------------------------
# 5) FastAPI + Kakao Map
# -----------------------------
app = FastAPI()

@app.get("/map")
def map_h():
    # 지도에 이미지까지 보여주기 위해 photo도 함께 가져옴
    with db_conn() as con:
        rows = con.execute("SELECT title, photo, lat, lng, addr, start FROM events ORDER BY created_at DESC").fetchall()

    # JS에서 쓰기 좋게 직렬화
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

    # 기본 센터(서울) - 실제로는 첫 이벤트가 있으면 거기로 센터 맞춤
    center_lat, center_lng = 37.56, 126.97
    if payload and payload[0]["lat"] and payload[0]["lng"]:
        center_lat, center_lng = payload[0]["lat"], payload[0]["lng"]

    if not KAKAO_JAVASCRIPT_KEY:
        return HTMLResponse("<div style='padding:24px;'>KAKAO_JAVASCRIPT_KEY 환경변수가 없어 지도 로딩이 안 됨</div>")

    return HTMLResponse(f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    html, body, #m {{ width:100%; height:100%; margin:0; padding:0; }}
    .iw-wrap {{ width:240px; }}
    .iw-title {{ font-weight:800; font-size:14px; margin:0 0 6px 0; }}
    .iw-meta {{ font-size:12px; color:#666; margin:2px 0; }}
    .iw-img {{ width:100%; height:120px; object-fit:cover; border-radius:10px; margin:6px 0; }}
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

      const safeTitle = (ev.title || "").replace(/</g,"&lt;").replace(/>/g,"&gt;");
      const safeAddr = (ev.addr || "").replace(/</g,"&lt;").replace(/>/g,"&gt;");
      const safeStart = (ev.start || "").replace(/</g,"&lt;").replace(/>/g,"&gt;");

      let imgHtml = "";
      if (ev.photo) {{
        imgHtml = `<img class="iw-img" src="data:image/jpeg;base64,${{ev.photo}}" />`;
      }}

      const content = `
        <div class="iw-wrap">
          <div class="iw-title">${{safeTitle}}</div>
          ${{imgHtml}}
          <div class="iw-meta">📅 ${{safeStart}}</div>
          <div class="iw-meta">📍 ${{safeAddr}}</div>
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

# Gradio 마운트
app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
