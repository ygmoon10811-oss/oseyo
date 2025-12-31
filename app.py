import os, sqlite3, uuid, base64, io
from datetime import datetime, timedelta

import gradio as gr
import folium
from PIL import Image

# =========================
# DB (Render Disk 대응)
# =========================
DATA_DIR = "/var/data" if os.path.isdir("/var/data") else "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "oseyo.db")

def db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

with db() as con:
    con.execute("""
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        title TEXT,
        start TEXT,
        end TEXT,
        addr TEXT,
        detail TEXT,
        lat REAL,
        lng REAL,
        image TEXT
    )
    """)

# =========================
# Utils
# =========================
def now():
    return datetime.now()

def hm(dt): 
    return dt.strftime("%H:%M")

def img_to_b64(np):
    if np is None:
        return ""
    im = Image.fromarray(np.astype("uint8"))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()

# =========================
# DB Ops
# =========================
def add_event(title, minutes, img, addr, detail, lat, lng):
    eid = uuid.uuid4().hex[:8]
    s = now()
    e = s + timedelta(minutes=int(minutes))
    with db() as con:
        con.execute(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
            (eid, title, s.isoformat(), e.isoformat(), addr, detail, lat, lng, img_to_b64(img))
        )

def del_event(eid):
    with db() as con:
        con.execute("DELETE FROM events WHERE id=?", (eid,))

def load_events():
    with db() as con:
        return con.execute("SELECT * FROM events ORDER BY start DESC").fetchall()

# =========================
# UI Render
# =========================
def render_cards():
    rows = load_events()
    if not rows:
        return "<div class='card empty'>아직 열린 공간이 없습니다</div>"

    html = ""
    for r in rows:
        eid, title, st, en, addr, detail, lat, lng, img = r
        img_html = (
            f"<img class='thumb' src='data:image/jpeg;base64,{img}'/>"
            if img else "<div class='thumb placeholder'></div>"
        )
        html += f"""
        <div class="card">
          <div class="rowcard">
            <div class="left">
              <div class="title">{title}</div>
              <div class="muted">오늘 {hm(datetime.fromisoformat(st))}–{hm(datetime.fromisoformat(en))}</div>
              <div class="muted">{addr}</div>
              <div class="muted">상세: {detail or "-"}</div>
              <div class="idline">ID: {eid}</div>
            </div>
            <div class="right">{img_html}</div>
          </div>
          <button class="btn-del" onclick="fetch('/delete/{eid}').then(()=>location.reload())">삭제</button>
        </div>
        """
    return html

def render_map():
    m = folium.Map(location=[36.02,129.34], zoom_start=13)
    for r in load_events():
        folium.Marker([r[6], r[7]], tooltip=r[1]).add_to(m)
    return m._repr_html_()

# =========================
# FastAPI hooks (삭제용)
# =========================
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

api = FastAPI()

@api.get("/delete/{eid}")
def api_delete(eid: str):
    del_event(eid)
    return {"ok": True}

# =========================
# CSS
# =========================
CSS = r"""
body, html { background:#FAF9F6; }
.card{ background:#ffffffcc; border:1px solid #e5e3dd; border-radius:18px; padding:14px; margin:14px auto; max-width:1200px; position:relative; }
.card.empty{ max-width:700px; }
.rowcard{ display:grid; grid-template-columns:1fr minmax(320px,560px); gap:18px; padding-right:86px; }
.title{ font-weight:900; }
.muted{ font-size:13px; color:#6b7280; }
.idline{ font-size:12px; color:#9ca3af; margin-top:6px; }
.thumb{ width:100%; height:220px; object-fit:cover; border-radius:14px; }
.thumb.placeholder{ border:1px dashed #ddd; }
.btn-del{ position:absolute; right:14px; bottom:14px; background:#ef4444; color:#fff; border:0; border-radius:12px; padding:10px 14px; font-weight:900; }
@media(max-width:820px){
  .rowcard{ grid-template-columns:1fr; padding-right:14px; }
  .btn-del{ position:static; width:100%; margin-top:10px; }
}
#oseyo_fab{ position:fixed !important; right:22px; bottom:22px; z-index:99999; }
#oseyo_fab button{ width:64px; height:64px; border-radius:50%; font-size:36px; background:#111; color:#fff; border:0; }
"""

# =========================
# Gradio
# =========================
with gr.Blocks(css=CSS) as demo:
    gr.Markdown("# 지금, 열려 있습니다\n원하시면 오세요")

    with gr.Tabs():
        with gr.Tab("탐색"):
            cards = gr.HTML(render_cards())
            gr.Button("새로고침").click(render_cards, outputs=cards)
        with gr.Tab("지도"):
            gr.HTML(render_map())

    fab = gr.Button("+", elem_id="oseyo_fab")

    with gr.Group(visible=False) as modal:
        img = gr.Image()
        title = gr.Textbox(label="활동")
        mins = gr.Dropdown([30,60,90,120], value=30, label="지속(분)")
        addr = gr.Textbox(label="주소")
        detail = gr.Textbox(label="상세")
        lat = gr.Number(value=36.02)
        lng = gr.Number(value=129.34)
        submit = gr.Button("등록")

    def open_modal(): return gr.update(visible=True)
    def close_modal(): return gr.update(visible=False)

    fab.click(open_modal, outputs=modal)
    submit.click(
        lambda *x: (add_event(*x), render_cards(), close_modal()),
        inputs=[title, mins, img, addr, detail, lat, lng],
        outputs=[cards, modal]
    )

# =========================
# Mount FastAPI + Gradio
# =========================
from fastapi import FastAPI
app = FastAPI()
app.mount("/app", gr.mount_gradio_app(app, demo))
app.mount("/", api)
