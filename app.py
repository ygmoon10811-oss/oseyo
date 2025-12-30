import os, time, uuid, base64, io
from datetime import datetime, timedelta

import requests
import folium
from PIL import Image
import gradio as gr
import json
import numpy as np

# ===== persistence =====
# Render에서 디스크를 붙이면 이 경로를 디스크 마운트 경로로 바꾸면 됨
# 예: /var/data (Render persistent disk) 같은 곳
DATA_DIR = os.getenv("OSEYO_DATA_DIR", ".")
SPACES_PATH = os.path.join(DATA_DIR, "spaces.json")
FAVS_PATH   = os.path.join(DATA_DIR, "favs.json")

def _safe_load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return default

def _safe_save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except:
        pass

# ---- timezone (KST) ----
os.environ["TZ"] = "Asia/Seoul"
try:
    time.tzset()
except Exception:
    pass

def now_kst():
    return datetime.now()

def fmt_hm(dt: datetime):
    return dt.strftime("%H:%M")

def safe_parse_hm(hm: str):
    hm = (hm or "").strip()
    if not hm or ":" not in hm:
        return None
    hh, mm = hm.split(":", 1)
    try:
        h = int(hh); m = int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except:
        return None
    return None

def image_np_to_b64(img_np):
    if img_np is None:
        return ""
    try:
        arr = img_np

        # float/다른 dtype이면 0~255로 보정 후 uint8
        if arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype("uint8")

        # (H,W,4) RGBA면 RGB로 변환
        if len(arr.shape) == 3 and arr.shape[2] == 4:
            im = Image.fromarray(arr, "RGBA").convert("RGB")
        else:
            im = Image.fromarray(arr)

        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except:
        return ""

def b64_to_data_uri(b64_str):
    return f"data:image/jpeg;base64,{b64_str}" if b64_str else ""

DURATIONS = [15, 30, 45, 60]

def seed_spaces():
    return _safe_load_json(SPACES_PATH, [])

def seed_favorites():
    return _safe_load_json(FAVS_PATH, [])

def active_spaces(spaces):
    t = now_kst()
    out = []
    for s in spaces:
        if s.get("hidden"):
            continue
        try:
            st = datetime.fromisoformat(s["start"])
            en = datetime.fromisoformat(s["end"])
            if st <= t <= en:
                out.append(s)
        except:
            pass
    return out

# ---- address search (Nominatim) ----
def nominatim_search(q: str, limit=12):
    q = (q or "").strip()
    if not q:
        return [], "⚠️ 주소/장소명을 입력해 주세요."
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": q, "format": "jsonv2", "limit": limit, "countrycodes": "kr"}
    headers = {
        "User-Agent": "oseyo-colab/1.0 (gradio)",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"
    }
    try:
        r = requests.get(url, params=params, headers=headers, timeout=12)
        if r.status_code == 429:
            return [], "⚠️ 주소 검색이 일시적으로 차단(429)되었습니다. 1~2분 뒤 다시 시도해 주세요."
        if r.status_code >= 400:
            return [], f"⚠️ 주소 검색 실패 (HTTP {r.status_code})"
        data = r.json()
    except Exception as e:
        return [], f"⚠️ 네트워크 오류: {type(e).__name__}"

    cands = []
    for it in data or []:
        label = (it.get("display_name") or "").strip()
        lat = it.get("lat")
        lon = it.get("lon")
        if not label or lat is None or lon is None:
            continue
        try:
            cands.append({"label": label, "lat": float(lat), "lng": float(lon)})
        except:
            continue
    if not cands:
        return [], "⚠️ 검색 결과가 없습니다. 키워드를 바꿔보세요."
    return cands, ""

def addr_do_search(query):
    cands, err = nominatim_search(query, limit=12)

    if err:
        return (
            cands,
            gr.update(choices=[], value=None),
            err,
            "선택: 없음",
            ""  # chosen_label (State) reset
        )

    labels = [c["label"] for c in cands]
    return (
        cands,
        gr.update(choices=labels, value=None),
        "",
        "선택: 없음",
        ""  # chosen_label reset
    )

def on_radio_change(label):
    if not label:
        return "선택: 없음", ""
    return f"선택: {label}", label

def confirm_addr_wrap_by_label(cands, label, detail):
    label = (label or "").strip()
    if not label:
        return (
            "⚠️ 주소 후보를 선택해 주세요.",
            "", "", None, None,
            gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
            gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)
        )

    chosen = None
    for c in cands:
        if c.get("label") == label:
            chosen = c
            break

    if not chosen:
        return (
            "⚠️ 선택한 주소를 다시 선택해 주세요.",
            "", "", None, None,
            gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
            gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)
        )

    confirmed = chosen["label"]
    det = (detail or "").strip()
    lat, lng = chosen["lat"], chosen["lng"]

    return (
        "✅ 주소가 선택되었습니다.",
        confirmed, det, lat, lng,
        gr.update(visible=True), gr.update(visible=True), gr.update(visible=True),
        gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)
    )

# ---- map ----
def make_map_html(items, center=(36.0190, 129.3435), zoom=13):
    m = folium.Map(location=list(center), zoom_start=zoom, control_scale=True, zoom_control=True, tiles=None)
    folium.TileLayer(
        tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
        attr="&copy; OpenStreetMap contributors &copy; CARTO",
        control=False
    ).add_to(m)

    if not items:
        folium.Marker(list(center), tooltip="지금은 열려 있는 곳이 없습니다").add_to(m)
    else:
        for s in items:
            try:
                cap = f"최대 {s['capacityMax']}명" if s.get("capacityEnabled") else "제한 없음"
                st = fmt_hm(datetime.fromisoformat(s["start"]))
                en = fmt_hm(datetime.fromisoformat(s["end"]))
                detail = (s.get("address_detail") or "").strip()
                detail_line = f"<div style='color:#6B7280;'>상세: {detail}</div>" if detail else ""
                photo_uri = b64_to_data_uri(s.get("photo_b64", ""))
                img_line = f"<img src='{photo_uri}' style='width:100%;height:120px;object-fit:cover;border-radius:12px;margin-bottom:8px;'/>" if photo_uri else ""
                popup = f"""
                <div style="font-family:system-ui;font-size:13px;width:260px;">
                  {img_line}
                  <div style="font-weight:900;margin-bottom:6px;">{s['title']}</div>
                  <div style="color:#6B7280;">{s['address_confirmed']}</div>
                  {detail_line}
                  <div style="color:#6B7280;">오늘 {st}–{en}</div>
                  <div style="color:#6B7280;">{cap}</div>
                  <div style="color:#9CA3AF;font-size:12px;margin-top:8px;">ID: {s['id']}</div>
                </div>
                """
                folium.Marker([s["lat"], s["lng"]], tooltip=s["title"], popup=folium.Popup(popup, max_width=320)).add_to(m)
            except:
                continue

    raw = m.get_root().render().encode("utf-8")
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"""
    <div class="mapWrap">
      <iframe src="data:text/html;base64,{b64}" class="mapFrame" loading="lazy"></iframe>
      <div class="mapHint">📍 확대/축소: 버튼/휠 · 이동: 드래그</div>
    </div>
    """

def draw_map(spaces):
    return make_map_html(active_spaces(spaces), center=(36.0190, 129.3435), zoom=13)

def render_home(spaces):
    items = active_spaces(spaces)
    if not items:
        return """
        <div class="card">
          <div class="h">아직 열린 공간이 없습니다</div>
          <div class="p">오른쪽 아래 + 버튼으로 먼저 열어보면 된다</div>
        </div>
        """
    out=[]
    for s in items:
        try:
            st = fmt_hm(datetime.fromisoformat(s["start"]))
            en = fmt_hm(datetime.fromisoformat(s["end"]))
        except:
            st, en = "-", "-"
        cap = f"최대 {s['capacityMax']}명" if s.get("capacityEnabled") else "제한 없음"
        detail = (s.get("address_detail") or "").strip()
        detail_line = f"<div class='muted'>상세: {detail}</div>" if detail else ""
        photo_uri = b64_to_data_uri(s.get("photo_b64",""))
        img = f"<div class='img' style=\"background-image:url('{photo_uri}')\"></div>" if photo_uri else ""
        out.append(f"""
        <div class="card">
          {img}
          <div class="title">{s['title']}</div>
          <div class="muted">오늘 {st}–{en}</div>
          <div class="muted">{s['address_confirmed']}</div>
          {detail_line}
          <div class="muted">{cap}</div>
          <div class="row"><div class="chip">ID: {s['id']}</div></div>
        </div>
        """)
    return "\n".join(out)

# ---- open/close main (✅ gr.update로 고정) ----
def open_main_sheet():
    return (
        gr.update(visible=True),
        gr.update(visible=True),
        gr.update(visible=True),
        "",                       # main_msg
        fmt_hm(now_kst()),         # start_hm autofill
    )

def close_main_sheet():
    return (
        gr.update(visible=False),
        gr.update(visible=False),
        gr.update(visible=False),
        "",  # main_msg
    )

# ---- open/close addr ----
def open_addr_sheet():
    return (
        gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),  # hide main
        gr.update(visible=True),  gr.update(visible=True),  gr.update(visible=True),   # show addr
        [],                       # addr_candidates
        gr.update(choices=[], value=None),  # addr_radio
        "",                       # addr_err
        "선택: 없음",              # chosen_text
        "",                       # chosen_label (State)
        "",                       # addr_detail_in
        ""                        # addr_msg
    )

def back_to_main_from_addr():
    return (
        gr.update(visible=True),  gr.update(visible=True),  gr.update(visible=True),
        gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
        ""  # addr_msg
    )

def show_chosen_place(addr_confirmed, addr_detail):
    if not addr_confirmed:
        return "**선택된 장소:** *(아직 없음)*"
    if addr_detail:
        return f"**선택된 장소:** {addr_confirmed}\n\n상세: {addr_detail}"
    return f"**선택된 장소:** {addr_confirmed}"

# ---- favorites ----
def add_favorite(favs, activity_text):
    act = (activity_text or "").strip()
    favs = list(favs or [])
    if not act:
        return favs, "⚠️ 활동을 입력한 뒤 추가하면 된다.", gr.update(choices=favs, value=None)
    
    if act not in favs:
    favs.append(act)

    _safe_save_json(FAVS_PATH, favs)  # ✅ 저장
    return favs, f"✅ '{act}'을(를) 자주 하는 활동에 추가했다.", gr.update(choices=favs, value=None)

def use_favorite(label):
    if not label:
        return gr.update()
    return gr.update(value=label)

# ---- create ----
def create_space_and_close(
    spaces, activity_text, start_hm, duration, capacity_unlimited, cap_max,
    photo_np, addr_confirmed, addr_detail, addr_lat, addr_lng
):
    act = (activity_text or "").strip()
    if not act:
        return spaces, "⚠️ 활동을 입력해 달라.", render_home(spaces), draw_map(spaces), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

    if not addr_confirmed or addr_lat is None or addr_lng is None:
        return spaces, "⚠️ 장소를 선택해 달라. (장소 검색하기)", render_home(spaces), draw_map(spaces), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)
    
    t_now = now_kst().replace(second=0, microsecond=0)
    t = t_now
    parsed = safe_parse_hm(start_hm)
    if parsed is not None:
        h, m = parsed
        t = t.replace(hour=h, minute=m)
        # 과거면 지금으로 보정 (안 그러면 active_spaces 필터에서 바로 사라질 수 있음)
        if t < t_now:
            t = t_now

    end = t + timedelta(minutes=int(duration))
    new_id = uuid.uuid4().hex[:8]
    photo_b64 = image_np_to_b64(photo_np)

    capacityEnabled = (not bool(capacity_unlimited))
    capacityMax = None if not capacityEnabled else int(min(int(cap_max), 10))

    new_space = {
        "id": new_id,
        "title": f"{duration}분 {act}",
        "photo_b64": photo_b64,
        "start": t.isoformat(),
        "end": end.isoformat(),
        "address_confirmed": addr_confirmed,
        "address_detail": (addr_detail or "").strip(),
        "lat": float(addr_lat),
        "lng": float(addr_lng),
        "capacityEnabled": capacityEnabled,
        "capacityMax": capacityMax,
        "hidden": False,
    }

    spaces2 = list(spaces) + [new_space]
    _safe_save_json(SPACES_PATH, spaces2)  # ✅ 저장
    
    msg = f"✓ '{new_space['title']}'이(가) 열렸다. (ID: {new_id})"
    return spaces2, msg, render_home(spaces2), draw_map(spaces2), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

# ---- CSS ----
CSS = r"""
:root { --bg:#FAF9F6; --ink:#1F2937; --muted:#6B7280; --line:#E5E3DD; --accent:#111; --card:#ffffffcc; --orange:#FF6A00; }

html, body { width:100%; max-width:100%; overflow-x:hidden !important; }
.gradio-container { background: var(--bg) !important; width:100% !important; max-width:100% !important; overflow-x:hidden !important; }
* { box-sizing: border-box !important; }
.gradio-container * { max-width:100% !important; }

.card{ background: var(--card); border: 1px solid var(--line); border-radius: 22px; padding: 14px; margin: 12px 8px; overflow:hidden; }
.card .img{ width:100%; height: 160px; border-radius: 18px; background-size: cover; background-position: center; margin-bottom: 12px; }
.h{ font-size: 16px; font-weight: 900; color: var(--ink); margin-bottom: 6px; }
.p{ font-size: 13px; color: var(--muted); line-height: 1.6; }
.title{ font-size: 16px; font-weight: 900; color: var(--ink); margin-bottom: 6px; }
.muted{ font-size: 13px; color: var(--muted); line-height: 1.6; }
.row{ margin-top: 10px; display:flex; align-items:center; gap:10px; flex-wrap:wrap; }
.chip{ display:inline-block; padding: 8px 12px; border-radius: 999px; border:1px solid var(--line); background:#fff; font-size: 12px; color: var(--muted); }

.mapWrap{ width: min(420px, 96vw); margin: 12px auto; overflow:hidden; }
.mapFrame{ width: 100%; height: 520px; border: 1px solid var(--line); border-radius: 22px; }
.mapHint{ text-align:center; font-size: 12px; color: var(--muted); margin-top: 8px; }

.oseyo_overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.35); z-index: 99990; }

.oseyo_panel{
  position: fixed; left: 50%; transform: translateX(-50%); bottom: 0;
  width: min(420px, 96vw); height: 88vh;
  overflow-y: auto !important; overflow-x: hidden !important;
  -webkit-overflow-scrolling: touch;
  background: var(--bg);
  border: 1px solid var(--line); border-bottom: 0;
  border-radius: 26px 26px 0 0;
  padding: 22px 16px 150px 16px;
  z-index: 99991;
  box-shadow: 0 -12px 40px rgba(0,0,0,0.25);
}

/* 내부 요소 스크롤 제거(모달만 스크롤) */
.oseyo_panel *{ overflow: visible !important; scrollbar-width: none !important; -ms-overflow-style:none !important; }
.oseyo_panel *::-webkit-scrollbar{ width:0px !important; height:0px !important; }

.oseyo_panel label,
.oseyo_panel .gradio-label,
.oseyo_panel .markdown,
.oseyo_panel .prose { white-space: normal !important; word-break: break-word !important; line-height: 1.35 !important; }

.oseyo_footer{
  position: fixed; left: 50%; transform: translateX(-50%); bottom: 0;
  width: min(420px, 96vw);
  padding: 12px 16px 16px 16px;
  background: rgba(250,249,246,0.98);
  border-top: 1px solid var(--line);
  z-index: 99992;
  overflow:hidden;
}
.oseyo_footer button{
  width: 100% !important;
  margin: 6px 0 0 0 !important;
  border-radius: 16px !important;
  font-weight: 900 !important;
  border: 1px solid var(--line) !important;
  background: #fff !important;
}
.oseyo_footer .primary button{ background: var(--orange) !important; color:#fff !important; border:0 !important; }

#oseyo_fab { position: fixed !important; right: 18px !important; bottom: 18px !important; z-index: 200000 !important; width: 56px !important; height:56px !important; background: transparent !important; border: none !important; }
#oseyo_fab button{
  width:56px !important; height:56px !important; min-width:56px !important;
  border-radius:50% !important; border:0 !important;
  background: var(--accent) !important; color:#FAF9F6 !important;
  font-size:32px !important; font-weight:900 !important;
  line-height:56px !important; padding:0 !important; margin:0 !important;
  box-shadow:0 14px 30px rgba(0,0,0,0.35) !important;
}
.act_row { gap: 10px !important; }
"""

with gr.Blocks(css=CSS, title="Oseyo 모바일 MVP (Colab)") as demo:
    spaces = gr.State(seed_spaces())
    favs = gr.State(seed_favorites())

    # selected place states
    addr_confirmed = gr.State("")
    addr_detail = gr.State("")
    addr_lat = gr.State(None)
    addr_lng = gr.State(None)

    # address modal states
    addr_candidates = gr.State([])
    chosen_label = gr.State("")

    gr.HTML("""
    <div style="max-width:420px;margin:0 auto;padding:16px 10px 6px;text-align:center;">
      <div style="font-size:26px;font-weight:900;color:#1F2937;letter-spacing:-0.2px;">지금, 열려 있습니다</div>
      <div style="margin-top:6px;font-size:13px;color:#6B7280;">원하시면 오세요</div>
    </div>
    """)

    with gr.Tabs():
        with gr.Tab("탐색"):
            home_html = gr.HTML()
            refresh_btn = gr.Button("새로고침")
        with gr.Tab("지도"):
            map_refresh = gr.Button("지도 새로고침")
            map_html = gr.HTML()

    fab = gr.Button("+", elem_id="oseyo_fab")

    # ===== MAIN MODAL =====
    main_overlay = gr.HTML("<div class='oseyo_overlay'></div>", visible=False)
    with gr.Group(visible=False, elem_classes=["oseyo_panel"]) as main_sheet:
        gr.HTML("<div style='font-size:22px;font-weight:900;color:#1F2937;margin:0 0 10px 0;'>열어놓기</div>")
        gr.HTML("<div style='font-size:13px;color:#6B7280;line-height:1.7;margin:0 0 16px 0;'>아래 내용을 입력해 주세요. (사진은 선택사항입니다)</div>")

        photo_np = gr.Image(label="사진(선택)", type="numpy")

        with gr.Row(elem_classes=["act_row"]):
            activity_text = gr.Textbox(label="활동", placeholder="예: 산책, 커피, 스터디…", lines=1, scale=9)
            add_act_btn = gr.Button("추가", scale=1)

        fav_msg = gr.Markdown("")
        fav_radio = gr.Radio(choices=[], value=None, label="자주 하는 활동(선택하면 활동칸에 입력됨)")

        start_hm = gr.Textbox(label="시작시간(선택)", placeholder="미입력 시 현재 시각 자동", lines=1)
        duration = gr.Dropdown(choices=DURATIONS, value=30, label="지속 시간(분)")
        capacity_unlimited = gr.Checkbox(value=True, label="제한 없음")
        cap_max = gr.Slider(1, 10, value=4, step=1, label="최대 인원(제한 있을 때)")

        chosen_place_view = gr.Markdown("**선택된 장소:** *(아직 없음)*")
        open_addr_btn = gr.Button("장소 검색하기")
        main_msg = gr.Markdown("")

    with gr.Group(visible=False, elem_classes=["oseyo_footer"]) as main_footer:
        main_close = gr.Button("닫기")
        main_create = gr.Button("완료", elem_classes=["primary"])

    # ===== ADDRESS MODAL =====
    addr_overlay = gr.HTML("<div class='oseyo_overlay'></div>", visible=False)
    with gr.Group(visible=False, elem_classes=["oseyo_panel"]) as addr_sheet:
        gr.HTML("<div style='font-size:22px;font-weight:900;color:#1F2937;margin:0 0 10px 0;'>장소 검색</div>")
        gr.HTML("<div style='font-size:13px;color:#6B7280;line-height:1.7;margin:0 0 16px 0;'>주소/장소명을 검색하고, 아래 후보에서 하나를 선택하면 된다.</div>")

        addr_query = gr.Textbox(label="주소/장소명", placeholder="예: 포항시청, 영일대, 철길숲 …", lines=1)
        addr_search_btn = gr.Button("검색")
        addr_err = gr.Markdown("")
        chosen_text = gr.Markdown("선택: 없음")

        addr_radio = gr.Radio(choices=[], value=None, label="주소 후보(선택)")
        addr_detail_in = gr.Textbox(label="상세(선택)", placeholder="예: 2층 203호 / 입구 설명 등", lines=1)
        addr_msg = gr.Markdown("")

    with gr.Group(visible=False, elem_classes=["oseyo_footer"]) as addr_footer:
        addr_back = gr.Button("뒤로")
        addr_confirm_btn = gr.Button("주소 선택 완료", elem_classes=["primary"])

    # initial
    demo.load(fn=render_home, inputs=spaces, outputs=home_html)
    demo.load(fn=draw_map, inputs=spaces, outputs=map_html)
    refresh_btn.click(fn=render_home, inputs=spaces, outputs=home_html)
    map_refresh.click(fn=draw_map, inputs=spaces, outputs=map_html)

    # + FAB open main
    fab.click(fn=open_main_sheet, inputs=None, outputs=[main_overlay, main_sheet, main_footer, main_msg, start_hm])
    main_close.click(fn=close_main_sheet, inputs=None, outputs=[main_overlay, main_sheet, main_footer, main_msg])

    # favorites
    add_act_btn.click(fn=add_favorite, inputs=[favs, activity_text], outputs=[favs, fav_msg, fav_radio])
    fav_radio.change(fn=use_favorite, inputs=[fav_radio], outputs=[activity_text])

    # open addr
    open_addr_btn.click(
        fn=open_addr_sheet,
        inputs=None,
        outputs=[
            main_overlay, main_sheet, main_footer,
            addr_overlay, addr_sheet, addr_footer,
            addr_candidates, addr_radio, addr_err,
            chosen_text, chosen_label,
            addr_detail_in, addr_msg
        ]
    )

    # back
    addr_back.click(
        fn=back_to_main_from_addr,
        inputs=None,
        outputs=[
            main_overlay, main_sheet, main_footer,
            addr_overlay, addr_sheet, addr_footer,
            addr_msg
        ]
    )

    # search
    addr_search_btn.click(
        fn=addr_do_search,
        inputs=[addr_query],
        outputs=[addr_candidates, addr_radio, addr_err, chosen_text, chosen_label]
    )

    # pick radio -> chosen_label(State) 저장
    addr_radio.change(
        fn=on_radio_change,
        inputs=[addr_radio],
        outputs=[chosen_text, chosen_label]
    )

    # confirm
    addr_confirm_btn.click(
        fn=confirm_addr_wrap_by_label,
        inputs=[addr_candidates, chosen_label, addr_detail_in],
        outputs=[
            addr_msg, addr_confirmed, addr_detail, addr_lat, addr_lng,
            main_overlay, main_sheet, main_footer,
            addr_overlay, addr_sheet, addr_footer
        ]
    )

    # show chosen place in main
    addr_confirmed.change(fn=show_chosen_place, inputs=[addr_confirmed, addr_detail], outputs=[chosen_place_view])
    addr_detail.change(fn=show_chosen_place, inputs=[addr_confirmed, addr_detail], outputs=[chosen_place_view])

    # create
    main_create.click(
        fn=create_space_and_close,
        inputs=[
            spaces, activity_text, start_hm, duration, capacity_unlimited, cap_max,
            photo_np, addr_confirmed, addr_detail, addr_lat, addr_lng
        ],
        outputs=[spaces, main_msg, home_html, map_html, main_overlay, main_sheet, main_footer]
    )

if __name__ == "__main__":
    demo.launch(share=True, debug=True)


