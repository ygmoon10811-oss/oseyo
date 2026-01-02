# =========================================================
# OSEYO — FINAL (DateTime + Kakao Address + No X-Scroll)
# =========================================================

import os, uuid, base64, io, sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import folium
from PIL import Image
import gradio as gr

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

# =========================
# 🔑 KAKAO API KEY
# =========================
KAKAO_REST_API_KEY = "c6a56d433bf68434d8e41ff12efafeb3"

# =========================
# TIMEZONE
# =========================
KST = ZoneInfo("Asia/Seoul")

def now_kst():
    return datetime.now(KST)

def normalize_dt(v):
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=KST)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, tz=KST)
    return None

def fmt_period(st_iso, en_iso):
    st = datetime.fromisoformat(st_iso).astimezone(KST)
    en = datetime.fromisoformat(en_iso).astimezone(KST)
    if st.date() == en.date():
        return f"{st:%m/%d %H:%M}–{en:%H:%M}"
    return f"{st:%m/%d %H:%M}–{en:%m/%d %H:%M}"

# =========================
# IMAGE
# =========================
def image_np_to_b64(img):
    if img is None:
        return ""
    im = Image.fromarray(img.astype("uint8"))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()

def b64_uri(b):
    return f"data:image/jpeg;base64,{b}" if b else ""

# =========================
# DB
# =========================
DATA_DIR = "/var/data" if os.path.isdir("/var/data") else "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = f"{DATA_DIR}/oseyo.db"

def db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

with db() as con:
    con.execute("""
    CREATE TABLE IF NOT EXISTS spaces (
        id TEXT PRIMARY KEY,
        title TEXT,
        photo TEXT,
        start TEXT,
        end TEXT,
        addr TEXT,
        detail TEXT,
        lat REAL,
        lng REAL,
        created TEXT
    )
    """)

def insert_space(s):
    with db() as con:
        con.execute("""
        INSERT INTO spaces VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            s["id"], s["title"], s["photo"], s["start"], s["end"],
            s["addr"], s["detail"], s["lat"], s["lng"],
            now_kst().isoformat()
        ))

def delete_space(i):
    with db() as con:
        con.execute("DELETE FROM spaces WHERE id=?", (i,))

def list_spaces():
    with db() as con:
        rows = con.execute("""
        SELECT id,title,photo,start,end,addr,detail,lat,lng
        FROM spaces ORDER BY created DESC
        """).fetchall()
    return [{
        "id":r[0],"title":r[1],"photo":r[2],"start":r[3],"end":r[4],
        "addr":r[5],"detail":r[6],"lat":r[7],"lng":r[8]
    } for r in rows]

def active_spaces():
    t = now_kst()
    out=[]
    for s in list_spaces():
        st = datetime.fromisoformat(s["start"]).astimezone(KST)
        en = datetime.fromisoformat(s["end"]).astimezone(KST)
        if st <= t <= en:
            out.append(s)
    return out

# =========================
# 📍 KAKAO ADDRESS SEARCH
# =========================
def kakao_search(q):
    if not q:
        return [], "⚠️ 검색어를 입력해라."
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params = {"query": q, "size": 10}
    r = requests.get(url, headers=headers, params=params)
    if r.status_code != 200:
        return [], "⚠️ 카카오 주소 검색 실패"
    docs = r.json()["documents"]
    cands=[]
    for d in docs:
        label = f"{d['place_name']} — {d['road_address_name'] or d['address_name']}"
        cands.append({
            "label": label,
            "lat": float(d["y"]),
            "lng": float(d["x"])
        })
    return cands, ""

def addr_search(q):
    c, e = kakao_search(q)
    if e:
        return [], gr.update(choices=[]), e, "선택: 없음", ""
    return c, gr.update(choices=[x["label"] for x in c]), "", "선택: 없음", ""

def addr_pick(cands, label, detail):
    for c in cands:
        if c["label"] == label:
            return "✅ 선택됨", label, detail, c["lat"], c["lng"], \
                   gr.update(visible=True), gr.update(visible=True), gr.update(visible=True), \
                   gr.update(visible=False), gr.update(visible=False), gr.update(visible=False)
    return "⚠️ 다시 선택", "", "", None, None, \
           gr.update(visible=False), gr.update(visible=False), gr.update(visible=False), \
           gr.update(visible=True), gr.update(visible=True), gr.update(visible=True)

# =========================
# MAP
# =========================
def draw_map():
    m = folium.Map([36.019,129.343], zoom_start=13)
    for s in active_spaces():
        pop = f"""
        <b>{s['title']}</b><br>
        {fmt_period(s['start'], s['end'])}<br>
        {s['addr']}<br>{s['detail']}
        """
        folium.Marker([s["lat"],s["lng"]], popup=pop).add_to(m)
    html = m.get_root().render()
    return f"<iframe style='width:100vw;height:80vh;border:0' srcdoc='{html}'></iframe>"

# =========================
# HOME
# =========================
def render_home():
    out=""
    for s in active_spaces():
        out+=f"""
        <div class='card'>
          <b>{s['title']}</b><br>
          <div class='period'>{fmt_period(s['start'],s['end'])}</div>
          <div>{s['addr']}</div>
          <a class='del' href='/delete/{s["id"]}'>삭제</a>
        </div>
        """
    return out or "<div class='card'>열린 이벤트 없음</div>"

# =========================
# CREATE
# =========================
def create(act, st, en, img, addr, detail, lat, lng):
    st = normalize_dt(st)
    en = normalize_dt(en)
    if not (act and st and en and addr):
        return "⚠️ 입력 누락", render_home(), draw_map(), gr.update(False), gr.update(False), gr.update(False)
    s = {
        "id": uuid.uuid4().hex[:8],
        "title": act,
        "photo": image_np_to_b64(img),
        "start": st.isoformat(),
        "end": en.isoformat(),
        "addr": addr,
        "detail": detail,
        "lat": lat,
        "lng": lng
    }
    insert_space(s)
    return "✅ 등록 완료", render_home(), draw_map(), gr.update(False), gr.update(False), gr.update(False)

# =========================
# UI + CSS (X-Scroll Kill)
# =========================
CSS = """
#main_sheet,#addr_sheet{overflow-x:hidden!important}
#main_sheet *,#addr_sheet *{max-width:100%!important}
"""

with gr.Blocks() as demo:
    gr.HTML(f"<style>{CSS}</style>")

    addr_cands = gr.State([])
    addr = gr.State("")
    lat = gr.State(None)
    lng = gr.State(None)

    home = gr.HTML()
    mapv = gr.HTML()

    fab = gr.Button("+", elem_id="fab")

    overlay = gr.HTML("<div class='oseyo_overlay'></div>", visible=False)
    main = gr.Column(visible=False, elem_id="main_sheet")
    foot = gr.Row(visible=False)

    with main:
        act = gr.Textbox(label="활동")
        start = gr.DateTime(label="시작", include_time=True)
        end = gr.DateTime(label="종료", include_time=True)
        img = gr.Image(type="numpy")
        place = gr.Markdown("선택된 장소: 없음")
        open_addr = gr.Button("장소 검색")

    with foot:
        close = gr.Button("닫기")
        ok = gr.Button("완료")

    addr_overlay = gr.HTML("<div class='oseyo_overlay'></div>", visible=False)
    addr_sheet = gr.Column(visible=False, elem_id="addr_sheet")
    addr_foot = gr.Row(visible=False)

    with addr_sheet:
        q = gr.Textbox(label="장소명")
        sbtn = gr.Button("검색")
        r = gr.Radio()
        d = gr.Textbox(label="상세")

    with addr_foot:
        back = gr.Button("뒤로")
        confirm = gr.Button("선택")

    demo.load(render_home, None, home)
    demo.load(draw_map, None, mapv)

    fab.click(lambda: (True,True,True), None, [overlay,main,foot])
    close.click(lambda: (False,False,False), None, [overlay,main,foot])

    open_addr.click(lambda: (False,False,False,True,True,True),
                    None,[overlay,main,foot,addr_overlay,addr_sheet,addr_foot])

    sbtn.click(addr_search,[q],[addr_cands,r])
    confirm.click(addr_pick,[addr_cands,r,d],
                  [place,addr,d,lat,lng,overlay,main,foot,addr_overlay,addr_sheet,addr_foot])

    ok.click(create,[act,start,end,img,addr,d,lat,lng],
             [home,mapv,overlay,main,foot])

# =========================
# FASTAPI
# =========================
app = FastAPI()

@app.get("/")
def root():
    return RedirectResponse("/app")

@app.get("/delete/{i}")
def delete(i):
    delete_space(i)
    return RedirectResponse("/app")

app = gr.mount_gradio_app(app, demo, path="/app")

