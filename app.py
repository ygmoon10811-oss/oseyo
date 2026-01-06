import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image
import gradio as gr

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

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
            address_detail TEXT DEFAULT '',
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            capacity_enabled INTEGER NOT NULL DEFAULT 0,
            capacity_max INTEGER,
            hidden INTEGER NOT NULL DEFAULT 0,
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

def db_insert_space(space: dict):
    with db_conn() as con:
        con.execute("""
        INSERT INTO spaces (
            id, title, photo_b64, start_iso, end_iso,
            address, address_detail, lat, lng,
            capacity_enabled, capacity_max, hidden, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            space["id"],
            space["title"],
            space.get("photo_b64",""),
            space["start_iso"],
            space["end_iso"],
            space["address"],
            space.get("address_detail",""),
            float(space["lat"]),
            float(space["lng"]),
            1 if space.get("capacityEnabled") else 0,
            space.get("capacityMax"),
            0,
            now_kst().isoformat(),
        ))
        con.commit()

def db_delete_space(space_id: str):
    with db_conn() as con:
        con.execute("DELETE FROM spaces WHERE id=?", (space_id,))
        con.commit()

def db_list_spaces():
    with db_conn() as con:
        rows = con.execute("""
            SELECT id, title, photo_b64, start_iso, end_iso,
                   address, address_detail, lat, lng,
                   capacity_enabled, capacity_max, hidden, created_at
            FROM spaces
            ORDER BY created_at DESC
        """).fetchall()

    out=[]
    for r in rows:
        out.append({
            "id": r[0],
            "title": r[1],
            "photo_b64": r[2] or "",
            "start_iso": r[3] or "",
            "end_iso": r[4] or "",
            "address": r[5] or "",
            "address_detail": r[6] or "",
            "lat": float(r[7]) if r[7] is not None else None,
            "lng": float(r[8]) if r[8] is not None else None,
            "capacityEnabled": bool(r[9]),
            "capacityMax": r[10],
            "hidden": bool(r[11]),
            "created_at": r[12] or "",
        })
    return out

def active_spaces():
    spaces = db_list_spaces()
    t = now_kst()
    out=[]
    for s in spaces:
        if s.get("hidden"):
            continue
        try:
            st = datetime.fromisoformat(s["start_iso"])
            en = datetime.fromisoformat(s["end_iso"])
            if st.tzinfo is None: st = st.replace(tzinfo=KST)
            if en.tzinfo is None: en = en.replace(tzinfo=KST)
            if st <= t <= en:
                out.append(s)
        except:
            pass
    return out

def db_list_favorites():
    with db_conn() as con:
        rows = con.execute("SELECT activity FROM favorites ORDER BY created_at DESC").fetchall()
    return [r[0] for r in rows if r and r[0]]

def db_add_favorite(activity: str):
    activity = (activity or "").strip()
    if not activity:
        return
    with db_conn() as con:
        con.execute(
            "INSERT OR IGNORE INTO favorites (activity, created_at) VALUES (?, ?)",
            (activity, now_kst().isoformat())
        )
        con.commit()

# =====================
# 유틸
# =====================
def image_np_to_b64(img_np):
    if img_np is None:
        return ""
    try:
        im = Image.fromarray(img_np.astype("uint8"))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except:
        return ""

def b64_to_data_uri(b64_str):
    return f"data:image/jpeg;base64,{b64_str}" if b64_str else ""

def fmt_period(st_iso: str, en_iso: str) -> str:
    try:
        st = datetime.fromisoformat(st_iso)
        en = datetime.fromisoformat(en_iso)
        if st.tzinfo is None: st = st.replace(tzinfo=KST)
        if en.tzinfo is None: en = en.replace(tzinfo=KST)
        if st.date() == en.date():
            return f"{st:%m/%d} {st:%H:%M}–{en:%H:%M}"
        return f"{st:%m/%d %H:%M}–{en:%m/%d %H:%M}"
    except:
        return "-"

# =====================
# 카카오 장소 검색
# =====================
def kakao_keyword_search(q: str, size=10):
    q = (q or "").strip()
    if not q:
        return [], "주소를 입력해 주세요"
    if not KAKAO_REST_API_KEY:
        return [], "⚠️ KAKAO_REST_API_KEY 환경변수가 필요합니다"

    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params = {"query": q, "size": size}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=10)
        if r.status_code == 429:
            return [], "⚠️ 검색 제한. 잠시 후 다시 시도해 주세요"
        if r.status_code >= 400:
            return [], f"⚠️ 검색 실패 (HTTP {r.status_code})"
        data = r.json()
    except Exception as e:
        return [], f"⚠️ 네트워크 오류: {str(e)}"

    cands=[]
    for d in (data.get("documents") or []):
        place = (d.get("place_name") or "").strip()
        road = (d.get("road_address_name") or "").strip()
        addr = (d.get("address_name") or "").strip()
        lat = d.get("y")
        lng = d.get("x")
        if not place or lat is None or lng is None:
            continue
        best_addr = road or addr
        label = f"{place} — {best_addr}" if best_addr else place
        try:
            cands.append({
                "label": label,
                "place": place,
                "lat": float(lat),
                "lng": float(lng)
            })
        except:
            pass

    if not cands:
        return [], "⚠️ 검색 결과가 없습니다"
    return cands, ""

# =====================
# 날짜/시간 파싱
# =====================
def parse_dt_any(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    s = s.replace("/", "-")
    if " " in s and "T" not in s:
        s = s.replace(" ", "T")
    if len(s) == 16:
        s = s + ":00"
    try:
        dt = datetime.fromisoformat(s)
    except:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    else:
        dt = dt.astimezone(KST)
    return dt

# =====================
# 홈/지도
# =====================
def render_home():
    items = active_spaces()

    persistent = os.path.isdir("/var/data")
    banner = (
        f"<div class='banner ok'>✅ 영구저장 모드</div>"
        if persistent else
        f"<div class='banner warn'>⚠️ 임시저장 모드</div>"
    )

    if not items:
        return banner + """
        <div class="card empty">
          <div class="h">아직 열린 공간이 없습니다</div>
          <div class="p">오른쪽 아래 + 버튼으로 공간을 열어보세요</div>
        </div>
        """

    out = [banner]
    for s in items:
        period = fmt_period(s["start_iso"], s["end_iso"])
        cap = f"최대 {s['capacityMax']}명" if s.get("capacityEnabled") else "제한 없음"
        photo_uri = b64_to_data_uri(s.get("photo_b64", ""))
        img = f"<img class='thumb' src='{photo_uri}' />" if photo_uri else "<div class='thumb placeholder'></div>"

        out.append(f"""
        <div class="card">
          <div class="rowcard">
            <div class="left">
              <div class="title">{s['title']}</div>
              <div class="period">{period}</div>
              <div class='muted'>{s['address']}</div>
              <div class='muted'>{cap}</div>
              <div class="idline">ID: {s['id']}</div>
            </div>
            <div class="right">{img}</div>
          </div>
          <a class="btn-del" href="/delete/{s['id']}">삭제</a>
        </div>
        """)
    return "\n".join(out)

def map_points_payload():
    items = active_spaces()
    points = []
    for s in items:
        points.append({
            "title": s["title"],
            "lat": s["lat"],
            "lng": s["lng"],
            "addr": s.get("address",""),
            "period": fmt_period(s.get("start_iso",""), s.get("end_iso","")),
            "id": s["id"],
            "photo_b64": s.get("photo_b64","") or ""
        })
    return points

def draw_map():
    ts = int(now_kst().timestamp())
    return f"""
    <div class="mapWrap">
      <iframe class="mapFrame" src="/kakao_map?ts={ts}" loading="lazy"></iframe>
    </div>
    """

# =====================
# 이벤트 생성
# =====================
def create_event(activity_text, start_txt, end_txt, capacity_unlimited, cap_max, photo_np, selected_place_json):
    act = (activity_text or "").strip()
    if not act:
        return "⚠️ 활동명을 입력해 주세요", render_home(), draw_map()

    try:
        place_data = json.loads(selected_place_json) if selected_place_json else None
    except:
        place_data = None
    if not place_data:
        return "⚠️ 장소를 검색하고 선택해 주세요", render_home(), draw_map()

    st = parse_dt_any(start_txt)
    en = parse_dt_any(end_txt)
    if st is None:
        return "⚠️ 시작 일시를 선택/입력해 주세요", render_home(), draw_map()
    if en is None:
        return "⚠️ 종료 일시를 선택/입력해 주세요", render_home(), draw_map()
    if en <= st:
        return "⚠️ 종료 일시는 시작 일시보다 뒤여야 합니다", render_home(), draw_map()

    capacityEnabled = not bool(capacity_unlimited)
    cap_max_val = None
    if capacityEnabled:
        try:
            cap_max_val = int(cap_max)
            cap_max_val = max(1, min(cap_max_val, 10))
        except:
            cap_max_val = 4

    photo_b64 = image_np_to_b64(photo_np)
    title = act if len(act) <= 30 else act[:30] + "…"
    new_id = uuid.uuid4().hex[:8]

    try:
        db_insert_space({
            "id": new_id,
            "title": title,
            "photo_b64": photo_b64,
            "start_iso": st.isoformat(),
            "end_iso": en.isoformat(),
            "address": place_data.get("place", ""),
            "address_detail": "",
            "lat": float(place_data["lat"]),
            "lng": float(place_data["lng"]),
            "capacityEnabled": capacityEnabled,
            "capacityMax": cap_max_val,
        })
        return f"✅ '{title}' 이벤트가 생성되었습니다!", render_home(), draw_map()
    except Exception as e:
        return f"⚠️ 저장 실패: {str(e)}", render_home(), draw_map()

# =====================
# ✅ CSS: 가로스크롤/눌림/가림 전부 차단
# =====================
# =====================
# ✅ CSS: 가로스크롤/눌림/가림 방지 최적화
# =====================
CSS = """
:root{--bg:#FAF9F6;--ink:#1F2937;--muted:#6B7280;--line:#E5E3DD;--card:#ffffffcc;--danger:#ef4444;}

/* 전체 레이아웃 강제 고정 */
*{box-sizing:border-box!important;}
html, body {
    width: 100%;
    overflow-x: hidden !important;
    margin: 0; padding: 0;
}

/* Gradio 컨테이너 가로폭 제한 */
.gradio-container {
    max-width: 100vw !important;
    overflow-x: hidden !important;
}

/* 모달 오버레이 */
.modal-overlay {
    position: fixed !important;
    inset: 0 !important;
    background: rgba(0,0,0,0.6) !important;
    z-index: 10000 !important;
    backdrop-filter: blur(4px) !important;
}

/* ✅ 모달 시트: 세로 스크롤만 허용, 가로 스크롤 원천 봉쇄 */
.modal-sheet {
    position: fixed !important;
    left: 50% !important;
    top: 50% !important;
    transform: translate(-50%, -50%) !important;
    width: min(520px, 94vw) !important;
    max-height: 90vh !important;
    background: #fff !important;
    border-radius: 24px !important;
    padding: 0 !important; /* 패딩을 내부 컨테이너로 위임 */
    z-index: 10001 !important;
    box-shadow: 0 25px 50px -12px rgba(0,0,0,0.25) !important;
    display: flex !important;
    flex-direction: column !important;
    overflow: hidden !important; /* 내부에서 스크롤 처리 */
}

/* 모달 내부 스크롤 영역 */
.modal-sheet > .form, 
.modal-sheet > .contain, 
.modal-sheet > .gradio-column {
    overflow-y: auto !important;
    overflow-x: hidden !important;
    padding: 20px !important;
    width: 100% !important;
    flex: 1;
}

/* ✅ 모든 행(Row) 요소 가로 배치 강제 해제 (가로 삐져나옴 방지) */
.modal-sheet .gr-row, 
.modal-sheet .row {
    display: flex !important;
    flex-direction: column !important; /* 모바일/좁은화면 대응을 위해 기본 수직 배치 */
    width: 100% !important;
    gap: 12px !important;
}

/* 가로로 나란히 있어야 할 버튼/슬라이더 등만 예외 처리 */
@media (min-width: 400px) {
    .modal-sheet .modal-footer { flex-direction: row !important; }
}

/* 즐겨찾기 그리드 (2열 유지) */
.fav-grid {
    display: grid !important;
    grid-template-columns: 1fr 1fr !important;
    gap: 8px !important;
    width: 100% !important;
}

/* 이미지 컴포넌트 최적화 */
.modal-sheet .gradio-image {
    width: 100% !important;
    min-height: 180px !important;
}
.modal-sheet .gradio-image img {
    object-fit: cover !important;
    border-radius: 12px !important;
}

/* 입력창 너비 고정 */
.modal-sheet input, 
.modal-sheet textarea {
    width: 100% !important;
}

/* 푸터 하단 고정 */
.modal-footer {
    padding: 16px !important;
    background: #f9fafb !important;
    border-top: 1px solid var(--line) !important;
    display: flex !important;
    gap: 10px !important;
}

/* 불필요한 스크롤바 숨기기 */
.modal-sheet::-webkit-scrollbar { width: 6px; }
.modal-sheet::-webkit-scrollbar-thumb { background: #d1d5db; border-radius: 10px; }

/* 기존 카드 스타일 유지 */
.card { background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 14px; margin: 12px auto; max-width: 760px; }
.rowcard { display: flex; gap: 18px; justify-content: space-between; }
.thumb { width: 120px; height: 120px; object-fit: cover; border-radius: 14px; flex-shrink: 0; }
@media (max-width: 600px) {
    .rowcard { flex-direction: column-reverse; }
    .thumb { width: 100%; height: 180px; }
}
"""
/* =========================
   ✅ 모달: 스크롤은 모달만!
   ========================= */
.modal-sheet{
  overflow-y:auto!important;
  overflow-x:hidden!important;
}

/* ✅ 모달 내부 컴포넌트들이 "자체 스크롤" 만들지 못하게 전부 풀어버림 */
.modal-sheet .gr-block,
.modal-sheet .gr-row,
.modal-sheet .gr-form,
.modal-sheet .wrap,
.modal-sheet .contain,
.modal-sheet .container,
.modal-sheet .padded,
.modal-sheet .form,
.modal-sheet .panel,
.modal-sheet .scroll-hide{
  overflow:visible!important;
  max-height:none!important;
}

/* ✅ 드롭다운/라디오/체크박스: 옵션 영역 자체 스크롤 금지 */
.modal-sheet .gradio-dropdown,
.modal-sheet .gradio-dropdown .wrap,
.modal-sheet .gradio-dropdown .options,
.modal-sheet .gradio-dropdown .options ul,
.modal-sheet .gradio-radio,
.modal-sheet .gradio-radio .wrap,
.modal-sheet .gradio-radio fieldset,
.modal-sheet .gradio-checkbox,
.modal-sheet .gradio-checkbox .wrap,
.modal-sheet .gradio-checkbox fieldset{
  overflow:visible!important;
  max-height:none!important;
}

/* ✅ 이미지 업로드 영역: 내부 스크롤 금지 + 높이 보장 */
.modal-sheet .gradio-image,
.modal-sheet .gradio-image .wrap,
.modal-sheet .gradio-image .container,
.modal-sheet .gradio-image .image-container,
.modal-sheet .gradio-image .upload-container{
  overflow:visible!important;
  max-height:none!important;
  min-height:170px!important;
}

/* ✅ 슬라이더/넘버 인풋 오른쪽에 스크롤 생기는 케이스 방지 */
.modal-sheet .gradio-slider,
.modal-sheet .gradio-slider .wrap,
.modal-sheet .gradio-number,
.modal-sheet .gradio-number .wrap{
  overflow:visible!important;
  max-height:none!important;
}

/* ✅ 혹시 남는 스크롤바(webkit) 강제 제거: 모달 내부는 숨김 */
.modal-sheet *::-webkit-scrollbar{
  width:0!important;
  height:0!important;
}}
"""

# ✅ 캘린더(클릭) 살리기: textbox input을 datetime-local로 강제
JS_BOOT = """
function apply(){
  const a = document.getElementById("start_dt_box");
  const b = document.getElementById("end_dt_box");
  if(a){
    const i=a.querySelector("input");
    if(i){ i.type="datetime-local"; i.step="60"; i.style.width="100%"; }
  }
  if(b){
    const i=b.querySelector("input");
    if(i){ i.type="datetime-local"; i.step="60"; i.style.width="100%"; }
  }
}
apply();
setTimeout(apply, 250);
setTimeout(apply, 900);
setTimeout(apply, 1800);
"""

# =====================
# UI
# =====================
with gr.Blocks(css=CSS, title="Oseyo") as demo:
    search_results_state = gr.State([])
    selected_place_state = gr.Textbox(visible=False, value="{}")

    gr.Markdown("# 지금, 열려 있습니다\n원하시면 오세요")

    with gr.Tabs():
        with gr.Tab("탐색"):
            home_html = gr.HTML()
            refresh_btn = gr.Button("🔄 새로고침", size="sm")
        with gr.Tab("지도"):
            map_html = gr.HTML()
            map_refresh_btn = gr.Button("🔄 지도 새로고침", size="sm")

    fab_btn = gr.Button("+", elem_id="fab-btn")

    modal_overlay = gr.HTML("<div></div>", visible=False, elem_classes=["modal-overlay"])

    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal_sheet:
        with gr.Row(elem_classes=["modal-header"]):
            gr.HTML("<div class='modal-title'>새 공간 열기</div>")
            close_btn = gr.Button("✕", size="sm")

        with gr.Row():
            activity_text = gr.Textbox(label="📝 활동명", placeholder="예: 산책, 커피, 스터디…", scale=4)
            add_fav_btn = gr.Button("⭐", size="sm", scale=1)

        fav_msg = gr.Markdown("")

        gr.Markdown("⭐ 즐겨찾기 (2×5)")
        with gr.Column(elem_classes=["fav-grid"]):
            fav_buttons = [gr.Button("", visible=False, elem_classes=["fav-chip"]) for _ in range(10)]

        photo_np = gr.Image(label="📸 사진", type="numpy", height=170)

        start_txt = gr.Textbox(label="📅 시작 일시", elem_id="start_dt_box", placeholder="YYYY-MM-DDTHH:MM")
        end_txt   = gr.Textbox(label="⏰ 종료 일시", elem_id="end_dt_box", placeholder="YYYY-MM-DDTHH:MM")

        with gr.Row():
            capacity_unlimited = gr.Checkbox(label="👥 제한없음", value=True, scale=1)
            cap_max = gr.Slider(label="최대인원", minimum=1, maximum=10, value=4, step=1, scale=2)

        with gr.Row():
            place_query = gr.Textbox(label="📍 장소", placeholder="예: 포항시청, 영일대", scale=4)
            search_btn = gr.Button("🔍", scale=1, size="sm")

        search_msg = gr.Markdown("")
        place_results = gr.Radio(label="검색 결과 (클릭하면 선택됨)", choices=[], value=None, visible=True)

        msg_output = gr.Markdown("")

        with gr.Row(elem_classes=["modal-footer"]):
            cancel_btn = gr.Button("취소", variant="secondary")
            create_btn = gr.Button("✅ 생성", variant="primary")

    demo.load(fn=render_home, outputs=home_html, js=JS_BOOT)
    demo.load(fn=draw_map, outputs=map_html)

    refresh_btn.click(fn=render_home, outputs=home_html)
    map_refresh_btn.click(fn=draw_map, outputs=map_html)

    def build_fav_updates(favs):
        favs = favs[:10]
        ups = []
        for i in range(10):
            if i < len(favs):
                ups.append(gr.update(value=favs[i], visible=True))
            else:
                ups.append(gr.update(value="", visible=False))
        return ups

    def open_modal_and_load():
        st = now_kst().replace(second=0, microsecond=0)
        en = st + timedelta(hours=2)
        st_s = st.strftime("%Y-%m-%dT%H:%M")
        en_s = en.strftime("%Y-%m-%dT%H:%M")

        favs = db_list_favorites()
        fav_ups = build_fav_updates(favs)

        return (
            gr.update(visible=True),
            gr.update(visible=True),
            st_s,
            en_s,
            "",
            gr.update(visible=True, choices=[], value=None),
            gr.update(value="{}"),
            gr.update(value=""),
            gr.update(value=""),
            *fav_ups
        )

    fab_btn.click(
        fn=open_modal_and_load,
        outputs=[modal_overlay, modal_sheet, start_txt, end_txt, msg_output, place_results,
                 selected_place_state, place_query, search_msg, *fav_buttons],
        js=JS_BOOT
    )

    def close_modal():
        return (gr.update(visible=False), gr.update(visible=False))

    close_btn.click(fn=close_modal, outputs=[modal_overlay, modal_sheet])
    cancel_btn.click(fn=close_modal, outputs=[modal_overlay, modal_sheet])

    def add_to_favorites(activity):
        activity = (activity or "").strip()
        if not activity:
            favs = db_list_favorites()
            return "⚠️ 활동명을 입력해 주세요", *build_fav_updates(favs)
        db_add_favorite(activity)
        favs = db_list_favorites()
        return f"✅ '{activity}'를 즐겨찾기에 추가했습니다", *build_fav_updates(favs)

    add_fav_btn.click(fn=add_to_favorites, inputs=[activity_text], outputs=[fav_msg, *fav_buttons])

    def choose_fav(v):
        return v or ""
    for b in fav_buttons:
        b.click(fn=choose_fav, inputs=[b], outputs=[activity_text])

    def search_and_store(query):
        cands, err = kakao_keyword_search(query, size=10)
        if err:
            return cands, gr.update(choices=[], value=None, visible=True), err, "{}"
        labels = [c["label"] for c in cands]
        return (cands, gr.update(choices=labels, value=None, visible=True), f"✅ {len(cands)}개 검색됨", "{}")

    search_btn.click(fn=search_and_store, inputs=[place_query],
                     outputs=[search_results_state, place_results, search_msg, selected_place_state])

    def update_selected(cands, label):
        if not label or not cands:
            return "{}", "", gr.update(visible=True), gr.update()
        for c in cands:
            if c["label"] == label:
                selected_json = json.dumps(c, ensure_ascii=False)
                msg = f"✅ '{c['place']}' 선택됨"
                # ✅ 선택하면 라디오 숨기고, 텍스트박스에 고정(라벨 표시)
                return selected_json, msg, gr.update(visible=False), gr.update(value=label)
        return "{}", "", gr.update(visible=True), gr.update()

    place_results.change(fn=update_selected, inputs=[search_results_state, place_results],
                         outputs=[selected_place_state, search_msg, place_results, place_query])

    def create_and_close(activity_text, start_txt, end_txt, capacity_unlimited, cap_max, photo_np, selected_place_json):
        msg, home, mapv = create_event(activity_text, start_txt, end_txt, capacity_unlimited, cap_max, photo_np, selected_place_json)
        if msg.startswith("✅"):
            return (msg, home, mapv, gr.update(visible=False), gr.update(visible=False))
        else:
            return (msg, home, mapv, gr.update(visible=True), gr.update(visible=True))

    create_btn.click(
        fn=create_and_close,
        inputs=[activity_text, start_txt, end_txt, capacity_unlimited, cap_max, photo_np, selected_place_state],
        outputs=[msg_output, home_html, map_html, modal_overlay, modal_sheet],
        js=JS_BOOT
    )

# =====================
# FastAPI + Kakao Map
# =====================
app = FastAPI()

@app.get("/delete/{space_id}")
def delete(space_id: str):
    try:
        db_delete_space(space_id)
    except:
        pass
    return RedirectResponse(url="/", status_code=302)

@app.get("/kakao_map")
def kakao_map():
    if not KAKAO_JAVASCRIPT_KEY:
        return HTMLResponse("<html><body><h3>KAKAO_JAVASCRIPT_KEY 필요</h3></body></html>")

    points = map_points_payload()
    if points:
        center_lat = sum(p["lat"] for p in points) / len(points)
        center_lng = sum(p["lng"] for p in points) / len(points)
    else:
        center_lat, center_lng = 36.0190, 129.3435

    html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
html,body{{margin:0;height:100%;}}
#map{{width:100%;height:100%;}}
.custom-info{{padding:10px;font-family:system-ui;font-size:12px;line-height:1.4;min-width:200px;}}
.info-title{{font-weight:900;margin-bottom:6px;font-size:13px;}}
.info-text{{color:#6B7280;margin:2px 0;font-size:11px;}}
.info-img{{width:100%;height:120px;object-fit:cover;border-radius:10px;margin:6px 0;border:1px solid #eee;}}
</style>
<script src="//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}"></script>
</head>
<body>
<div id="map"></div>
<script>
const map=new kakao.maps.Map(document.getElementById('map'),{{center:new kakao.maps.LatLng({center_lat},{center_lng}),level:6}});
const points={json.dumps(points,ensure_ascii=False)};
const bounds=new kakao.maps.LatLngBounds();
let activeIW = new kakao.maps.InfoWindow({{removable:true}});

function escHtml(s){{
  return String(s||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
}}

if(points.length===0){{
  const pos = new kakao.maps.LatLng({center_lat},{center_lng});
  new kakao.maps.Marker({{position:pos,map:map}});
}} else {{
  points.forEach(p=>{{
    const pos=new kakao.maps.LatLng(p.lat,p.lng);
    bounds.extend(pos);
    const marker=new kakao.maps.Marker({{position:pos,map:map}});
    kakao.maps.event.addListener(marker,'click',function(){{
      activeIW.close();
      const img = (p.photo_b64 && p.photo_b64.length>0)
        ? `<img class="info-img" src="data:image/jpeg;base64,${{p.photo_b64}}"/>`
        : "";
      const content = `
        <div class="custom-info">
          <div class="info-title">${{escHtml(p.title)}}</div>
          ${{img}}
          <div class="info-text">${{escHtml(p.period)}}</div>
          <div class="info-text">${{escHtml(p.addr)}}</div>
          <div class="info-text" style="margin-top:4px;color:#9CA3AF;">ID:${{escHtml(p.id)}}</div>
        </div>
      `;
      activeIW.setContent(content);
      activeIW.open(map, marker);
    }});
  }});
  map.setBounds(bounds);
}}
</script>
</body>
</html>
"""
    return HTMLResponse(html)

app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)


