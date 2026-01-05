# =========================================================
# OSEYO — HARD FIX (Gradio 4.44+ / Render)
# Fix:
# - DateTime picker 안 뜸: flatpickr position:fixed + z-index + transform 방지
# - 선택했는데도 "일시 선택" 뜸/등록 안됨: normalize_dt() Z/ms/dict/list/timestamp 전부 처리
# - 모달 가로 스크롤 제거: box-sizing/max-width/overflow-x 강제 + row wrap 정리
# - + 버튼이 완료 가림: 모달 열리면 FAB 숨김
# - 지도: data: iframe 차단 대비 -> FastAPI가 /kakao_map HTML 직접 서빙
# - 즐겨찾기 활동: 추가/선택 가능, DB 저장
# =========================================================

import os, uuid, base64, io, sqlite3, json, traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image
import gradio as gr

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse

# -------------------------
# CONFIG
# -------------------------
KST = ZoneInfo("Asia/Seoul")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def now_kst():
    return datetime.now(KST)

def _clean_iso(s: str) -> str:
    """
    다양한 ISO/날짜 문자열을 fromisoformat에 맞게 정리
    - 공백 -> T
    - ...Z -> ...+00:00
    - 밀리초 포함 + Z 처리
    """
    s = (s or "").strip()
    if not s:
        return ""
    s = s.replace(" ", "T")
    # Z(UTC) 처리
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return s

def normalize_dt(v):
    """
    gr.DateTime 값이 환경/브라우저/버전에 따라 형태가 다름.
    - datetime
    - "2026-01-05 15:40:00"
    - "2026-01-05T15:40:00"
    - "2026-01-05T06:40:00.000Z" 같은 Z 포함
    - {"value": "..."} dict
    - [..] list/tuple
    - epoch timestamp (ms/s)
    전부 datetime(KST)로 변환.
    """
    if v is None:
        return None

    if isinstance(v, dict):
        v = v.get("value") or v.get("data") or v.get("datetime") or v.get("date") or v.get("time") or None
        if v is None:
            return None

    if isinstance(v, (list, tuple)):
        if len(v) == 0:
            return None
        v = v[0]

    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=KST)

    if isinstance(v, (int, float)):
        try:
            if v > 1e12:  # ms
                v = v / 1000.0
            return datetime.fromtimestamp(v, tz=KST)
        except:
            return None

    if isinstance(v, str):
        s = _clean_iso(v)
        if not s:
            return None
        # 밀리초가 너무 길게 붙어 fromisoformat 실패하는 경우 대비
        # 예: 2026-01-05T06:40:00.000000+00:00 -> OK
        # 예외 나면 밀리초 부분을 잘라 재시도
        try:
            dt = datetime.fromisoformat(s)
        except:
            try:
                # . 이후 밀리초 부분을 6자리까지만
                if "." in s:
                    head, tail = s.split(".", 1)
                    # tail에 timezone이 섞여 있을 수 있음
                    tz = ""
                    frac = tail
                    if "+" in tail:
                        frac, tz = tail.split("+", 1)
                        tz = "+" + tz
                    elif "-" in tail[1:]:
                        # timezone negative (주의: 날짜 '-'랑 구분 위해 tail[1:])
                        parts = tail.split("-", 1)
                        frac = parts[0]
                        tz = "-" + parts[1]
                    frac = "".join([c for c in frac if c.isdigit()])[:6]
                    s2 = head + "." + (frac if frac else "0") + tz
                    dt = datetime.fromisoformat(s2)
                else:
                    return None
            except:
                return None

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return dt.astimezone(KST)

    return None

def fmt_period(st_iso: str, en_iso: str) -> str:
    try:
        st = datetime.fromisoformat(_clean_iso(st_iso))
        en = datetime.fromisoformat(_clean_iso(en_iso))
        if st.tzinfo is None: st = st.replace(tzinfo=KST)
        if en.tzinfo is None: en = en.replace(tzinfo=KST)
        st = st.astimezone(KST); en = en.astimezone(KST)
        if st.date() == en.date():
            return f"{st:%m/%d} {st:%H:%M}–{en:%H:%M}"
        return f"{st:%m/%d %H:%M}–{en:%m/%d %H:%M}"
    except:
        return "-"

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

# -------------------------
# DB
# -------------------------
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
            address_confirmed TEXT NOT NULL,
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
            address_confirmed, address_detail, lat, lng,
            capacity_enabled, capacity_max, hidden, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            space["id"],
            space["title"],
            space.get("photo_b64",""),
            space["start_iso"],
            space["end_iso"],
            space["address_confirmed"],
            space.get("address_detail",""),
            float(space["lat"]),
            float(space["lng"]),
            1 if space.get("capacityEnabled") else 0,
            space.get("capacityMax"),
            1 if space.get("hidden") else 0,
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
                   address_confirmed, address_detail, lat, lng,
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
            "address_confirmed": r[5] or "",
            "address_detail": r[6] or "",
            "lat": float(r[7]) if r[7] is not None else None,
            "lng": float(r[8]) if r[8] is not None else None,
            "capacityEnabled": bool(r[9]) if r[9] is not None else False,
            "capacityMax": r[10],
            "hidden": bool(r[11]) if r[11] is not None else False,
            "created_at": r[12] or "",
        })
    return out

def active_spaces(spaces):
    t = now_kst()
    out=[]
    for s in spaces:
        if s.get("hidden"):
            continue
        if s.get("lat") is None or s.get("lng") is None:
            continue
        try:
            st = datetime.fromisoformat(_clean_iso(s["start_iso"]))
            en = datetime.fromisoformat(_clean_iso(s["end_iso"]))
            if st.tzinfo is None: st = st.replace(tzinfo=KST)
            if en.tzinfo is None: en = en.replace(tzinfo=KST)
            st = st.astimezone(KST); en = en.astimezone(KST)
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
    a = (activity or "").strip()
    if not a:
        return
    with db_conn() as con:
        con.execute("INSERT OR IGNORE INTO favorites(activity, created_at) VALUES (?,?)", (a, now_kst().isoformat()))
        con.commit()

# -------------------------
# Kakao keyword search (REST)
# -------------------------
def kakao_keyword_search(q: str, size=15):
    q = (q or "").strip()
    if not q:
        return [], "⚠️ 장소/주소를 입력해 달라."
    if not KAKAO_REST_API_KEY:
        return [], "⚠️ KAKAO_REST_API_KEY가 없다. (JS 키 말고 REST 키)"

    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params = {"query": q, "size": size}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=12)
        if r.status_code == 429:
            return [], "⚠️ 카카오 검색 제한(429). 잠시 후 다시."
        if r.status_code >= 400:
            body = (r.text or "")[:300]
            return [], f"⚠️ 카카오 검색 실패 (HTTP {r.status_code})\n응답: {body}"
        data = r.json()
    except Exception as e:
        return [], f"⚠️ 네트워크 오류: {type(e).__name__}"

    cands=[]
    for d in (data.get("documents") or []):
        place = (d.get("place_name") or "").strip()
        road = (d.get("road_address_name") or "").strip()
        addr = (d.get("address_name") or "").strip()
        lat = d.get("y"); lng = d.get("x")
        if not place or lat is None or lng is None:
            continue
        best_addr = road or addr
        label = f"{place} — {best_addr}" if best_addr else place
        try:
            cands.append({"label": label, "lat": float(lat), "lng": float(lng)})
        except:
            pass

    if not cands:
        return [], "⚠️ 검색 결과가 없다."
    return cands, ""

# -------------------------
# Home
# -------------------------
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
        period = fmt_period(s["start_iso"], s["end_iso"])
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

# -------------------------
# Map: served by FastAPI
# -------------------------
def map_points_payload():
    spaces = db_list_spaces()
    items = active_spaces(spaces)
    points = []
    for s in items:
        points.append({
            "title": s["title"],
            "lat": s["lat"],
            "lng": s["lng"],
            "addr": s.get("address_confirmed",""),
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
# Modal control
# -------------------------
def open_modal():
    st = now_kst().replace(second=0, microsecond=0)
    en = st + timedelta(minutes=30)
    return (
        gr.update(visible=True),    # overlay
        gr.update(visible=True),    # sheet
        gr.update(visible=True),    # footer
        gr.update(visible=True),    # main_view
        gr.update(visible=False),   # addr_view
        "",                         # msg_main
        "",                         # msg_addr
        st,                         # start_dt
        en,                         # end_dt
        gr.update(visible=False),   # fab hide
    )

def close_modal():
    return (
        gr.update(visible=False),
        gr.update(visible=False),
        gr.update(visible=False),
        gr.update(visible=True),
        gr.update(visible=False),
        "",
        "",
        gr.update(visible=True),
    )

def goto_addr():
    return (
        gr.update(visible=False),
        gr.update(visible=True),
        [],
        gr.update(choices=[], value=None),
        "선택: 없음",
        "",
    )

def back_to_main():
    return (
        gr.update(visible=True),
        gr.update(visible=False),
        "",
    )

# -------------------------
# Address flow
# -------------------------
def addr_search(query):
    cands, err = kakao_keyword_search(query, size=15)
    if err:
        return (cands, gr.update(choices=[], value=None), "선택: 없음", err)

    labels = [c["label"] for c in cands]
    default = labels[0] if labels else None
    chosen = f"선택: {default}" if default else "선택: 없음"
    return (cands, gr.update(choices=labels, value=default), chosen, "")

def on_pick(label):
    return f"선택: {label}" if label else "선택: 없음"

def confirm_addr(cands, picked_label, detail_text):
    picked_label = (picked_label or "").strip()
    if not picked_label:
        return ("⚠️ 주소를 선택해 달라.", "", "", None, None, gr.update(visible=False), gr.update(visible=True))

    chosen = None
    for c in (cands or []):
        if c.get("label") == picked_label:
            chosen = c
            break
    if not chosen:
        return ("⚠️ 선택값이 꼬였다. 다시 검색해 달라.", "", "", None, None, gr.update(visible=False), gr.update(visible=True))

    confirmed = chosen["label"]
    det = (detail_text or "").strip()
    return ("✅ 주소가 입력되었다.", confirmed, det, chosen["lat"], chosen["lng"], gr.update(visible=True), gr.update(visible=False))

def show_chosen_place(addr_confirmed, addr_detail):
    if not addr_confirmed:
        return "**선택된 장소:** *(아직 없음)*"
    if addr_detail:
        return f"**선택된 장소:** {addr_confirmed}\n\n상세: {addr_detail}"
    return f"**선택된 장소:** {addr_confirmed}"

# -------------------------
# Favorites
# -------------------------
def load_favs():
    return gr.update(choices=db_list_favorites(), value=None)

def add_fav_from_activity(activity_text):
    a = (activity_text or "").strip()
    if not a:
        return "⚠️ 즐겨찾기에 추가할 활동을 먼저 입력해 달라.", load_favs()
    db_add_favorite(a)
    return f"✅ '{a}' 즐겨찾기에 추가했다.", load_favs()

def pick_fav_to_activity(fav_value):
    return fav_value or ""

# -------------------------
# Create event
# -------------------------
def create_event(activity_text, start_dt_val, end_dt_val, capacity_unlimited, cap_max, photo_np,
                 addr_confirmed, addr_detail, addr_lat, addr_lng):
    act = (activity_text or "").strip()
    if not act:
        return "⚠️ 활동을 입력해 달라.", render_home(), draw_map()

    if (not addr_confirmed) or (addr_lat is None) or (addr_lng is None):
        return "⚠️ 장소를 선택해 달라. (장소 검색하기)", render_home(), draw_map()

    st = normalize_dt(start_dt_val)
    en = normalize_dt(end_dt_val)

    if st is None or en is None:
        # 원인 파악용(너한테 보이게) - 너무 길게는 안 띄움
        raw_s = str(start_dt_val)[:80]
        raw_e = str(end_dt_val)[:80]
        return f"⚠️ 시작/종료 일시를 선택해 달라.\n(디버그: start={raw_s} / end={raw_e})", render_home(), draw_map()

    st = st.astimezone(KST); en = en.astimezone(KST)
    if en <= st:
        return "⚠️ 종료 일시는 시작 일시보다 뒤여야 한다.", render_home(), draw_map()

    new_id = uuid.uuid4().hex[:8]
    photo_b64 = image_np_to_b64(photo_np)

    capacityEnabled = (not bool(capacity_unlimited))
    cap_max_val = None
    if capacityEnabled:
        try:
            cap_max_val = int(cap_max)
        except:
            cap_max_val = 4
        cap_max_val = max(1, min(cap_max_val, 10))

    title = act if len(act) <= 24 else act[:24] + "…"

    try:
        db_insert_space({
            "id": new_id,
            "title": title,
            "photo_b64": photo_b64,
            "start_iso": st.isoformat(),
            "end_iso": en.isoformat(),
            "address_confirmed": addr_confirmed,
            "address_detail": (addr_detail or "").strip(),
            "lat": float(addr_lat),
            "lng": float(addr_lng),
            "capacityEnabled": capacityEnabled,
            "capacityMax": cap_max_val,
            "hidden": False,
        })
    except Exception:
        err = traceback.format_exc().splitlines()[-1]
        return f"⚠️ DB 저장 실패: {err}", render_home(), draw_map()

    return f"✅ 등록 완료: '{title}'", render_home(), draw_map()

# -------------------------
# CSS (가로스크롤 제거 + DateTime picker 강제 표시)
# -------------------------
CSS = r"""
:root { --bg:#FAF9F6; --ink:#1F2937; --muted:#6B7280; --line:#E5E3DD; --card:#ffffffcc; --danger:#ef4444; }

* { box-sizing: border-box !important; }

html, body { width:100%; overflow-x:hidden !important; background:var(--bg) !important; }
.gradio-container { background:var(--bg) !important; width:100% !important; overflow-x:hidden !important; }

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
.mapFrame{ width:100vw; height: calc(100vh - 220px); border:0; border-radius:0; }

/* ✅ DateTime picker(flatpickr) 강제 표시 (모달/오버레이 위로) */
.flatpickr-calendar,
.flatpickr-time,
.flatpickr-dropdown {
  z-index: 1000005 !important;
}
.flatpickr-calendar {
  position: fixed !important; /* ✅ 이게 핵심 */
}

/* 모달 */
.oseyo_overlay{
  position:fixed !important;
  inset:0 !important;
  background:rgba(0,0,0,0.35) !important;
  z-index:99990 !important;
}

/* ✅ 모달 가로스크롤 절대 금지 */
.oseyo_sheet{
  position:fixed !important;
  left:50% !important; transform:translateX(-50%) !important;
  bottom:0 !important;
  width:min(420px,96vw) !important;
  height:88vh !important;
  overflow-y:auto !important;
  overflow-x:hidden !important;  /* ✅ */
  background:var(--bg) !important;
  border:1px solid var(--line) !important; border-bottom:0 !important;
  border-radius:26px 26px 0 0 !important;
  padding:22px 16px 170px 16px !important;
  z-index:99991 !important;
  box-shadow:0 -12px 40px rgba(0,0,0,0.25) !important;
}

.oseyo_sheet *{
  max-width:100% !important;     /* ✅ */
}

.oseyo_footer{
  position:fixed !important;
  left:50% !important; transform:translateX(-50%) !important;
  bottom:0 !important;
  width:min(420px,96vw) !important;
  padding:12px 16px 16px 16px !important;
  background:rgba(250,249,246,0.98) !important;
  border-top:1px solid var(--line) !important;
  z-index:99992 !important;
}

/* 입력들 */
.oseyo_sheet input, .oseyo_sheet textarea, .oseyo_sheet select{
  width:100% !important;
  min-width:0 !important;
}

/* slider 같은 애들이 삐져나오는 것 방지 */
.oseyo_sheet .wrap,
.oseyo_sheet .gr-row,
.oseyo_sheet .gr-form,
.oseyo_sheet .gr-box{
  max-width:100% !important;
  overflow-x:hidden !important;
}

/* FAB: 레이아웃 자리(바) 제거 */
.oseyo_fab_wrap{
  height:0 !important;
  overflow:visible !important;
}
.oseyo_fab{
  position:fixed !important;
  right:22px !important; bottom:22px !important;
  z-index:99980 !important;
}
.oseyo_fab button{
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

# -------------------------
# UI
# -------------------------
with gr.Blocks(css=CSS, title="Oseyo (DB)") as demo:
    addr_confirmed = gr.State("")
    addr_detail = gr.State("")
    addr_lat = gr.State(None)
    addr_lng = gr.State(None)
    addr_candidates = gr.State([])

    gr.Markdown("## 지금, 열려 있습니다\n원하시면 오세요")

    with gr.Tabs():
        with gr.Tab("탐색"):
            home_html = gr.HTML()
            refresh_btn = gr.Button("새로고침")
        with gr.Tab("지도"):
            map_html = gr.HTML()
            map_refresh = gr.Button("지도 새로고침")

    with gr.Row(elem_classes=["oseyo_fab_wrap"]):
        fab = gr.Button("+", elem_classes=["oseyo_fab"])

    overlay = gr.HTML("<div></div>", visible=False, elem_classes=["oseyo_overlay"])
    sheet = gr.Column(visible=False, elem_classes=["oseyo_sheet"])
    footer = gr.Row(visible=False, elem_classes=["oseyo_footer"])

    with sheet:
        with gr.Column(visible=True) as main_view:
            gr.Markdown("### 열어놓기")
            photo_np = gr.Image(label="사진(선택)", type="numpy")

            with gr.Row():
                activity_text = gr.Textbox(label="활동", placeholder="예: 산책, 커피, 스터디…", lines=1, scale=8)
                btn_add_fav = gr.Button("＋ 즐겨찾기", scale=2)

            fav_dropdown = gr.Dropdown(
                label="즐겨찾기 활동(선택하면 활동 입력에 채워짐)",
                choices=[],
                value=None
            )
            fav_msg = gr.Markdown("")

            start_dt = gr.DateTime(label="시작 일시", include_time=True)
            end_dt = gr.DateTime(label="종료 일시", include_time=True)

            capacity_unlimited = gr.Checkbox(value=True, label="제한 없음")
            cap_max = gr.Slider(1, 10, value=4, step=1, label="최대 인원(제한 있을 때)")

            chosen_place_view = gr.Markdown("**선택된 장소:** *(아직 없음)*")
            btn_open_addr = gr.Button("장소 검색하기")
            msg_main = gr.Markdown("")

        with gr.Column(visible=False) as addr_view:
            gr.Markdown("### 장소 검색")
            addr_query = gr.Textbox(label="주소/장소명", placeholder="예: 포항시청, 영일대, 포항테크노파크 …", lines=1)
            btn_addr_search = gr.Button("검색")
            msg_addr = gr.Markdown("")
            chosen_text = gr.Markdown("선택: 없음")
            addr_pick = gr.Dropdown(choices=[], value=None, label="검색 결과(선택)")
            addr_detail_in = gr.Textbox(label="상세(선택)", placeholder="예: 2층 203호 …", lines=1)

    with footer:
        btn_close = gr.Button("닫기")
        btn_back = gr.Button("뒤로")
        btn_done = gr.Button("완료")
        btn_addr_confirm = gr.Button("주소 선택 완료")

    # load
    demo.load(fn=render_home, inputs=None, outputs=home_html)
    demo.load(fn=draw_map, inputs=None, outputs=map_html)
    demo.load(fn=load_favs, inputs=None, outputs=fav_dropdown)

    refresh_btn.click(fn=render_home, inputs=None, outputs=home_html)
    map_refresh.click(fn=draw_map, inputs=None, outputs=map_html)

    # open/close modal (+ 숨김)
    fab.click(
        fn=open_modal,
        inputs=None,
        outputs=[overlay, sheet, footer, main_view, addr_view, msg_main, msg_addr, start_dt, end_dt, fab],
    )

    btn_close.click(
        fn=close_modal,
        inputs=None,
        outputs=[overlay, sheet, footer, main_view, addr_view, msg_main, msg_addr, fab],
    )

    # address flow
    btn_open_addr.click(
        fn=goto_addr,
        inputs=None,
        outputs=[main_view, addr_view, addr_candidates, addr_pick, chosen_text, msg_addr],
    )

    btn_back.click(
        fn=back_to_main,
        inputs=None,
        outputs=[main_view, addr_view, msg_addr],
    )

    btn_addr_search.click(
        fn=addr_search,
        inputs=[addr_query],
        outputs=[addr_candidates, addr_pick, chosen_text, msg_addr],
    )

    addr_pick.change(fn=on_pick, inputs=[addr_pick], outputs=[chosen_text])

    btn_addr_confirm.click(
        fn=confirm_addr,
        inputs=[addr_candidates, addr_pick, addr_detail_in],
        outputs=[msg_addr, addr_confirmed, addr_detail, addr_lat, addr_lng, main_view, addr_view],
    )

    addr_confirmed.change(fn=show_chosen_place, inputs=[addr_confirmed, addr_detail], outputs=[chosen_place_view])
    addr_detail.change(fn=show_chosen_place, inputs=[addr_confirmed, addr_detail], outputs=[chosen_place_view])

    # favorites
    btn_add_fav.click(
        fn=add_fav_from_activity,
        inputs=[activity_text],
        outputs=[fav_msg, fav_dropdown],
    )
    fav_dropdown.change(
        fn=pick_fav_to_activity,
        inputs=[fav_dropdown],
        outputs=[activity_text],
    )

    # create then close on success
    def create_then_close(*args):
        msg, home, mapv = create_event(*args)
        if isinstance(msg, str) and msg.startswith("✅"):
            return (
                msg, home, mapv,
                gr.update(visible=False), gr.update(visible=False), gr.update(visible=False),
                gr.update(visible=True), gr.update(visible=False),
                "",
                gr.update(visible=True),
                load_favs(),
            )
        return (
            msg, home, mapv,
            gr.update(visible=True), gr.update(visible=True), gr.update(visible=True),
            gr.update(visible=True), gr.update(visible=False),
            "",
            gr.update(visible=False),
            load_favs(),
        )

    btn_done.click(
        fn=create_then_close,
        inputs=[activity_text, start_dt, end_dt, capacity_unlimited, cap_max, photo_np,
                addr_confirmed, addr_detail, addr_lat, addr_lng],
        outputs=[msg_main, home_html, map_html,
                 overlay, sheet, footer, main_view, addr_view, msg_addr,
                 fab,
                 fav_dropdown],
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
def kakao_map(request: Request):
    if not KAKAO_JAVASCRIPT_KEY:
        return HTMLResponse("""
        <html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/></head>
        <body style="margin:0;font-family:system-ui;">
          <div style="padding:18px;">
            <h3 style="margin:0 0 8px;color:#b91c1c;">KAKAO_JAVASCRIPT_KEY가 없다</h3>
            <div>Render 환경변수에 KAKAO_JAVASCRIPT_KEY를 넣어야 카카오 지도가 뜬다.</div>
          </div>
        </body></html>
        """)

    points = map_points_payload()
    center_lat, center_lng = (36.0190, 129.3435)

    html = f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<style>
  html,body{{margin:0;height:100%;}}
  #map{{width:100%;height:100%;}}
  .iw{{font-family:system-ui;font-size:13px;line-height:1.4; padding:10px 10px;}}
  .t{{font-weight:900;margin-bottom:6px;}}
  .m{{color:#6B7280;}}
</style>
<script src="//dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}"></script>
</head>
<body>
<div id="map"></div>
<script>
  const center = new kakao.maps.LatLng({center_lat}, {center_lng});
  const map = new kakao.maps.Map(document.getElementById('map'), {{
    center: center,
    level: 6
  }});

  const points = {json.dumps(points, ensure_ascii=False)};

  if (!points.length) {{
    const marker = new kakao.maps.Marker({{ position: center }});
    marker.setMap(map);
  }} else {{
    const bounds = new kakao.maps.LatLngBounds();
    points.forEach(p => {{
      const pos = new kakao.maps.LatLng(p.lat, p.lng);
      bounds.extend(pos);

      const marker = new kakao.maps.Marker({{ position: pos }});
      marker.setMap(map);

      const content = `
        <div class="iw">
          <div class="t">${{p.title}}</div>
          <div class="m">${{p.period}}</div>
          <div class="m">${{p.addr}}</div>
          ${{p.detail ? `<div class="m">상세: ${{p.detail}}</div>` : ''}}
          <div class="m" style="margin-top:6px;font-size:12px;color:#9CA3AF;">ID: ${{p.id}}</div>
        </div>
      `;
      const infowindow = new kakao.maps.InfoWindow({{ content: content }});
      kakao.maps.event.addListener(marker, 'click', function() {{
        infowindow.open(map, marker);
      }});
    }});
    map.setBounds(bounds);
  }}
</script>
</body>
</html>
"""
    return HTMLResponse(html)

app = gr.mount_gradio_app(app, demo, path="/app")
