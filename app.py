import os, time, uuid, base64, io, sqlite3
from datetime import datetime, timedelta

import requests
import folium
import numpy as np
from PIL import Image
import gradio as gr

# =========================
# timezone (KST)
# =========================
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

def image_to_b64(img):
    if img is None:
        return ""
    try:
        if isinstance(img, Image.Image):
            im = img.convert("RGB")
        else:
            arr = img
            if arr.dtype != np.uint8:
                arr = np.clip(arr, 0, 255).astype("uint8")
            if len(arr.shape) == 3 and arr.shape[2] == 4:
                im = Image.fromarray(arr, "RGBA").convert("RGB")
            else:
                im = Image.fromarray(arr).convert("RGB")

        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except:
        return ""

def b64_to_data_uri(b64_str):
    return f"data:image/jpeg;base64,{b64_str}" if b64_str else ""

# =========================
# DB path 결정 (영구저장 우선)
# =========================
def _dir_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        testfile = os.path.join(path, ".write_test")
        with open(testfile, "w") as f:
            f.write("ok")
        os.remove(testfile)
        return True
    except:
        return False

# Render Disk가 /var/data 로 붙으면 여기 writable 됨
PREFERRED_DIR = os.getenv("OSEYO_DATA_DIR", "/var/data")
if _dir_writable(PREFERRED_DIR):
    DATA_DIR = PREFERRED_DIR
    PERSISTENT = True
else:
    DATA_DIR = "/tmp/oseyo"
    os.makedirs(DATA_DIR, exist_ok=True)
    PERSISTENT = False

DB_PATH = os.path.join(DATA_DIR, "oseyo.db")

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def db_init():
    con = db_conn()
    cur = con.cursor()
    cur.execute("""
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
    cur.execute("CREATE INDEX IF NOT EXISTS idx_spaces_time ON spaces(start_iso, end_iso);")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS favorites (
        activity TEXT PRIMARY KEY
    );
    """)
    con.commit()
    con.close()

db_init()

def db_list_spaces(limit=300):
    con = db_conn()
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("""
        SELECT * FROM spaces
        WHERE hidden = 0
        ORDER BY created_at DESC
        LIMIT ?
    """, (int(limit),))
    rows = [dict(r) for r in cur.fetchall()]
    con.close()

    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "title": r["title"],
            "photo_b64": r.get("photo_b64") or "",
            "start": r["start_iso"],
            "end": r["end_iso"],
            "address_confirmed": r["address_confirmed"],
            "address_detail": r.get("address_detail") or "",
            "lat": float(r["lat"]),
            "lng": float(r["lng"]),
            "capacityEnabled": bool(r["capacity_enabled"]),
            "capacityMax": r["capacity_max"],
            "hidden": bool(r["hidden"]),
        })
    return out

def db_insert_space(space: dict):
    con = db_conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO spaces (
            id, title, photo_b64, start_iso, end_iso,
            address_confirmed, address_detail, lat, lng,
            capacity_enabled, capacity_max, hidden, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        space["id"],
        space["title"],
        space.get("photo_b64",""),
        space["start"],
        space["end"],
        space["address_confirmed"],
        space.get("address_detail",""),
        float(space["lat"]),
        float(space["lng"]),
        1 if space.get("capacityEnabled") else 0,
        None if space.get("capacityMax") is None else int(space["capacityMax"]),
        1 if space.get("hidden") else 0,
        now_kst().isoformat()
    ))
    con.commit()
    con.close()

def db_delete_space(space_id: str) -> bool:
    con = db_conn()
    cur = con.cursor()
    cur.execute("DELETE FROM spaces WHERE id = ?", (space_id,))
    ok = (cur.rowcount > 0)
    con.commit()
    con.close()
    return ok

def db_list_favorites():
    con = db_conn()
    cur = con.cursor()
    cur.execute("SELECT activity FROM favorites ORDER BY activity ASC")
    favs = [r[0] for r in cur.fetchall()]
    con.close()
    return favs

def db_add_favorite(act: str):
    con = db_conn()
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO favorites(activity) VALUES(?)", (act,))
    con.commit()
    con.close()

# =========================
# 핵심 로직
# =========================
HOURS = list(range(0, 13))
MINS  = [0, 15, 30, 45]

def active_spaces(spaces):
    t = now_kst()
    out = []
    for s in spaces or []:
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
    for c in cands or []:
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
                img_line = f"<img src='{photo_uri}' style='width:100%;height:140px;object-fit:cover;border-radius:12px;margin-bottom:8px;'/>" if photo_uri else ""
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
    </div>
    """

def draw_map_from_db():
    spaces = db_list_spaces()
    return make_map_html(active_spaces(spaces), center=(36.0190, 129.3435), zoom=13)

# =========================
# 카드 렌더 (삭제 버튼 포함)
# =========================
def banner_html():
    if PERSISTENT:
        return f"""
        <div class="banner ok">
          ✅ 영구저장 모드이다 (DB: <b>{DB_PATH}</b>). 새로고침해도 이벤트가 유지된다.
        </div>
        """
    return f"""
    <div class="banner warn">
      ⚠️ 임시저장 모드이다 (DB: <b>{DB_PATH}</b>). <b>Render Disk를 /var/data로 붙이지 않으면</b> 새로고침/재시작 시 이벤트가 사라질 수 있다.
    </div>
    """

def render_home_from_db():
    spaces = db_list_spaces()
    items = active_spaces(spaces)

    top = banner_html()
    if not items:
        return top + """
        <div class="card empty">
          <div class="h">아직 열린 공간이 없습니다</div>
          <div class="p">오른쪽 아래 + 버튼으로 먼저 열어보면 된다</div>
        </div>
        """

    out = [top]
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
        img = f"<img class='thumb' src='{photo_uri}' alt='photo' />" if photo_uri else "<div class='thumb placeholder'></div>"

        # 삭제 버튼: /api/delete?id=... 호출 후 새로고침
        out.append(f"""
        <div class="card rowcard">
          <div class="left">
            <div class="title">{s['title']}</div>
            <div class="muted">오늘 {st}–{en}</div>
            <div class="muted">{s['address_confirmed']}</div>
            {detail_line}
            <div class="muted">{cap}</div>
            <div class="idline">ID: {s['id']}</div>
            <div class="actions">
              <button class="btn-del" onclick="oseyoDelete('{s['id']}')">삭제</button>
            </div>
          </div>
          <div class="right">
            {img}
          </div>
        </div>
        """)
    return "\n".join(out)

# =========================
# create / favorites / UI 핸들러
# =========================
def open_main_sheet():
    return (gr.update(visible=True), gr.update(visible=True), gr.update(visible=True), "", fmt_hm(now_kst()))

def close_main_sheet():
    return (gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), "")

def open_addr_sheet():
    return (
        gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
        gr.update(visible=True),  gr.update(visible=True),  gr.update(visible=True),
        [], gr.update(choices=[], value=None), "", "선택: 없음", "", "", ""
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

def add_favorite_db(activity_text):
    act = (activity_text or "").strip()
    if not act:
        favs = db_list_favorites()
        return "⚠️ 활동을 입력한 뒤 추가하면 된다.", gr.update(choices=favs, value=None)
    db_add_favorite(act)
    favs = db_list_favorites()
    return f"✅ '{act}'을(를) 자주 하는 활동에 추가했다.", gr.update(choices=favs, value=None)

def use_favorite(label):
    if not label:
        return gr.update()
    return gr.update(value=label)

def create_space_db(
    activity_text, start_hm, dur_h, dur_m, capacity_unlimited, cap_max,
    photo_pil, addr_confirmed, addr_detail, addr_lat, addr_lng
):
    act = (activity_text or "").strip()
    if not act:
        return "⚠️ 활동을 입력해 달라.", render_home_from_db(), draw_map_from_db(), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

    if not addr_confirmed or addr_lat is None or addr_lng is None:
        return "⚠️ 장소를 선택해 달라. (장소 검색하기)", render_home_from_db(), draw_map_from_db(), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

    total_min = int(dur_h) * 60 + int(dur_m)
    if total_min <= 0:
        return "⚠️ 지속시간을 0분보다 크게 설정해 달라.", render_home_from_db(), draw_map_from_db(), gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

    t_now = now_kst().replace(second=0, microsecond=0)
    t = t_now
    parsed = safe_parse_hm(start_hm)
    if parsed is not None:
        h, m = parsed
        t = t.replace(hour=h, minute=m)
        if t < t_now:
            t = t_now

    end = t + timedelta(minutes=total_min)
    new_id = uuid.uuid4().hex[:8]
    photo_b64 = image_to_b64(photo_pil)

    capacity_enabled = (not bool(capacity_unlimited))
    capacity_max = None if not capacity_enabled else int(min(int(cap_max), 10))

    if int(dur_h) > 0 and int(dur_m) > 0:
        title = f"{int(dur_h)}시간 {int(dur_m)}분 {act}"
    elif int(dur_h) > 0:
        title = f"{int(dur_h)}시간 {act}"
    else:
        title = f"{int(dur_m)}분 {act}"

    space = {
        "id": new_id,
        "title": title,
        "photo_b64": photo_b64,
        "start": t.isoformat(),
        "end": end.isoformat(),
        "address_confirmed": addr_confirmed,
        "address_detail": (addr_detail or "").strip(),
        "lat": float(addr_lat),
        "lng": float(addr_lng),
        "capacityEnabled": bool(capacity_enabled),
        "capacityMax": capacity_max,
        "hidden": False,
    }
    db_insert_space(space)

    msg = f"✓ '{title}'이(가) 열렸다. (ID: {new_id})"
    return msg, render_home_from_db(), draw_map_from_db(), gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)

# =========================
# CSS + JS (FAB / 삭제)
# =========================
CSS = r"""
:root { --bg:#FAF9F6; --ink:#1F2937; --muted:#6B7280; --line:#E5E3DD; --accent:#111; --card:#ffffffcc; --orange:#FF6A00; --danger:#ef4444; }

html, body { width:100%; max-width:100%; overflow-x:hidden !important; }
.gradio-container { background: var(--bg) !important; width:100% !important; max-width:100% !important; overflow-x:hidden !important; }
* { box-sizing: border-box !important; }
.gradio-container * { max-width:100% !important; }

.banner{ max-width: 900px; margin: 10px auto 6px; padding: 10px 12px; border-radius: 14px; font-size: 13px; line-height:1.5; }
.banner.ok{ background:#ecfdf5; border:1px solid #a7f3d0; color:#065f46; }
.banner.warn{ background:#fff7ed; border:1px solid #fed7aa; color:#9a3412; }

.card{ background: var(--card); border: 1px solid var(--line); border-radius: 18px; padding: 14px; margin: 12px auto; max-width: 900px; }
.card.empty{ max-width: 600px; }
.h{ font-size: 16px; font-weight: 900; color: var(--ink); margin-bottom: 6px; }
.p{ font-size: 13px; color: var(--muted); line-height: 1.6; }

.rowcard{ display:flex; gap: 14px; align-items: stretch; }
.rowcard .left{ flex: 1; min-width: 0; display:flex; flex-direction:column; }
.rowcard .right{ width: 180px; flex: 0 0 180px; display:flex; align-items:center; justify-content:center; }
.title{ font-size: 16px; font-weight: 900; color: var(--ink); margin-bottom: 6px; }
.muted{ font-size: 13px; color: var(--muted); line-height: 1.55; }
.idline{ margin-top: 8px; font-size: 12px; color:#9CA3AF; }
.actions{ margin-top: auto; display:flex; justify-content:flex-end; padding-top: 10px; }
.btn-del{
  appearance:none; border:0; cursor:pointer;
  background: var(--danger); color:#fff;
  font-weight:900; font-size:13px;
  padding: 10px 12px;
  border-radius: 12px;
}

.thumb{
  width: 100%;
  height: 130px;
  object-fit: cover;
  border-radius: 14px;
  border: 1px solid var(--line);
  display:block;
  background:#fff;
}
.thumb.placeholder{
  width: 100%;
  height: 130px;
  border-radius: 14px;
  border: 1px dashed var(--line);
  background: rgba(255,255,255,0.6);
}

/* 지도 탭 화면 꽉차게 */
.mapWrap{ width: 100vw; max-width: 100vw; margin: 0; padding: 0; overflow:hidden; }
.mapFrame{ width: 100vw; height: calc(100vh - 180px); border: 0; border-radius: 0; }

/* 모달 */
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
.oseyo_panel *{ overflow: visible !important; scrollbar-width: none !important; -ms-overflow-style:none !important; }
.oseyo_panel *::-webkit-scrollbar{ width:0px !important; height:0px !important; }

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

/* 진짜 FAB(오른쪽 하단 고정 원형) */
#oseyo_fab_ui{
  position: fixed;
  right: 18px;
  bottom: 18px;
  width: 58px;
  height: 58px;
  border-radius: 50%;
  border: 0;
  background: var(--accent);
  color: #FAF9F6;
  font-size: 34px;
  font-weight: 900;
  line-height: 58px;
  text-align:center;
  cursor:pointer;
  z-index: 200000;
  box-shadow: 0 14px 30px rgba(0,0,0,0.35);
}
"""

JS = r"""
<script>
window.oseyoDelete = async function(id){
  if(!confirm("이 이벤트를 삭제하겠습니까?")) return;
  try{
    const res = await fetch(`/api/delete?id=${encodeURIComponent(id)}`, {method:"POST"});
    if(!res.ok){
      alert("삭제 실패");
      return;
    }
    // Gradio state 리렌더가 아니라 "DB 기반"이라 그냥 새로고침하면 됨
    location.reload();
  }catch(e){
    alert("삭제 실패(네트워크)");
  }
}

window.oseyoOpenModal = function(){
  const btn = document.querySelector("#fab_hidden button");
  if(btn) btn.click();
}
</script>
"""

# =========================
# Gradio UI
# =========================
with gr.Blocks(css=CSS, title="Oseyo (DB)") as demo:
    # FastAPI app (for delete API)
    app = demo.app

    # place states
    addr_confirmed = gr.State("")
    addr_detail = gr.State("")
    addr_lat = gr.State(None)
    addr_lng = gr.State(None)

    # address modal states
    addr_candidates = gr.State([])
    chosen_label = gr.State("")

    gr.HTML(JS)

    gr.HTML("""
    <div style="max-width:900px;margin:0 auto;padding:16px 10px 6px;text-align:center;">
      <div style="font-size:26px;font-weight:900;color:#1F2937;letter-spacing:-0.2px;">지금, 열려 있습니다</div>
      <div style="margin-top:6px;font-size:13px;color:#6B7280;">원하시면 오세요</div>
    </div>
    """)

    with gr.Tabs():
        with gr.Tab("탐색"):
            home_html = gr.HTML(value=render_home_from_db())
            refresh_btn = gr.Button("새로고침")
        with gr.Tab("지도"):
            map_refresh = gr.Button("지도 새로고침")
            map_html = gr.HTML(value=draw_map_from_db())

    # 화면 위에 FAB UI만 올리고, 실제 동작은 숨겨진 gr.Button으로 트리거
    gr.HTML("<button id='oseyo_fab_ui' onclick='oseyoOpenModal()'>+</button>")
    fab_hidden = gr.Button("FAB", elem_id="fab_hidden", visible=False)

    # ===== MAIN MODAL =====
    main_overlay = gr.HTML("<div class='oseyo_overlay'></div>", visible=False)
    with gr.Group(visible=False, elem_classes=["oseyo_panel"]) as main_sheet:
        gr.HTML("<div style='font-size:22px;font-weight:900;color:#1F2937;margin:0 0 10px 0;'>열어놓기</div>")
        gr.HTML("<div style='font-size:13px;color:#6B7280;line-height:1.7;margin:0 0 16px 0;'>아래 내용을 입력해 주세요. (사진은 선택사항입니다)</div>")

        photo_pil = gr.Image(label="사진(선택)", type="pil")

        with gr.Row():
            activity_text = gr.Textbox(label="활동", placeholder="예: 산책, 커피, 스터디…", lines=1, scale=9)
            add_act_btn = gr.Button("추가", scale=1)

        fav_msg = gr.Markdown("")
        fav_radio = gr.Radio(choices=db_list_favorites(), value=None, label="자주 하는 활동(선택하면 활동칸에 입력됨)")

        start_hm = gr.Textbox(label="시작시간(선택)", placeholder="미입력 시 현재 시각 자동", lines=1)

        with gr.Row():
            dur_h = gr.Dropdown(choices=HOURS, value=0, label="지속 시간(시간)")
            dur_m = gr.Dropdown(choices=MINS, value=30, label="지속 시간(분)")

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

    # ===== API: delete =====
    @app.post("/api/delete")
    def api_delete(id: str):
        ok = db_delete_space((id or "").strip())
        return {"ok": ok}

    # ===== wiring =====
    refresh_btn.click(fn=render_home_from_db, inputs=None, outputs=home_html)
    map_refresh.click(fn=draw_map_from_db, inputs=None, outputs=map_html)

    fab_hidden.click(fn=open_main_sheet, inputs=None, outputs=[main_overlay, main_sheet, main_footer, main_msg, start_hm])
    main_close.click(fn=close_main_sheet, inputs=None, outputs=[main_overlay, main_sheet, main_footer, main_msg])

    add_act_btn.click(fn=add_favorite_db, inputs=[activity_text], outputs=[fav_msg, fav_radio])
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
        outputs=[main_overlay, main_sheet, main_footer, addr_overlay, addr_sheet, addr_footer, addr_msg]
    )

    addr_search_btn.click(
        fn=addr_do_search,
        inputs=[addr_query],
        outputs=[addr_candidates, addr_radio, addr_err, chosen_text, chosen_label]
    )

    addr_radio.change(fn=on_radio_change, inputs=[addr_radio], outputs=[chosen_text, chosen_label])

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
        fn=create_space_db,
        inputs=[activity_text, start_hm, dur_h, dur_m, capacity_unlimited, cap_max, photo_pil,
                addr_confirmed, addr_detail, addr_lat, addr_lng],
        outputs=[main_msg, home_html, map_html, main_overlay, main_sheet, main_footer]
    )

# Render용 실행
if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=int(os.getenv("PORT", "7860")))
