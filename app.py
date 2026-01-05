# =========================================================
# OSEYO — FINAL STABLE (Render + Gradio + FastAPI)
# ✅ DB: /var/data SQLite + 자동 마이그레이션
# ✅ 모달: 부모 레이어 토글(잔상 0%)
# ✅ 가로 스크롤: 전체 차단
# ✅ 일시: gr.DateTime (캘린더 + 24h + 60m)
# ✅ 주소: Kakao 키워드 검색(REST) + 선택은 Dropdown(모바일 안정)
# ✅ 지도: Kakao 지도(JS SDK) (Leaflet/Folium 제거)
# ✅ 삭제: 카드 삭제 버튼 /delete/{id}
#
# Render Env:
# - KAKAO_REST_API_KEY       (카카오 REST 키)
# - KAKAO_JAVASCRIPT_KEY     (카카오 JS 키)
#
# Render Start Command:
# uvicorn app:app --host 0.0.0.0 --port $PORT
# =========================================================

import os, uuid, base64, io, sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image
import gradio as gr

from fastapi import FastAPI
from fastapi.responses import RedirectResponse


# -------------------------
# CONFIG
# -------------------------
KST = ZoneInfo("Asia/Seoul")
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

def now_kst():
    return datetime.now(KST)

def normalize_dt(v):
    """gr.DateTime 값이 datetime/timestamp로 올 수 있어 통일"""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=KST)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=KST)
    return None

def fmt_period(st_iso: str, en_iso: str) -> str:
    try:
        st = datetime.fromisoformat(st_iso)
        en = datetime.fromisoformat(en_iso)
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
# DB (Render Disk friendly)
# -------------------------
def get_data_dir():
    return "/var/data" if os.path.isdir("/var/data") else os.path.join(os.getcwd(), "data")

DATA_DIR = get_data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "oseyo.db")

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def _cols(con, table="spaces"):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]

def db_init_and_migrate():
    """
    ✅ 어떤 과거 스키마가 남아 있어도 앱이 안 죽게:
    - 최신 테이블 생성
    - 누락 컬럼 ADD COLUMN
    - 과거 컬럼명(photo/start/end/addr/detail/created 등) 있으면 최신 컬럼으로 복사
    """
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
        con.commit()

        cols = set(_cols(con))

        def addcol(sql):
            con.execute(sql)

        # 최신 컬럼 누락 보강
        if "photo_b64" not in cols: addcol("ALTER TABLE spaces ADD COLUMN photo_b64 TEXT DEFAULT ''")
        if "start_iso" not in cols: addcol("ALTER TABLE spaces ADD COLUMN start_iso TEXT")
        if "end_iso" not in cols: addcol("ALTER TABLE spaces ADD COLUMN end_iso TEXT")
        if "address_confirmed" not in cols: addcol("ALTER TABLE spaces ADD COLUMN address_confirmed TEXT")
        if "address_detail" not in cols: addcol("ALTER TABLE spaces ADD COLUMN address_detail TEXT DEFAULT ''")
        if "lat" not in cols: addcol("ALTER TABLE spaces ADD COLUMN lat REAL")
        if "lng" not in cols: addcol("ALTER TABLE spaces ADD COLUMN lng REAL")
        if "capacity_enabled" not in cols: addcol("ALTER TABLE spaces ADD COLUMN capacity_enabled INTEGER NOT NULL DEFAULT 0")
        if "capacity_max" not in cols: addcol("ALTER TABLE spaces ADD COLUMN capacity_max INTEGER")
        if "hidden" not in cols: addcol("ALTER TABLE spaces ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        if "created_at" not in cols: addcol("ALTER TABLE spaces ADD COLUMN created_at TEXT")

        con.commit()
        cols = set(_cols(con))

        # 과거 컬럼 → 최신 컬럼 데이터 복사 (있을 때만)
        if "photo" in cols:
            con.execute("UPDATE spaces SET photo_b64 = COALESCE(photo_b64, photo, '') WHERE (photo_b64 IS NULL OR photo_b64='')")
        if "start" in cols:
            con.execute("UPDATE spaces SET start_iso = COALESCE(start_iso, start) WHERE (start_iso IS NULL OR start_iso='')")
        if "end" in cols:
            con.execute("UPDATE spaces SET end_iso = COALESCE(end_iso, end) WHERE (end_iso IS NULL OR end_iso='')")
        if "addr" in cols:
            con.execute("UPDATE spaces SET address_confirmed = COALESCE(address_confirmed, addr) WHERE (address_confirmed IS NULL OR address_confirmed='')")
        if "detail" in cols:
            con.execute("UPDATE spaces SET address_detail = COALESCE(address_detail, detail, '') WHERE (address_detail IS NULL OR address_detail='')")
        if "created" in cols:
            con.execute("UPDATE spaces SET created_at = COALESCE(created_at, created) WHERE (created_at IS NULL OR created_at='')")

        con.execute(
            "UPDATE spaces SET created_at = COALESCE(created_at, ?) WHERE created_at IS NULL OR created_at=''",
            (now_kst().isoformat(),)
        )
        con.commit()

db_init_and_migrate()

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
            "start_iso": r[3],
            "end_iso": r[4],
            "address_confirmed": r[5],
            "address_detail": r[6] or "",
            "lat": float(r[7]),
            "lng": float(r[8]),
            "capacityEnabled": bool(r[9]),
            "capacityMax": r[10],
            "hidden": bool(r[11]),
            "created_at": r[12] or "",
        })
    return out

def active_spaces(spaces):
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
            st = st.astimezone(KST); en = en.astimezone(KST)
            if st <= t <= en:
                out.append(s)
        except:
            pass
    return out


# -------------------------
# Kakao place search (REST)
# -------------------------
def kakao_keyword_search(q: str, size=12):
    q = (q or "").strip()
    if not q:
        return [], "⚠️ 장소/주소를 입력해 달라."
    if not KAKAO_REST_API_KEY:
        return [], "⚠️ KAKAO_REST_API_KEY가 없다. Render 환경변수에 REST API 키를 넣어야 한다."

    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params = {"query": q, "size": size}

    try:
        r = requests.get(url, headers=headers, params=params, timeout=12)
        if r.status_code == 401:
            return [], "⚠️ (401) 인증 실패. REST API 키인지 확인해 달라."
        if r.status_code == 429:
            return [], "⚠️ (429) 호출 제한. 잠시 후 다시 시도해 달라."
        if r.status_code >= 400:
            return [], f"⚠️ 카카오 검색 실패 (HTTP {r.status_code})"
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
        return [], "⚠️ 검색 결과가 없다. 키워드를 조금 바꿔 달라."
    return cands, ""

def addr_do_search(query):
    cands, err = kakao_keyword_search(query, size=12)
    if err:
        return cands, gr.update(choices=[], value=None), err, "선택: 없음"
    labels = [c["label"] for c in cands]
    return cands, gr.update(choices=labels, value=None), "", "선택: 없음"

def confirm_addr_by_label(cands, label, detail):
    label = (label or "").strip()
    if not label:
        return "⚠️ 주소 후보를 선택해 달라.", "", "", None, None

    chosen = None
    for c in (cands or []):
        if c.get("label") == label:
            chosen = c
            break

    if not chosen:
        return "⚠️ 선택한 주소를 다시 선택해 달라.", "", "", None, None

    confirmed = chosen["label"]
    det = (detail or "").strip()
    return "✅ 주소가 선택되었다.", confirmed, det, chosen["lat"], chosen["lng"]

def show_chosen_place(addr_confirmed, addr_detail):
    if not addr_confirmed:
        return "**선택된 장소:** *(아직 없음)*"
    if addr_detail:
        return f"**선택된 장소:** {addr_confirmed}\n\n상세: {addr_detail}"
    return f"**선택된 장소:** {addr_confirmed}"


# -------------------------
# Kakao Map (JS SDK)
# -------------------------
def make_kakao_map_html(items, center=(36.0190, 129.3435), level=6):
    if not KAKAO_JAVASCRIPT_KEY:
        return """
        <div style="max-width:900px;margin:24px auto;padding:18px;border:1px solid #fee2e2;border-radius:14px;background:#fff;">
          <div style="font-weight:900;color:#b91c1c;margin-bottom:6px;">KAKAO_JAVASCRIPT_KEY가 없다</div>
          <div style="color:#6b7280;font-size:13px;line-height:1.6;">
            Render 환경변수에 KAKAO_JAVASCRIPT_KEY를 추가해야 카카오 지도가 뜬다.
          </div>
        </div>
        """

    markers_js = []
    for s in (items or []):
        try:
            title = (s.get("title") or "").replace("\\", "\\\\").replace("'", "\\'")
            addr = (s.get("address_confirmed") or "").replace("\\", "\\\\").replace("'", "\\'")
            period = fmt_period(s.get("start_iso",""), s.get("end_iso","")).replace("\\", "\\\\").replace("'", "\\'")
            lat = float(s["lat"]); lng = float(s["lng"])
            markers_js.append(f"{{title:'{title}', addr:'{addr}', period:'{period}', lat:{lat}, lng:{lng}}}")
        except:
            pass

    markers_array = "[" + ",".join(markers_js) + "]"
    c_lat, c_lng = float(center[0]), float(center[1])
    lvl = int(level)

    # ⚠️ Gradio 탭 전환/리렌더링 안정성을 위해 "컨테이너마다" 초기화
    # - mapDiv가 존재하면 항상 새 Map 객체를 만든다.
    # - relayout은 약간 지연해서 실행.
    return f"""
    <div class="kakaoMapWrap">
      <div id="kakao_map" class="kakaoMap"></div>
      <div id="kakao_map_hint" class="kakaoHint" style="display:none;"></div>
    </div>

    <script src="https://dapi.kakao.com/v2/maps/sdk.js?appkey={KAKAO_JAVASCRIPT_KEY}&autoload=false"></script>
    <script>
      (function() {{
        function showHint(msg) {{
          var el = document.getElementById('kakao_map_hint');
          if (!el) return;
          el.style.display = 'block';
          el.innerHTML = msg;
        }}

        kakao.maps.load(function() {{
          var container = document.getElementById('kakao_map');
          if (!container) return;

          var map = new kakao.maps.Map(container, {{
            center: new kakao.maps.LatLng({c_lat}, {c_lng}),
            level: {lvl}
          }});

          map.addControl(new kakao.maps.ZoomControl(), kakao.maps.ControlPosition.RIGHT);

          var markers = {markers_array};

          if (!markers || markers.length === 0) {{
            showHint("지금은 열려 있는 곳이 없다");
            setTimeout(function(){{ map.relayout(); }}, 250);
            return;
          }}

          var bounds = new kakao.maps.LatLngBounds();

          markers.forEach(function(m) {{
            var pos = new kakao.maps.LatLng(m.lat, m.lng);
            bounds.extend(pos);

            var marker = new kakao.maps.Marker({{
              position: pos,
              map: map
            }});

            var iw = new kakao.maps.InfoWindow({{
              content:
                "<div style='padding:10px 12px;min-width:220px;font-family:system-ui;'>" +
                "<div style='font-weight:900;margin-bottom:6px;color:#111827;'>" + m.title + "</div>" +
                "<div style='font-size:12px;color:#111827;font-weight:800;'>" + m.period + "</div>" +
                "<div style='font-size:12px;color:#6b7280;margin-top:4px;'>" + m.addr + "</div>" +
                "</div>"
            }});

            kakao.maps.event.addListener(marker, 'click', function() {{
              iw.open(map, marker);
            }});
          }});

          map.setBounds(bounds);

          setTimeout(function() {{
            map.relayout();
            map.setBounds(bounds);
          }}, 350);
        }});
      }})();
    </script>
    """

def draw_map():
    items = active_spaces(db_list_spaces())
    return make_kakao_map_html(items, center=(36.0190, 129.3435), level=6)


# -------------------------
# Home render (cards)
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
# Modal control (잔상 0%: 부모 레이어만 토글)
# -------------------------
def open_main():
    st = now_kst().replace(second=0, microsecond=0)
    en = st + timedelta(minutes=30)
    return gr.update(visible=True), gr.update(visible=False), st, en, "", ""

def close_all():
    return gr.update(visible=False), gr.update(visible=False), "", ""

def go_addr():
    return (
        gr.update(visible=False),                # main_layer off
        gr.update(visible=True),                 # addr_layer on
        [],                                      # candidates reset
        gr.update(choices=[], value=None),       # dropdown reset
        "",                                      # addr_err
        "선택: 없음",                             # chosen_text
        "",                                      # addr_msg
        gr.update(value="")                      # detail reset
    )

def back_main():
    return gr.update(visible=True), gr.update(visible=False), ""

def confirm_and_back(cands, selected_label, detail):
    msg, conf, det, la, ln = confirm_addr_by_label(cands, selected_label, detail)
    if conf and la is not None and ln is not None:
        return msg, conf, det, la, ln, gr.update(visible=True), gr.update(visible=False)
    return msg, conf, det, la, ln, gr.update(visible=False), gr.update(visible=True)

def chosen_text_fn(v):
    return "선택: 없음" if not v else f"선택: {v}"


# -------------------------
# Create
# -------------------------
def create_and_close(
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
            return "⚠️ 활동을 입력해 달라.", render_home(), draw_map(), gr.update(visible=True)

        if (not addr_confirmed) or (addr_lat is None) or (addr_lng is None):
            return "⚠️ 장소를 선택해 달라. (장소 검색하기)", render_home(), draw_map(), gr.update(visible=True)

        st = normalize_dt(start_dt_val)
        en = normalize_dt(end_dt_val)
        if st is None or en is None:
            return "⚠️ 시작/종료 일시를 선택해 달라.", render_home(), draw_map(), gr.update(visible=True)

        st = st.astimezone(KST); en = en.astimezone(KST)
        if en <= st:
            return "⚠️ 종료 일시는 시작 일시보다 뒤여야 한다.", render_home(), draw_map(), gr.update(visible=True)

        new_id = uuid.uuid4().hex[:8]
        photo_b64 = image_np_to_b64(photo_np)

        capacityEnabled = (not bool(capacity_unlimited))
        capacityMax = None if not capacityEnabled else int(min(int(cap_max), 10))

        title = act if len(act) <= 24 else act[:24] + "…"

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
            "capacityMax": capacityMax,
            "hidden": False,
        })

        return f"✅ 등록 완료: '{title}'", render_home(), draw_map(), gr.update(visible=False)

    except Exception as e:
        return f"❌ 등록 중 오류: {type(e).__name__}", render_home(), draw_map(), gr.update(visible=True)


# -------------------------
# CSS (가로스크롤 0% + 모달/지도 스타일)
# -------------------------
CSS = r"""
:root { --bg:#FAF9F6; --ink:#1F2937; --muted:#6B7280; --line:#E5E3DD; --card:#ffffffcc; --danger:#ef4444; }

html, body { width:100%; max-width:100%; overflow-x:hidden !important; background:var(--bg) !important; }
.gradio-container { background:var(--bg) !important; width:100% !important; max-width:100% !important; overflow-x:hidden !important; }
.gradio-container * { box-sizing:border-box !important; max-width:100% !important; }

/* 페이지 전체에서 가로 스크롤 생성 자체를 막음 */
body, .gradio-container, .contain, .wrap { overflow-x:hidden !important; }

/* 상단 배너 */
.banner{ max-width:1200px; margin:10px auto 6px; padding:10px 12px; border-radius:14px; font-size:13px; line-height:1.5; }
.banner.ok{ background:#ecfdf5; border:1px solid #a7f3d0; color:#065f46; }
.banner.warn{ background:#fff7ed; border:1px solid #fed7aa; color:#9a3412; }

/* 카드 */
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

/* Kakao 지도 */
.kakaoMapWrap{ width:100vw; max-width:100vw; margin:0; padding:0; overflow:hidden; position:relative; }
.kakaoMap{ width:100vw; height: calc(100vh - 140px); }
.kakaoHint{
  position:absolute; left:50%; top:14px; transform:translateX(-50%);
  background:rgba(255,255,255,0.92); border:1px solid #E5E3DD;
  border-radius:999px; padding:8px 12px; font-size:12px; color:#111827; font-weight:900;
}

/* 오버레이 */
.oseyo_overlay{
  position:fixed !important;
  inset:0 !important;
  background:rgba(0,0,0,0.35) !important;
  z-index:99990 !important;
}

/* 모달 */
#main_sheet, #addr_sheet{
  position:fixed !important;
  left:50% !important; transform:translateX(-50%) !important;
  bottom:0 !important;
  width:min(420px,96vw) !important;
  height:88vh !important;
  overflow-y:auto !important;
  overflow-x:hidden !important;
  background:var(--bg) !important;
  border:1px solid var(--line) !important; border-bottom:0 !important;
  border-radius:26px 26px 0 0 !important;
  padding:22px 16px 160px 16px !important;
  z-index:99991 !important;
  box-shadow:0 -12px 40px rgba(0,0,0,0.25) !important;
}

/* 푸터 */
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

/* ABSOLUTE FINAL: 가로스크롤 0% */
#main_sheet, #addr_sheet { overflow-x:hidden !important; }
#main_sheet *, #addr_sheet *{
  overflow-x:hidden !important;
  max-width:100% !important;
  box-sizing:border-box !important;
}
#main_sheet .gr-row, #addr_sheet .gr-row{ flex-wrap:wrap !important; }
#main_sheet .gr-row > *, #addr_sheet .gr-row > *{ min-width:0 !important; }

/* DateTime / input 폭 튐 방지 */
#main_sheet input, #addr_sheet input,
#main_sheet textarea, #addr_sheet textarea,
#main_sheet select, #addr_sheet select{
  width:100% !important;
  min-width:0 !important;
}

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


# -------------------------
# UI (Gradio)
# -------------------------
with gr.Blocks(title="Oseyo (DB)") as demo:
    gr.HTML(f"<style>{CSS}</style>")

    # state
    addr_confirmed = gr.State("")
    addr_detail = gr.State("")
    addr_lat = gr.State(None)
    addr_lng = gr.State(None)
    addr_candidates = gr.State([])

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

    # ✅ 부모 레이어 토글: 잔상 0%
    main_layer = gr.Column(visible=False)
    addr_layer = gr.Column(visible=False)

    # main layer
    with main_layer:
        gr.HTML("<div class='oseyo_overlay'></div>")
        main_sheet = gr.Column(elem_id="main_sheet")
        main_footer = gr.Row(elem_id="main_footer")

    # addr layer
    with addr_layer:
        gr.HTML("<div class='oseyo_overlay'></div>")
        addr_sheet = gr.Column(elem_id="addr_sheet")
        addr_footer = gr.Row(elem_id="addr_footer")

    # main modal content
    with main_sheet:
        gr.HTML("<div style='font-size:22px;font-weight:900;color:#1F2937;margin:0 0 10px 0;'>열어놓기</div>")
        photo_np = gr.Image(label="사진(선택)", type="numpy")
        activity_text = gr.Textbox(label="활동", placeholder="예: 산책, 커피, 스터디…", lines=1)

        start_dt = gr.DateTime(label="시작 일시", include_time=True)
        end_dt = gr.DateTime(label="종료 일시", include_time=True)

        capacity_unlimited = gr.Checkbox(value=True, label="제한 없음")
        cap_max = gr.Slider(1, 10, value=4, step=1, label="최대 인원(제한 있을 때)")

        chosen_place_view = gr.Markdown("**선택된 장소:** *(아직 없음)*")
        open_addr_btn = gr.Button("장소 검색하기")
        main_msg = gr.Markdown("")

    with main_footer:
        main_close = gr.Button("닫기")
        main_create = gr.Button("완료", elem_classes=["primary"])

    # addr modal content
    with addr_sheet:
        gr.HTML("<div style='font-size:22px;font-weight:900;color:#1F2937;margin:0 0 10px 0;'>장소 검색</div>")
        addr_query = gr.Textbox(label="주소/장소명", placeholder="예: 포항시청, 영일대 …", lines=1)
        addr_search_btn = gr.Button("검색")
        addr_err = gr.Markdown("")
        chosen_text = gr.Markdown("선택: 없음")

        # ✅ Radio 대신 Dropdown (모바일에서 제일 안정)
        addr_select = gr.Dropdown(choices=[], value=None, label="검색 결과(선택)", interactive=True)

        addr_detail_in = gr.Textbox(label="상세(선택)", placeholder="예: 2층 203호 …", lines=1)
        addr_msg = gr.Markdown("")

    with addr_footer:
        addr_close = gr.Button("닫기")
        addr_back = gr.Button("뒤로")
        addr_confirm_btn = gr.Button("주소 선택 완료", elem_classes=["primary"])

    # initial load
    demo.load(fn=render_home, inputs=None, outputs=home_html)
    demo.load(fn=draw_map, inputs=None, outputs=map_html)

    refresh_btn.click(fn=render_home, inputs=None, outputs=home_html)
    map_refresh.click(fn=draw_map, inputs=None, outputs=map_html)

    # open main
    fab.click(fn=open_main, inputs=None, outputs=[main_layer, addr_layer, start_dt, end_dt, main_msg, addr_msg])

    # close all (잔상 0%)
    main_close.click(fn=close_all, inputs=None, outputs=[main_layer, addr_layer, main_msg, addr_msg])
    addr_close.click(fn=close_all, inputs=None, outputs=[main_layer, addr_layer, main_msg, addr_msg])

    # go addr / back
    open_addr_btn.click(
        fn=go_addr,
        inputs=None,
        outputs=[main_layer, addr_layer, addr_candidates, addr_select, addr_err, chosen_text, addr_msg, addr_detail_in]
    )
    addr_back.click(fn=back_main, inputs=None, outputs=[main_layer, addr_layer, addr_msg])

    # search addr
    addr_search_btn.click(
        fn=addr_do_search,
        inputs=[addr_query],
        outputs=[addr_candidates, addr_select, addr_err, chosen_text]
    )

    addr_select.change(fn=chosen_text_fn, inputs=[addr_select], outputs=[chosen_text])

    # confirm addr -> auto back to main
    addr_confirm_btn.click(
        fn=confirm_and_back,
        inputs=[addr_candidates, addr_select, addr_detail_in],
        outputs=[addr_msg, addr_confirmed, addr_detail, addr_lat, addr_lng, main_layer, addr_layer]
    )

    # chosen place view
    addr_confirmed.change(fn=show_chosen_place, inputs=[addr_confirmed, addr_detail], outputs=[chosen_place_view])
    addr_detail.change(fn=show_chosen_place, inputs=[addr_confirmed, addr_detail], outputs=[chosen_place_view])

    # create
    main_create.click(
        fn=create_and_close,
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
        outputs=[main_msg, home_html, map_html, main_layer]
    )


# -------------------------
# FastAPI + Delete Route
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

app = gr.mount_gradio_app(app, demo, path="/app")
