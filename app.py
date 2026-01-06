import os, uuid, base64, io, sqlite3, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image
import gradio as gr

from fastapi import FastAPI
from fastapi.responses import RedirectResponse, HTMLResponse

KST = ZoneInfo("Asia/Seoul")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def now_kst():
    return datetime.now(KST)

# -------------------------
# DB 설정
# -------------------------
def get_data_dir():
    return "/var/data" if os.path.isdir("/var/data") else os.path.join(os.getcwd(), "data")

DATA_DIR = get_data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "oseyo.db")

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def db_init():
    # ❗DROP TABLE 제거 (이게 이벤트가 사라지는 원인)
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
        con.execute("INSERT OR IGNORE INTO favorites (activity, created_at) VALUES (?, ?)",
                   (activity, now_kst().isoformat()))
        con.commit()

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
        detail = (s.get("address_detail") or "").strip()
        detail_line = f"<div class='muted'>상세: {detail}</div>" if detail else ""
        photo_uri = b64_to_data_uri(s.get("photo_b64", ""))
        img = f"<img class='thumb' src='{photo_uri}' />" if photo_uri else "<div class='thumb placeholder'></div>"

        out.append(f"""
        <div class="card">
          <div class="rowcard">
            <div class="left">
              <div class="title">{s['title']}</div>
              <div class="period">{period}</div>
              <div class='muted'>{s['address']}</div>
              {detail_line}
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
            "detail": s.get("address_detail",""),
            "period": fmt_period(s.get("start_iso",""), s.get("end_iso","")),
            "id": s["id"],
        })
    return points

def draw_map():
    ts = int(now_kst().timestamp())
    return f"""
    <div class="mapWrap">
      <iframe class="mapFrame" src="/kakao_map?ts={ts}" loading="lazy"></iframe>
    </div>
    """

# -------------------------
# 날짜/시간 파싱 (키보드 입력까지 커버)
# -------------------------
def parse_dt_any(v):
    """
    gr.DateTime 값은 datetime 또는 str로 들어올 수 있음.
    키보드 입력(문자열)도 허용:
    - ISO (2026-01-06 10:30, 2026-01-06T10:30, 2026-01-06T10:30:00+09:00 등)
    """
    if v is None or v == "":
        return None

    if isinstance(v, datetime):
        dt = v
    elif isinstance(v, str):
        s = v.strip()
        # 흔한 케이스 보정
        s = s.replace("Z", "+00:00")
        s = s.replace("/", "-")
        # "YYYY-MM-DD HH:MM" → fromisoformat 가능
        try:
            dt = datetime.fromisoformat(s)
        except:
            # 마지막 보루: 초가 없거나 공백 등 보정
            try:
                if "T" not in s and " " in s:
                    dt = datetime.fromisoformat(s)
                else:
                    return None
            except:
                return None
    else:
        return None

    # tz 보정
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    else:
        dt = dt.astimezone(KST)
    return dt

def create_event(activity_text, start_dt, end_dt, capacity_unlimited, cap_max, photo_np, selected_place_json):
    print(f"[CREATE] activity={activity_text}, start={start_dt}, end={end_dt}")

    act = (activity_text or "").strip()
    if not act:
        return "⚠️ 활동명을 입력해 주세요", render_home(), draw_map()

    try:
        place_data = json.loads(selected_place_json) if selected_place_json else None
    except:
        place_data = None

    if not place_data:
        return "⚠️ 장소를 검색하고 선택해 주세요", render_home(), draw_map()

    st = parse_dt_any(start_dt)
    en = parse_dt_any(end_dt)
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
        print(f"[SUCCESS] 이벤트 생성: {new_id}")
        return f"✅ '{title}' 이벤트가 생성되었습니다!", render_home(), draw_map()
    except Exception as e:
        print(f"[ERROR] DB 저장 실패: {e}")
        return f"⚠️ 저장 실패: {str(e)}", render_home(), draw_map()

CSS = """
:root{--bg:#FAF9F6;--ink:#1F2937;--muted:#6B7280;--line:#E5E3DD;--card:#ffffffcc;--danger:#ef4444;}
*{box-sizing:border-box!important;}
html,body{width:100%;overflow-x:hidden!important;background:var(--bg)!important;margin:0;padding:0;}
.gradio-container{background:var(--bg)!important;max-width:1200px!important;margin:0 auto!important;padding-bottom:110px!important;}

/* ✅ 전역 가로스크롤 차단 */
#root, .contain, .wrap, .gradio-container, .gr-block, .gr-form { overflow-x:hidden!important; }

.banner{margin:10px auto 6px;padding:10px 12px;border-radius:14px;font-size:13px;}
.banner.ok{background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46;}
.banner.warn{background:#fff7ed;border:1px solid #fed7aa;color:#9a3412;}

.card{position:relative;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:14px;margin:12px 0;}
.card.empty{text-align:center;padding:40px;}
.h{font-size:18px;font-weight:900;margin-bottom:8px;}
.p{font-size:14px;color:var(--muted);}
.rowcard{display:grid;grid-template-columns:1fr 320px;gap:18px;padding-right:86px;}
.title{font-size:16px;font-weight:900;color:var(--ink);margin-bottom:6px;}
.period{font-size:14px;font-weight:900;color:#111827;margin:2px 0 8px;}
.muted{font-size:13px;color:var(--muted);line-height:1.55;margin:2px 0;}
.idline{margin-top:8px;font-size:12px;color:#9CA3AF;}
.thumb{width:100%;height:180px;object-fit:cover;border-radius:14px;}
.thumb.placeholder{background:rgba(0,0,0,0.05);}
.btn-del{position:absolute;right:14px;bottom:14px;background:var(--danger);color:#fff!important;font-weight:900;font-size:13px;padding:10px 14px;border-radius:12px;text-decoration:none;}

.mapWrap{width:100%;margin:0;padding:0;}
.mapFrame{width:100%;height:600px;border:0;border-radius:18px;}

/* ✅ FAB: 적당한 원형, 모달 열리면 자동 숨김 */
.fab-container{position:fixed!important;right:20px!important;bottom:20px!important;z-index:9000!important;height:0!important;overflow:visible!important;}
.fab-container button{
  width:56px!important;height:56px!important;min-width:56px!important;min-height:56px!important;
  border-radius:50%!important;border:0!important;background:#2B2A27!important;color:#FAF9F6!important;
  font-size:28px!important;font-weight:300!important;line-height:56px!important;padding:0!important;
  box-shadow:0 4px 12px rgba(0,0,0,0.2)!important;cursor:pointer!important;transition:all 0.2s ease!important;
}
.fab-container button:hover{transform:scale(1.08)!important;box-shadow:0 6px 16px rgba(0,0,0,0.3)!important;}
/* 모달 열리면 FAB 숨김(겹침 방지) */
body.modal-open .fab-container{display:none!important;}

/* ✅ 모달 overlay */
.modal-overlay{position:fixed!important;inset:0!important;background:rgba(0,0,0,0.5)!important;z-index:10000!important;backdrop-filter:blur(3px)!important;}

/*
  ✅ 핵심: 모달 시트에서 "상하 스크롤 1개만"
  - overflow-y:auto (시트만)
  - overflow-x:hidden (가로/내부 스크롤 금지)
  - 내부 컴포넌트가 별도 스크롤 생성 못하게 max-height/overflow 제거
*/
.modal-sheet{
  position:fixed!important;left:50%!important;top:50%!important;transform:translate(-50%,-50%)!important;
  width:min(500px,92vw)!important;
  max-height:88vh!important;
  overflow-y:auto!important;      /* ✅ 오직 이 스크롤만 허용 */
  overflow-x:hidden!important;    /* ✅ 가로스크롤 금지 */
  overscroll-behavior:contain!important;
  background:white!important;border:1px solid var(--line)!important;border-radius:20px!important;
  padding:20px 20px 120px 20px!important; /* ✅ 푸터 가림 방지: 하단 여유 */
  z-index:10001!important;box-shadow:0 20px 40px rgba(0,0,0,0.15)!important;
}
/* ✅ 내부 요소에서 별도 스크롤 못 만들게 */
.modal-sheet *{overflow:visible!important;}
.modal-sheet .gr-box,
.modal-sheet .gr-block,
.modal-sheet .gr-form,
.modal-sheet .wrap,
.modal-sheet .contain { overflow:visible!important; }

/* 모달 헤더 */
.modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;padding-bottom:12px;border-bottom:2px solid var(--line);}
.modal-title{font-size:20px;font-weight:900;color:var(--ink);}

/* 인풋 가독성 */
.modal-sheet label{font-size:13px!important;font-weight:700!important;margin-bottom:6px!important;display:block!important;}
.modal-sheet input[type="text"], .modal-sheet textarea{
  font-size:14px!important;padding:10px 12px!important;border-radius:10px!important;width:100%!important;border:1px solid var(--line)!important;
}

/* ✅ DateTime 팝업이 모달/오버레이 위로 뜨게(클릭 안되는 문제 해결) */
.flatpickr-calendar{z-index:10050!important;}
.flatpickr-time{z-index:10050!important;}
.flatpickr-months, .flatpickr-weekdays, .flatpickr-days{z-index:10050!important;}

/* ✅ 검색 결과 Radio가 내부 스크롤 만들지 않게 (높이 고정/스크롤 제거) */
.modal-sheet .gradio-radio, .modal-sheet .gradio-checkboxgroup, .modal-sheet .gradio-dropdown{
  max-height:none!important;
}
.modal-sheet .gradio-radio .wrap{max-height:none!important;}

/* ✅ 모달 푸터: 고정, 내용 안 가리도록 시트 패딩 확보 */
.modal-footer{
  position:fixed!important;left:50%!important;bottom:0!important;transform:translateX(-50%)!important;
  width:min(500px,92vw)!important;display:flex!important;gap:10px!important;
  padding:16px 20px!important;background:white!important;border-top:2px solid var(--line)!important;
  border-radius:0 0 20px 20px!important;z-index:10002!important;box-shadow:0 -4px 12px rgba(0,0,0,0.08)!important;
}
.modal-footer button{flex:1!important;padding:12px!important;border-radius:12px!important;font-weight:800!important;font-size:14px!important;}

@media (max-width:768px){
  .rowcard{grid-template-columns:1fr;padding-right:14px;}
  .thumb{height:200px;}
  .modal-sheet{width:94vw!important;max-height:90vh!important;padding:16px 16px 120px 16px!important;}
  .modal-footer{width:94vw!important;padding:14px 16px!important;}
  .modal-title{font-size:18px!important;}
  .fab-container{right:16px!important;bottom:16px!important;}
  .fab-container button{width:52px!important;height:52px!important;min-width:52px!important;min-height:52px!important;font-size:26px!important;}
}
"""

# -------------------------
# UI
# -------------------------
with gr.Blocks(css=CSS, title="Oseyo") as demo:
    search_results_state = gr.State([])
    selected_place_state = gr.Textbox(visible=False, value="{}")

    # ✅ 모달 상태에 따라 body class 토글용(HTML)
    body_class = gr.HTML("<script>document.body.classList.remove('modal-open');</script>")

    gr.Markdown("# 지금, 열려 있습니다\n원하시면 오세요")

    with gr.Tabs():
        with gr.Tab("탐색"):
            home_html = gr.HTML()
            refresh_btn = gr.Button("🔄 새로고침", size="sm")

        with gr.Tab("지도"):
            map_html = gr.HTML()
            map_refresh_btn = gr.Button("🔄 지도 새로고침", size="sm")

    with gr.Row(elem_classes=["fab-container"]):
        fab_btn = gr.Button("+", elem_id="fab")

    modal_overlay = gr.HTML("<div></div>", visible=False, elem_classes=["modal-overlay"])

    with gr.Column(visible=False, elem_classes=["modal-sheet"]) as modal_sheet:
        with gr.Row(elem_classes=["modal-header"]):
            gr.HTML("<div class='modal-title'>새 공간 열기</div>")
            close_btn = gr.Button("✕", size="sm")

        with gr.Row():
            activity_text = gr.Textbox(label="📝 활동명", placeholder="예: 산책, 커피, 스터디…", scale=4)
            add_fav_btn = gr.Button("⭐", size="sm", scale=1)

        favorites_dropdown = gr.Dropdown(label="⭐ 즐겨찾기", choices=[], value=None, interactive=True)
        fav_msg = gr.Markdown("")

        photo_np = gr.Image(label="📸 사진", type="numpy", height=160)

        # ✅ 클릭(팝업) + 키보드 입력 모두 허용
        start_dt = gr.DateTime(label="📅 시작 일시", include_time=True)
        end_dt = gr.DateTime(label="⏰ 종료 일시", include_time=True)

        with gr.Row():
            capacity_unlimited = gr.Checkbox(label="👥 제한없음", value=True, scale=1)
            cap_max = gr.Slider(label="최대인원", minimum=1, maximum=10, value=4, step=1, scale=2)

        with gr.Row():
            place_query = gr.Textbox(label="📍 장소", placeholder="예: 포항시청, 영일대", scale=4)
            search_btn = gr.Button("🔍", scale=1, size="sm")

        search_msg = gr.Markdown("")
        # ✅ 선택하면 자동으로 숨길 거라 visible=True로 두고 update로 제어
        place_results = gr.Radio(label="검색 결과 (클릭하면 선택됨)", choices=[], value=None, visible=True)

        msg_output = gr.Markdown("")

        with gr.Row(elem_classes=["modal-footer"]):
            cancel_btn = gr.Button("취소", variant="secondary")
            create_btn = gr.Button("✅ 생성", variant="primary")

    demo.load(fn=render_home, outputs=home_html)
    demo.load(fn=draw_map, outputs=map_html)

    refresh_btn.click(fn=render_home, outputs=home_html)
    map_refresh_btn.click(fn=draw_map, outputs=map_html)

    # 모달 열기 시 즐겨찾기 목록 로드 + body class ON
    def open_and_load_favs():
        st = now_kst().replace(second=0, microsecond=0)
        en = st + timedelta(hours=2)
        favs = db_list_favorites()
        body = "<script>document.body.classList.add('modal-open');</script>"
        return (
            gr.update(visible=True),   # overlay
            gr.update(visible=True),   # sheet
            body,                      # body class
            st,
            en,
            "",
            gr.update(choices=favs, value=None),
            gr.update(visible=True, choices=[], value=None),  # place_results 초기화
            gr.update(value="{}"),     # selected_place_state
            gr.update(value=""),       # place_query
            gr.update(value=""),       # search_msg
        )

    fab_btn.click(
        fn=open_and_load_favs,
        outputs=[modal_overlay, modal_sheet, body_class, start_dt, end_dt, msg_output, favorites_dropdown, place_results, selected_place_state, place_query, search_msg],
    )

    def close_modal():
        body = "<script>document.body.classList.remove('modal-open');</script>"
        return (gr.update(visible=False), gr.update(visible=False), body)

    close_btn.click(fn=close_modal, outputs=[modal_overlay, modal_sheet, body_class])
    cancel_btn.click(fn=close_modal, outputs=[modal_overlay, modal_sheet, body_class])

    # 즐겨찾기 추가
    def add_to_favorites(activity):
        activity = (activity or "").strip()
        if not activity:
            return "⚠️ 활동명을 입력해 주세요", gr.update()
        db_add_favorite(activity)
        favs = db_list_favorites()
        return f"✅ '{activity}'를 즐겨찾기에 추가했습니다", gr.update(choices=favs, value=None)

    add_fav_btn.click(fn=add_to_favorites, inputs=[activity_text], outputs=[fav_msg, favorites_dropdown])

    # 즐겨찾기 선택 → 활동명에 반영
    def select_favorite(fav):
        return fav or ""

    favorites_dropdown.change(fn=select_favorite, inputs=[favorites_dropdown], outputs=[activity_text])

    # 장소 검색
    def search_and_store(query):
        cands, err = kakao_keyword_search(query, size=10)
        if err:
            return cands, gr.update(visible=True, choices=[], value=None), err, "{}"
        labels = [c["label"] for c in cands]
        return (cands, gr.update(visible=True, choices=labels, value=None), f"✅ {len(cands)}개 검색됨", "{}")

    search_btn.click(fn=search_and_store, inputs=[place_query], outputs=[search_results_state, place_results, search_msg, selected_place_state])

    # ✅ 장소 선택: 선택 즉시 "주소 입력칸에 고정" + "옵션 리스트 접기(숨김)"
    def update_selected(cands, label):
        if not label or not cands:
            return "{}", "", gr.update(visible=True), gr.update()
        for c in cands:
            if c["label"] == label:
                # place_query에는 선택된 label(사람이 보기 쉬움)을 고정
                selected_json = json.dumps(c, ensure_ascii=False)
                msg = f"✅ '{c['place']}' 선택됨"
                # 옵션은 접는다(숨김)
                return selected_json, msg, gr.update(visible=False), gr.update(value=label)
        return "{}", "", gr.update(visible=True), gr.update()

    place_results.change(
        fn=update_selected,
        inputs=[search_results_state, place_results],
        outputs=[selected_place_state, search_msg, place_results, place_query],
    )

    def create_and_close(activity_text, start_dt, end_dt, capacity_unlimited, cap_max, photo_np, selected_place_json):
        msg, home, mapv = create_event(activity_text, start_dt, end_dt, capacity_unlimited, cap_max, photo_np, selected_place_json)
        if msg.startswith("✅"):
            body = "<script>document.body.classList.remove('modal-open');</script>"
            return (msg, home, mapv, gr.update(visible=False), gr.update(visible=False), body)
        else:
            body = "<script>document.body.classList.add('modal-open');</script>"
            return (msg, home, mapv, gr.update(visible=True), gr.update(visible=True), body)

    create_btn.click(
        fn=create_and_close,
        inputs=[activity_text, start_dt, end_dt, capacity_unlimited, cap_max, photo_np, selected_place_state],
        outputs=[msg_output, home_html, map_html, modal_overlay, modal_sheet, body_class],
    )

# -------------------------
# FastAPI
# -------------------------
app = FastAPI()

@app.get("/")
def root():
    return RedirectResponse(url="/app", status_code=302)

@app.get("/delete/{space_id}")
def delete(space_id: str):
    try:
        db_delete_space(space_id)
    except:
        pass
    return RedirectResponse(url="/app", status_code=302)

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
.custom-info{{padding:10px;font-family:system-ui;font-size:12px;line-height:1.4;min-width:180px;}}
.info-title{{font-weight:900;margin-bottom:4px;font-size:13px;}}
.info-text{{color:#6B7280;margin:2px 0;font-size:11px;}}
</style>
<script src="//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}"></script>
</head>
<body>
<div id="map"></div>
<script>
const map=new kakao.maps.Map(document.getElementById('map'),{{center:new kakao.maps.LatLng({center_lat},{center_lng}),level:6}});
const points={json.dumps(points,ensure_ascii=False)};
if(points.length===0){{new kakao.maps.Marker({{position:new kakao.maps.LatLng({center_lat},{center_lng})}}).setMap(map);}}else{{
const bounds=new kakao.maps.LatLngBounds();
points.forEach(p=>{{
const pos=new kakao.maps.LatLng(p.lat,p.lng);
bounds.extend(pos);
const marker=new kakao.maps.Marker({{position:pos,map:map}});
const content=`<div class="custom-info"><div class="info-title">${{p.title}}</div><div class="info-text">${{p.period}}</div><div class="info-text">${{p.addr}}</div>${{p.detail?`<div class="info-text">상세:${{p.detail}}</div>`:''}}
<div class="info-text" style="margin-top:4px;color:#9CA3AF;">ID:${{p.id}}</div></div>`;
const infowindow=new kakao.maps.InfoWindow({{content:content}});
kakao.maps.event.addListener(marker,'click',function(){{infowindow.open(map,marker);}});
}});
map.setBounds(bounds);
}}
</script>
</body>
</html>
"""
    return HTMLResponse(html)

app = gr.mount_gradio_app(app, demo, path="/app")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(app, host="0.0.0.0", port=port)
