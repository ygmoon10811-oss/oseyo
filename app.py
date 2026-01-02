import os, uuid, base64, io, sqlite3
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
    return datetime.now(KST)

# =========================
# datetime normalize/format
# =========================
def parse_dt_kst(s: str):
    s = (s or "").strip()
    if not s:
        return None
    s = s.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=KST)
        except:
            pass
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        else:
            dt = dt.astimezone(KST)
        return dt
    except:
        return None

def normalize_dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=KST)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=KST)
    if isinstance(v, str):
        return parse_dt_kst(v)
    return None

def fmt_period(start_iso: str, end_iso: str):
    try:
        st = datetime.fromisoformat(start_iso)
        en = datetime.fromisoformat(end_iso)
        if st.tzinfo is None: st = st.replace(tzinfo=KST)
        if en.tzinfo is None: en = en.replace(tzinfo=KST)
        st = st.astimezone(KST)
        en = en.astimezone(KST)
        if st.date() == en.date():
            return f"{st.strftime('%m/%d')} {st.strftime('%H:%M')}–{en.strftime('%H:%M')}"
        return f"{st.strftime('%m/%d %H:%M')}–{en.strftime('%m/%d %H:%M')}"
    except:
        return "-"

# =========================
# image b64 helpers
# =========================
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
            if st.tzinfo is None: st = st.replace(tzinfo=KST)
            if en.tzinfo is None: en = en.replace(tzinfo=KST)
            st = st.astimezone(KST)
            en = en.astimezone(KST)
            if st <= t <= en:
                out.append(s)
        except:
            pass
    return out

# =========================
# Address search: Nominatim + (POI fallback) Overpass + reverse
# =========================
def overpass_poi_search(q: str, center=(36.0190, 129.3435), radius_m=30000, limit=12):
    q = (q or "").strip()
    if not q:
        return [], "⚠️ 검색어가 비었다."
    overpass_url = "https://overpass-api.de/api/interpreter"
    query = f"""
    [out:json][timeout:18];
    (
      nwr["name"~"{q}",i](around:{radius_m},{center[0]},{center[1]});
    );
    out center {limit};
    """
    try:
        r = requests.post(overpass_url, data=query.encode("utf-8"), timeout=20)
        if r.status_code == 429:
            return [], "⚠️ 장소 검색이 일시적으로 차단(429)되었다. 잠시 뒤 다시 시도해 달라."
        if r.status_code >= 400:
            return [], f"⚠️ 장소 검색 실패 (HTTP {r.status_code})"
        data = r.json()
    except Exception as e:
        return [], f"⚠️ 장소 검색 네트워크 오류: {type(e).__name__}"

    cands = []
    for el in (data.get("elements", []) or [])[:limit]:
        tags = el.get("tags", {}) or {}
        name = (tags.get("name") or "").strip()
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if not name or lat is None or lon is None:
            continue
        cands.append({"name": name, "lat": float(lat), "lng": float(lon)})
    if not cands:
        return [], "⚠️ 근처에서 해당 장소명을 찾지 못했다."
    return cands, ""

def nominatim_reverse(lat: float, lng: float):
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 18, "addressdetails": 1}
    headers = {"User-Agent":"oseyo-render/1.0 (gradio)", "Accept-Language":"ko-KR,ko;q=0.9,en;q=0.5"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=12)
        if r.status_code == 429:
            return None
        if r.status_code >= 400:
            return None
        js = r.json()
        return (js.get("display_name") or "").strip() or None
    except:
        return None

def nominatim_search(q: str, limit=12):
    q = (q or "").strip()
    if not q:
        return [], "⚠️ 주소/장소명을 입력해 달라."

    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": q, "format": "jsonv2", "limit": limit,
        "countrycodes": "kr", "addressdetails": 1, "namedetails": 1, "extratags": 1
    }
    headers = {"User-Agent":"oseyo-render/1.0 (gradio)", "Accept-Language":"ko-KR,ko;q=0.9,en;q=0.5"}

    data = None
    try:
        r = requests.get(url, params=params, headers=headers, timeout=12)
        if r.status_code == 429:
            return [], "⚠️ 주소 검색이 일시적으로 차단(429)되었다. 1~2분 뒤 다시 시도해 달라."
        if r.status_code >= 400:
            return [], f"⚠️ 주소 검색 실패 (HTTP {r.status_code})"
        data = r.json()
    except:
        data = None

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

    # 충분하면 바로 반환
    if len(cands) >= 3:
        return cands, ""

    # fallback: POI -> reverse 주소 제안
    poi_list, poi_err = overpass_poi_search(q, center=(36.0190, 129.3435), radius_m=30000, limit=limit)
    if poi_err:
        return (cands, "") if cands else ([], "⚠️ 검색 결과가 없다. 키워드를 바꿔 달라.")

    merged = list(cands)
    for poi in poi_list:
        addr = nominatim_reverse(poi["lat"], poi["lng"])
        if not addr:
            continue
        label = f"{poi['name']} — {addr}"
        merged.append({"label": label, "lat": poi["lat"], "lng": poi["lng"]})

    seen = set()
    uniq = []
    for c in merged:
        if c["label"] in seen:
            continue
        seen.add(c["label"])
        uniq.append(c)
        if len(uniq) >= limit:
            break
    return uniq, "" if uniq else "⚠️ 검색 결과가 없다. 키워드를 바꿔 달라."

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
            "⚠️ 주소 후보를 선택해 달라.",
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
            "⚠️ 선택한 주소를 다시 선택해 달라.",
            "", "", None, None,
            gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
            gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)
        )

    confirmed = chosen["label"]
    det = (detail or "").strip()
    lat, lng = chosen["lat"], chosen["lng"]

    return (
        "✅ 주소가 선택되었다.",
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
        folium.Marker(list(center), tooltip="지금은 열려 있는 곳이 없다").add_to(m)
    else:
        for s in items:
            try:
                cap = f"최대 {s['capacityMax']}명" if s.get("capacityEnabled") else "제한 없음"
                period = fmt_period(s["start"], s["end"])
                detail = (s.get("address_detail") or "").strip()
                detail_line = f"<div style='color:#6B7280;'>상세: {detail}</div>" if detail else ""
                photo_uri = b64_to_data_uri(s.get("photo_b64", ""))
                img_line = f"<img src='{photo_uri}' style='width:100%;height:140px;object-fit:cover;border-radius:12px;margin-bottom:8px;'/>" if photo_uri else ""
                popup = f"""
                <div style="font-family:system-ui;font-size:13px;width:280px;">
                  {img_line}
                  <div style="font-weight:900;margin-bottom:6px;">{s['title']}</div>
                  <div style="color:#111827;font-weight:900;">{period}</div>
                  <div style="color:#6B7280;margin-top:4px;">{s['address_confirmed']}</div>
                  {detail_line}
                  <div style="color:#6B7280;margin-top:4px;">{cap}</div>
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
          <div class="h">아직 열린 공간이 없다</div>
          <div class="p">오른쪽 아래 + 버튼으로 먼저 열면 된다</div>
        </div>
        """

    out = [banner]
    for s in items:
        period = fmt_period(s["start"], s["end"])
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
              <div class="period">{period}</div>
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
    now = now_kst().replace(second=0, microsecond=0)
    return (
        gr.update(visible=True),
        gr.update(visible=True),
        gr.update(visible=True),
        "",
        now,
        now + timedelta(minutes=30),
    )

def close_all():
    # 메인/주소 모달 모두 확실히 닫기 + 메시지 초기화
    return (
        gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
        gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
        ""
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
# Create space
# =========================
def create_space_and_close(
    activity_text,
    start_dt_val,
    end_dt_val,
    capacity_unlimited,
    cap_max,
    photo_np,
    addr_confirmed,
    addr_detail,
    addr_lat,
    addr_lng
):
    try:
        act = (activity_text or "").strip()
        if not act:
            return "⚠️ 활동을 입력해 달라.", render_home(), draw_map(), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

        if (not addr_confirmed) or (addr_lat is None) or (addr_lng is None):
            return "⚠️ 장소를 선택해 달라. (장소 검색하기)", render_home(), draw_map(), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

        st = normalize_dt(start_dt_val)
        en = normalize_dt(end_dt_val)

        if st is None or en is None:
            return "⚠️ 시작/종료 일시를 선택해 달라.", render_home(), draw_map(), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

        st = st.astimezone(KST)
        en = en.astimezone(KST)

        if en <= st:
            return "⚠️ 종료 일시는 시작 일시보다 뒤여야 한다.", render_home(), draw_map(), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

        new_id = uuid.uuid4().hex[:8]
        photo_b64 = image_np_to_b64(photo_np)

        capacityEnabled = (not bool(capacity_unlimited))
        capacityMax = None if not capacityEnabled else int(min(int(cap_max), 10))

        title = act if len(act) <= 24 else act[:24] + "…"

        new_space = {
            "id": new_id,
            "title": title,
            "photo_b64": photo_b64,
            "start": st.isoformat(),
            "end": en.isoformat(),
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

        return msg, render_home(), draw_map(), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

    except Exception as e:
        return f"❌ 등록 중 오류: {type(e).__name__}", render_home(), draw_map(), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

# =========================
# CSS (가로 스크롤 제거 + 모달/오버레이 잔상 방지)
# =========================
CSS = r"""
:root { --bg:#FAF9F6; --ink:#1F2937; --muted:#6B7280; --line:#E5E3DD; --card:#ffffffcc; --danger:#ef4444; }
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
.period{ font-size:14px; font-weight:900; color:#111827; margin:2px 0 8px; }
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

/* 지도 */
.mapWrap{ width:100vw; max-width:100vw; margin:0; padding:0; overflow:hidden; }
.mapFrame{ width:100vw; height: calc(100vh - 140px); border:0; border-radius:0; }

/* 오버레이 */
.oseyo_overlay{
  position:fixed !important;
  inset:0 !important;
  background:rgba(0,0,0,0.35) !important;
  z-index:99990 !important;
}

/* 모달 패널/푸터 fixed */
#main_sheet, #addr_sheet{
  position:fixed !important;
  left:50% !important; transform:translateX(-50%) !important;
  bottom:0 !important;
  width:min(420px,96vw) !important;
  height:88vh !important;
  overflow-y:auto !important;
  overflow-x:hidden !important; /* ✅ 가로 스크롤 차단 */
  background:var(--bg) !important;
  border:1px solid var(--line) !important; border-bottom:0 !important;
  border-radius:26px 26px 0 0 !important;
  padding:22px 16px 160px 16px !important;
  z-index:99991 !important;
  box-shadow:0 -12px 40px rgba(0,0,0,0.25) !important;
}

#main_footer, #addr_footer{
  position:fixed !important;
  left:50% !important; transform:translateX(-50%) !important;
  bottom:0 !important;
  width:min(420px,96vw) !important;
  padding:12px 16px 16px 16px !important;
  background:rgba(250,249,246,0.98) !important;
  border-top:1px solid var(--line) !important;
  z-index:99992 !important;
}

/* ✅ 모달 내부 가로 넘침 방지 (Row가 원인인 경우가 많다) */
#main_sheet *, #addr_sheet *{ max-width:100% !important; box-sizing:border-box !important; }
#main_sheet .gr-row, #addr_sheet .gr-row{ flex-wrap:wrap !important; }
#main_sheet .gr-row > *, #addr_sheet .gr-row > *{ min-width:0 !important; }
#main_sheet .wrap, #addr_sheet .wrap, #main_sheet .prose, #addr_sheet .prose{
  overflow-x:hidden !important;
  word-break:break-word !important;
}
#main_sheet img, #addr_sheet img{ max-width:100% !important; height:auto !important; }

/* FAB */
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

# =========================
# UI
# =========================
with gr.Blocks(title="Oseyo (DB)") as demo:
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

    # overlays (내용 있는 div로 고정)
    main_overlay = gr.HTML("<div class='oseyo_overlay'></div>", visible=False)
    addr_overlay = gr.HTML("<div class='oseyo_overlay'></div>", visible=False)

    # modal containers (fixed by elem_id)
    main_sheet = gr.Column(visible=False, elem_id="main_sheet")
    main_footer = gr.Row(visible=False, elem_id="main_footer")

    addr_sheet = gr.Column(visible=False, elem_id="addr_sheet")
    addr_footer = gr.Row(visible=False, elem_id="addr_footer")

    with main_sheet:
        gr.HTML("<div style='font-size:22px;font-weight:900;color:#1F2937;margin:0 0 10px 0;'>열어놓기</div>")
        photo_np = gr.Image(label="사진(선택)", type="numpy")

        with gr.Row():
            activity_text = gr.Textbox(label="활동", placeholder="예: 산책, 커피, 스터디…", lines=1, scale=9)
            add_act_btn = gr.Button("추가", scale=1)

        fav_msg = gr.Markdown("")
        fav_radio = gr.Radio(choices=[], value=None, label="자주 하는 활동(선택)")

        # DateTime (환경에 따라 없을 수 있어 안전장치)
        if hasattr(gr, "DateTime"):
            start_dt = gr.DateTime(label="시작 일시", include_time=True)
            end_dt   = gr.DateTime(label="종료 일시", include_time=True)
        else:
            start_dt = gr.Textbox(label="시작 일시", placeholder="YYYY-MM-DD HH:MM", lines=1)
            end_dt   = gr.Textbox(label="종료 일시", placeholder="YYYY-MM-DD HH:MM", lines=1)

        capacity_unlimited = gr.Checkbox(value=True, label="제한 없음")
        cap_max = gr.Slider(1, 10, value=4, step=1, label="최대 인원(제한 있을 때)")

        chosen_place_view = gr.Markdown("**선택된 장소:** *(아직 없음)*")
        open_addr_btn = gr.Button("장소 검색하기")
        main_msg = gr.Markdown("")

    with main_footer:
        main_close = gr.Button("닫기")
        main_create = gr.Button("완료", elem_classes=["primary"])

    with addr_sheet:
        gr.HTML("<div style='font-size:22px;font-weight:900;color:#1F2937;margin:0 0 10px 0;'>장소 검색</div>")

        addr_query = gr.Textbox(label="주소/장소명", placeholder="예: 포항근로복지공단, 포항시청, 영일대 …", lines=1)
        addr_search_btn = gr.Button("검색")
        addr_err = gr.Markdown("")
        chosen_text = gr.Markdown("선택: 없음")

        addr_radio = gr.Radio(choices=[], value=None, label="주소 후보(선택)")
        addr_detail_in = gr.Textbox(label="상세(선택)", placeholder="예: 2층 203호 …", lines=1)
        addr_msg = gr.Markdown("")

    with addr_footer:
        addr_back = gr.Button("뒤로")
        addr_confirm_btn = gr.Button("주소 선택 완료", elem_classes=["primary"])

    # load / refresh
    demo.load(fn=render_home, inputs=None, outputs=home_html)
    demo.load(fn=draw_map, inputs=None, outputs=map_html)
    refresh_btn.click(fn=render_home, inputs=None, outputs=home_html)
    map_refresh.click(fn=draw_map, inputs=None, outputs=map_html)

    # open main modal
    fab.click(fn=open_main_sheet, inputs=None, outputs=[main_overlay, main_sheet, main_footer, main_msg, start_dt, end_dt])

    # close modal (확실히 전체 닫기)
    main_close.click(
        fn=close_all,
        inputs=None,
        outputs=[main_overlay, main_sheet, main_footer, addr_overlay, addr_sheet, addr_footer, main_msg]
    )

    # favorites
    add_act_btn.click(fn=add_favorite, inputs=[favs, activity_text], outputs=[favs, fav_msg, fav_radio])
    fav_radio.change(fn=use_favorite, inputs=[fav_radio], outputs=[activity_text])

    # open address modal
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

    # back to main from addr
    addr_back.click(
        fn=back_to_main_from_addr,
        inputs=None,
        outputs=[
            main_overlay, main_sheet, main_footer,
            addr_overlay, addr_sheet, addr_footer,
            addr_msg
        ]
    )

    # address search
    addr_search_btn.click(
        fn=addr_do_search,
        inputs=[addr_query],
        outputs=[addr_candidates, addr_radio, addr_err, chosen_text, chosen_label]
    )
    addr_radio.change(fn=on_radio_change, inputs=[addr_radio], outputs=[chosen_text, chosen_label])

    # confirm address
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

    # create
    main_create.click(
        fn=create_space_and_close,
        inputs=[
            activity_text,
            start_dt,
            end_dt,
            capacity_unlimited,
            cap_max,
            photo_np,
            addr_confirmed,
            addr_detail,
            addr_lat,
            addr_lng
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
