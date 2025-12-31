import os, time, uuid, base64, io, sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import folium
from PIL import Image
import gradio as gr

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

KST = ZoneInfo("Asia/Seoul")

def now_kst():
    # Render/리눅스에서 TZ가 꼬여도 이건 무조건 KST로 간다
    return datetime.now(KST)

def fmt_hm(dt: datetime):
    return dt.astimezone(KST).strftime("%H:%M")

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
        im = Image.fromarray(img_np.astype("uint8"))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except:
        return ""

def b64_to_data_uri(b64_str):
    return f"data:image/jpeg;base64,{b64_str}" if b64_str else ""

# =========================
# DB (SQLite) - Render Disk 대응
# =========================
def get_data_dir():
    if os.path.isdir("/var/data"):
        return "/var/data"
    return os.path.join(os.getcwd(), "data")

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
            photo_b64 TEXT,
            start_iso TEXT NOT NULL,
            end_iso TEXT NOT NULL,
            address_confirmed TEXT NOT NULL,
            address_detail TEXT,
            lat REAL NOT NULL,
            lng REAL NOT NULL,
            capacity_enabled INTEGER NOT NULL,
            capacity_max INTEGER,
            hidden INTEGER NOT NULL DEFAULT 0,
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
            address_confirmed, address_detail, lat, lng,
            capacity_enabled, capacity_max, hidden, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            space["id"], space["title"], space.get("photo_b64",""),
            space["start"], space["end"],
            space["address_confirmed"], space.get("address_detail",""),
            float(space["lat"]), float(space["lng"]),
            1 if space.get("capacityEnabled") else 0,
            space.get("capacityMax"),
            1 if space.get("hidden") else 0,
            now_kst().isoformat()
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
                   address_confirmed, address_detail, lat, lng,
                   capacity_enabled, capacity_max, hidden
            FROM spaces
            ORDER BY created_at DESC
        """).fetchall()
    out=[]
    for r in rows:
        out.append({
            "id": r[0],
            "title": r[1],
            "photo_b64": r[2] or "",
            "start": r[3],
            "end": r[4],
            "address_confirmed": r[5],
            "address_detail": r[6] or "",
            "lat": float(r[7]),
            "lng": float(r[8]),
            "capacityEnabled": bool(r[9]),
            "capacityMax": r[10],
            "hidden": bool(r[11]),
        })
    return out

# =========================
# Active filtering (timezone-aware)
# =========================
def active_spaces(spaces):
    t = now_kst()
    out = []
    for s in spaces:
        if s.get("hidden"):
            continue
        try:
            st = datetime.fromisoformat(s["start"])
            en = datetime.fromisoformat(s["end"])
            # 저장된 ISO가 tz-aware면 그대로, naive면 KST로 간주
            if st.tzinfo is None:
                st = st.replace(tzinfo=KST)
            if en.tzinfo is None:
                en = en.replace(tzinfo=KST)

            if st <= t <= en:
                out.append(s)
        except:
            pass
    return out

# =========================
# Address search (Nominatim)
# =========================
def nominatim_search(q: str, limit=12):
    q = (q or "").strip()
    if not q:
        return [], "⚠️ 주소/장소명을 입력해 주세요."
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": q, "format": "jsonv2", "limit": limit, "countrycodes": "kr"}
    headers = {
        "User-Agent": "oseyo-render/1.0 (gradio)",
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
        lat = it.get("lat"); lon = it.get("lon")
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
        return (cands, gr.update(choices=[], value=None), err, "선택: 없음", "")
    labels = [c["label"] for c in cands]
    return (cands, gr.update(choices=labels, value=None), "", "선택: 없음", "")

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

# =========================
# Map HTML (full screen)
# =========================
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
                img_line = f"<img src='{photo_uri}' style='width:100%;height:140px;object-fit:cover;border-radius:12px;margin-bottom:8px;'/>" if photo_uri else ""
                popup = f"""
                <div style="font-family:system-ui;font-size:13px;width:280px;">
                  {img_line}
                  <div style="font-weight:900;margin-bottom:6px;">{s['title']}</div>
                  <div style="color:#6B7280;">{s['address_confirmed']}</div>
                  {detail_line}
                  <div style="color:#6B7280;">오늘 {st}–{en}</div>
                  <div style="color:#6B7280;">{cap}</div>
                  <div style="color:#9CA3AF;font-size:12px;margin-top:8px;">ID: {s['id']}</div>
                </div>
                """
                folium.Marker([s["lat"], s["lng"]], tooltip=s["title"], popup=folium.Popup(popup, max_width=360)).add_to(m)
            except:
                continue

    raw = m.get_root().render().encode("utf-8")
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"""
    <div class="mapWrap">
      <iframe src="data:text/html;base64,{b64}" class="mapFrame" loading="lazy"></iframe>
    </div>
    """

def draw_map():
    spaces = db_list_spaces()
    return make_map_html(active_spaces(spaces), center=(36.0190, 129.3435), zoom=13)

# =========================
# Home HTML (cards)
# =========================
def render_home():
    spaces = db_list_spaces()
    items = active_spaces(spaces)

    persistent = os.path.isdir("/var/data")
    banner = (
        f"<div class='banner ok'>✅ 영구저장 모드이다 (DB: {DB_PATH}). 새로고침해도 이벤트가 유지된다.</div>"
        if persistent else
        f"<div class='banner warn'>⚠️ 임시저장 모드이다 (DB: {DB_PATH}). Render Disk를 붙이면 영구저장 된다.</div>"
    )

    if not items:
        return banner + """
        <div class="card empty">
          <div class="h">아직 열린 공간이 없습니다</div>
          <div class="p">오른쪽 아래 + 버튼으로 먼저 열어보면 된다</div>
        </div>
        """

    out = [banner]
    for s in items:
        try:
            st = fmt_hm(datetime.fromisoformat(s["start"]))
            en = fmt_hm(datetime.fromisoformat(s["end"]))
        except:
            st, en = "-", "-"

        cap = f"최대 {s['capacityMax']}명" if s.get("capacityEnabled") else "제한 없음"
        detail = (s.get("address_detail") or "").strip()
        detail_line = f"<div class='muted'>상세: {detail}</div>" if detail else "<div class='muted'>상세: -</div>"

        photo_uri = b64_to_data_uri(s.get("photo_b64", ""))
        img = f"<img class='thumb' src='{photo_uri}' />" if photo_uri else "<div class='thumb placeholder'></div>"

        out.append(f"""
        <div class="card">
          <div class="rowcard">
            <div class="left">
              <div class="title">{s['title']}</div>
              <div class="muted">오늘 {st}–{en}</div>
              <div class="muted">{s['address_confirmed']}</div>
              {detail_line}
              <div class="muted">{cap}</div>
              <div class="idline">ID: {s['id']}</div>
            </div>
            <div class="right">{img}</div>
          </div>
          <a class="btn-del" href="/delete/{s['id']}">삭제</a>
        </div>
        """)

    return "\n".join(out)

# =========================
# Modal open/close
# =========================
def open_main_sheet():
    return (
        gr.update(visible=True),
        gr.update(visible=True),
        gr.update(visible=True),
        "",
        fmt_hm(now_kst()),
    )

def close_main_sheet():
    return (
        gr.update(visible=False),
        gr.update(visible=False),
        gr.update(visible=False),
        "",
    )

def open_addr_sheet():
    return (
        gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
        gr.update(visible=True),  gr.update(visible=True),  gr.update(visible=True),
        [],
        gr.update(choices=[], value=None),
        "",
        "선택: 없음",
        "",
        "",
        ""
    )

def back_to_main_from_addr():
    return (
        gr.update(visible=True),  gr.update(visible=True),  gr.update(visible=True),
        gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
        ""
    )

def show_chosen_place(addr_confirmed, addr_detail):
    if not addr_confirmed:
        return "**선택된 장소:** *(아직 없음)*"
    if addr_detail:
        return f"**선택된 장소:** {addr_confirmed}\n\n상세: {addr_detail}"
    return f"**선택된 장소:** {addr_confirmed}"

# =========================
# Favorites
# =========================
def seed_favorites():
    return []

def add_favorite(favs, activity_text):
    act = (activity_text or "").strip()
    favs = list(favs or [])
    if not act:
        return favs, "⚠️ 활동을 입력한 뒤 추가하면 된다.", gr.update(choices=favs, value=None)
    if act not in favs:
        favs.append(act)
    return favs, f"✅ '{act}'을(를) 자주 하는 활동에 추가했다.", gr.update(choices=favs, value=None)

def use_favorite(label):
    if not label:
        return gr.update()
    return gr.update(value=label)

# =========================
# Create space (1-click 안정화)
# =========================
def create_space_and_close(
    activity_text, start_hm, dur_h, dur_m, capacity_unlimited, cap_max,
    photo_np, addr_confirmed, addr_detail, addr_lat, addr_lng
):
    try:
        act = (activity_text or "").strip()
        if not act:
            return "⚠️ 활동을 입력해 달라.", render_home(), draw_map(), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

        if (not addr_confirmed) or (addr_lat is None) or (addr_lng is None):
            return "⚠️ 장소를 선택해 달라. (장소 검색하기)", render_home(), draw_map(), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

        t = now_kst().replace(second=0, microsecond=0)
        parsed = safe_parse_hm(start_hm)
        if parsed is not None:
            h, m = parsed
            t = t.replace(hour=h, minute=m)

        minutes = int(dur_h) * 60 + int(dur_m)
        if minutes <= 0:
            minutes = 30

        end = t + timedelta(minutes=minutes)
        new_id = uuid.uuid4().hex[:8]
        photo_b64 = image_np_to_b64(photo_np)

        capacityEnabled = (not bool(capacity_unlimited))
        capacityMax = None if not capacityEnabled else int(min(int(cap_max), 10))

        if minutes < 60:
            title = f"{minutes}분 {act}"
        else:
            title = f"{minutes//60}시간 {minutes%60}분 {act}" if minutes % 60 else f"{minutes//60}시간 {act}"

        new_space = {
            "id": new_id,
            "title": title,
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

        db_insert_space(new_space)
        msg = f"✅ 등록 완료: '{title}' (ID: {new_id})"

        # ✅ 등록 즉시 홈/지도 강제 갱신 + 모달 닫기
        return msg, render_home(), draw_map(), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    except Exception as e:
        return f"❌ 등록 중 오류: {type(e).__name__}", render_home(), draw_map(), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

# =========================
# CSS는 <style>로 강제 주입 (Gradio 6 대응)
# =========================
CSS = r"""
:root { --bg:#FAF9F6; --ink:#1F2937; --muted:#6B7280; --line:#E5E3DD; --card:#ffffffcc; --orange:#FF6A00; --danger:#ef4444; }
html, body { width:100%; max-width:100%; overflow-x:hidden !important; background: var(--bg) !important; }
.gradio-container { background: var(--bg) !important; width:100% !important; max-width:100% !important; overflow-x:hidden !important; }
* { box-sizing:border-box !important; }
.gradio-container * { max-width:100% !important; }

.banner{ max-width:1200px; margin:10px auto 6px; padding:10px 12px; border-radius:14px; font-size:13px; line-height:1.5; }
.banner.ok{ background:#ecfdf5; border:1px solid #a7f3d0; color:#065f46; }
.banner.warn{ background:#fff7ed; border:1px solid #fed7aa; color:#9a3412; }

.card{ position:relative; background:var(--card); border:1px solid var(--line); border-radius:18px; padding:14px; margin:12px auto; max-width:1200px; }
.card.empty{ max-width:700px; }
.h{ font-size:16px; font-weight:900; color:var(--ink); margin-bottom:6px; }
.p{ font-size:13px; color:var(--muted); line-height:1.6; }

.rowcard{ display:grid; grid-template-columns:1fr minmax(320px,560px); gap:18px; align-items:start; padding-right:86px; }
.title{ font-size:16px; font-weight:900; color:var(--ink); margin-bottom:6px; }
.muted{ font-size:13px; color:var(--muted); line-height:1.55; }
.idline{ margin-top:8px; font-size:12px; color:#9CA3AF; }

.thumb{ width:100%; height:220px; object-fit:cover; border-radius:14px; border:1px solid var(--line); display:block; background:#fff; }
.thumb.placeholder{ width:100%; height:220px; border-radius:14px; border:1px dashed var(--line); background:rgba(255,255,255,0.6); }

.btn-del{
  position:absolute; right:14px; bottom:14px;
  text-decoration:none !important;
  background:var(--danger); color:#fff !important;
  font-weight:900; font-size:13px;
  padding:10px 14px; border-radius:12px;
}

@media (max-width:820px){
  .rowcard{ grid-template-columns:1fr; padding-right:14px; }
  .thumb{ height:180px; }
  .btn-del{ position:static; display:block; width:100%; margin-top:10px; text-align:center; }
}

/* 지도: 탭/헤더 빼고 거의 풀스크린 */
.mapWrap{ width:100vw; max-width:100vw; margin:0; padding:0; overflow:hidden; }
.mapFrame{ width:100vw; height: calc(100vh - 140px); border:0; border-radius:0; }

/* 모달 */
.oseyo_overlay { position:fixed; inset:0; background:rgba(0,0,0,0.35); z-index:99990; }
.oseyo_panel{
  position:fixed; left:50%; transform:translateX(-50%); bottom:0;
  width:min(420px,96vw); height:88vh; overflow-y:auto !important;
  background:var(--bg); border:1px solid var(--line); border-bottom:0;
  border-radius:26px 26px 0 0; padding:22px 16px 150px 16px;
  z-index:99991; box-shadow:0 -12px 40px rgba(0,0,0,0.25);
}
.oseyo_footer{
  position:fixed; left:50%; transform:translateX(-50%); bottom:0;
  width:min(420px,96vw); padding:12px 16px 16px 16px;
  background:rgba(250,249,246,0.98); border-top:1px solid var(--line);
  z-index:99992;
}

/* FAB: wrapper 자체를 fixed */
#oseyo_fab{
  position:fixed !important;
  right:22px !important; bottom:22px !important;
  z-index:999999 !important;
  width:64px !important; height:64px !important;
}
#oseyo_fab button{
  width:64px !important; height:64px !important;
  min-width:64px !important;
  border-radius:50% !important;
  border:0 !important;
  background:#111 !important;
  color:#FAF9F6 !important;
  font-size:36px !important;
  font-weight:900 !important;
  line-height:64px !important;
  box-shadow:0 14px 30px rgba(0,0,0,0.35) !important;
}
"""

DUR_H = [0, 1, 2, 3, 4, 5, 6]
DUR_M = [0, 15, 30, 45]

with gr.Blocks(title="Oseyo (DB)") as demo:
    # ✅ CSS 강제 주입 (Gradio 6에서도 무조건 먹는다)
    gr.HTML(f"<style>{CSS}</style>")

    favs = gr.State([])
    addr_confirmed = gr.State("")
    addr_detail = gr.State("")
    addr_lat = gr.State(None)
    addr_lng = gr.State(None)

    addr_candidates = gr.State([])
    chosen_label = gr.State("")

    gr.HTML("""
    <div style="max-width:1200px;margin:0 auto;padding:18px 12px 10px;text-align:center;">
      <div style="font-size:28px;font-weight:900;color:#1F2937;letter-spacing:-0.2px;">지금, 열려 있습니다</div>
      <div style="margin-top:6px;font-size:13px;color:#6B7280;">원하시면 오세요</div>
    </div>
    """)

    with gr.Tabs():
        with gr.Tab("탐색"):
            home_html = gr.HTML()
            refresh_btn = gr.Button("새로고침")
        with gr.Tab("지도"):
            map_html = gr.HTML()
            map_refresh = gr.Button("지도 새로고침")

    fab = gr.Button("+", elem_id="oseyo_fab")

    main_overlay = gr.HTML("<div class='oseyo_overlay'></div>", visible=False)
    with gr.Group(visible=False, elem_classes=["oseyo_panel"]) as main_sheet:
        gr.HTML("<div style='font-size:22px;font-weight:900;color:#1F2937;margin:0 0 10px 0;'>열어놓기</div>")
        photo_np = gr.Image(label="사진(선택)", type="numpy")

        with gr.Row():
            activity_text = gr.Textbox(label="활동", placeholder="예: 산책, 커피, 스터디…", lines=1, scale=9)
            add_act_btn = gr.Button("추가", scale=1)

        fav_msg = gr.Markdown("")
        fav_radio = gr.Radio(choices=[], value=None, label="자주 하는 활동(선택)")

        start_hm = gr.Textbox(label="시작시간(선택)", placeholder="미입력 시 현재 시각 자동", lines=1)

        with gr.Row():
            dur_h = gr.Dropdown(choices=DUR_H, value=0, label="시간")
            dur_m = gr.Dropdown(choices=DUR_M, value=30, label="분")

        capacity_unlimited = gr.Checkbox(value=True, label="제한 없음")
        cap_max = gr.Slider(1, 10, value=4, step=1, label="최대 인원(제한 있을 때)")

        chosen_place_view = gr.Markdown("**선택된 장소:** *(아직 없음)*")
        open_addr_btn = gr.Button("장소 검색하기")
        main_msg = gr.Markdown("")

    with gr.Group(visible=False, elem_classes=["oseyo_footer"]) as main_footer:
        main_close = gr.Button("닫기")
        main_create = gr.Button("완료", elem_classes=["primary"])

    addr_overlay = gr.HTML("<div class='oseyo_overlay'></div>", visible=False)
    with gr.Group(visible=False, elem_classes=["oseyo_panel"]) as addr_sheet:
        gr.HTML("<div style='font-size:22px;font-weight:900;color:#1F2937;margin:0 0 10px 0;'>장소 검색</div>")

        addr_query = gr.Textbox(label="주소/장소명", placeholder="예: 포항시청, 영일대 …", lines=1)
        addr_search_btn = gr.Button("검색")
        addr_err = gr.Markdown("")
        chosen_text = gr.Markdown("선택: 없음")

        addr_radio = gr.Radio(choices=[], value=None, label="주소 후보(선택)")
        addr_detail_in = gr.Textbox(label="상세(선택)", placeholder="예: 2층 203호 …", lines=1)
        addr_msg = gr.Markdown("")

    with gr.Group(visible=False, elem_classes=["oseyo_footer"]) as addr_footer:
        addr_back = gr.Button("뒤로")
        addr_confirm_btn = gr.Button("주소 선택 완료", elem_classes=["primary"])

    # ✅ 핵심: 새로고침/접속할 때마다 DB 다시 읽게 함
    demo.load(fn=render_home, inputs=None, outputs=home_html)
    demo.load(fn=draw_map, inputs=None, outputs=map_html)

    refresh_btn.click(fn=render_home, inputs=None, outputs=home_html)
    map_refresh.click(fn=draw_map, inputs=None, outputs=map_html)

    fab.click(fn=open_main_sheet, inputs=None, outputs=[main_overlay, main_sheet, main_footer, main_msg, start_hm])
    main_close.click(fn=close_main_sheet, inputs=None, outputs=[main_overlay, main_sheet, main_footer, main_msg])

    add_act_btn.click(fn=add_favorite, inputs=[favs, activity_text], outputs=[favs, fav_msg, fav_radio])
    fav_radio.change(fn=use_favorite, inputs=[fav_radio], outputs=[activity_text])

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

    addr_back.click(
        fn=back_to_main_from_addr,
        inputs=None,
        outputs=[
            main_overlay, main_sheet, main_footer,
            addr_overlay, addr_sheet, addr_footer,
            addr_msg
        ]
    )

    addr_search_btn.click(
        fn=addr_do_search,
        inputs=[addr_query],
        outputs=[addr_candidates, addr_radio, addr_err, chosen_text, chosen_label]
    )

    addr_radio.change(
        fn=on_radio_change,
        inputs=[addr_radio],
        outputs=[chosen_text, chosen_label]
    )

    addr_confirm_btn.click(
        fn=confirm_addr_wrap_by_label,
        inputs=[addr_candidates, chosen_label, addr_detail_in],
        outputs=[
            addr_msg, addr_confirmed, addr_detail, addr_lat, addr_lng,
            main_overlay, main_sheet, main_footer,
            addr_overlay, addr_sheet, addr_footer
        ]
    )

    addr_confirmed.change(fn=show_chosen_place, inputs=[addr_confirmed, addr_detail], outputs=[chosen_place_view])
    addr_detail.change(fn=show_chosen_place, inputs=[addr_confirmed, addr_detail], outputs=[chosen_place_view])

    main_create.click(
        fn=create_space_and_close,
        inputs=[
            activity_text, start_hm, dur_h, dur_m, capacity_unlimited, cap_max,
            photo_np, addr_confirmed, addr_detail, addr_lat, addr_lng
        ],
        outputs=[main_msg, home_html, map_html, main_overlay, main_sheet, main_footer]
    )

# =========================
# FastAPI + delete route
# =========================
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

app = gr.mount_gradio_app(app, demo, path="/app")
