# -*- coding: utf-8 -*-
print("### DEPLOY MARKER: V21_POSTGRES_MIGRATION ###", flush=True)
import os
import io
import re
import uuid
import json
import base64
import hashlib
import html
from datetime import datetime, timedelta, timezone

import uvicorn
import requests
from PIL import Image
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response

# --- PostgreSQL Library ---
import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager

# --- BEGIN: Render-safe gradio_client bool-schema hotfix ---
try:
    from gradio_client import utils as _gc_utils
    if not getattr(_gc_utils, "_OSEYO_PATCHED_BOOL_SCHEMA", False):
        _APIInfoParseError = getattr(_gc_utils, "APIInfoParseError", Exception)
        def _schema_from(args, kwargs):
            if "schema" in kwargs: return kwargs.get("schema")
            return args[0] if args else None
        def _wrap(orig):
            def _wrapped(*args, **kwargs):
                schema = _schema_from(args, kwargs)
                if isinstance(schema, bool): return "Any"
                try: return orig(*args, **kwargs)
                except _APIInfoParseError: return "Any"
                except TypeError:
                    try: return orig(schema)
                    except Exception: return "Any"
                except Exception: raise
            return _wrapped
        if hasattr(_gc_utils, "json_schema_to_python_type"):
            _gc_utils.json_schema_to_python_type = _wrap(_gc_utils.json_schema_to_python_type)
        if hasattr(_gc_utils, "_json_schema_to_python_type"):
            _gc_utils._json_schema_to_python_type = _wrap(_gc_utils._json_schema_to_python_type)
        if hasattr(_gc_utils, "get_type"):
            _gc_utils.get_type = _wrap(_gc_utils.get_type)
        _gc_utils._OSEYO_PATCHED_BOOL_SCHEMA = True
except Exception: pass
# --- END ---

import gradio as gr

# =========================================================
# 0) 시간/키/DB 설정
# =========================================================
KST = timezone(timedelta(hours=9))
def now_kst(): return datetime.now(KST)

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()
COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 24 * 7

# PostgreSQL Connection Pool Setup
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

try:
    # pool_min=1, pool_max=10 (적절히 조절 가능)
    db_pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn=DATABASE_URL)
    print("[DB] PostgreSQL Connection Pool Initialized.")
except Exception as e:
    print(f"[DB] Initial Connection Error: {e}")
    db_pool = None

@contextmanager
def get_cursor():
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        db_pool.putconn(conn)

def init_db():
    with get_cursor() as cur:
        # users
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY,
              email TEXT UNIQUE,
              pw_hash TEXT,
              name TEXT,
              gender TEXT,
              birth TEXT,
              created_at TEXT
            );
        """)
        # sessions
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
              token TEXT PRIMARY KEY,
              user_id TEXT,
              expires_at TEXT
            );
        """)
        # email_otps
        cur.execute("""
            CREATE TABLE IF NOT EXISTS email_otps (
              email TEXT PRIMARY KEY,
              otp TEXT,
              expires_at TEXT
            );
        """)
        # events (PostgreSQL에서 start, end는 예약어이므로 쌍따옴표 권장)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY,
              title TEXT,
              photo TEXT,
              "start" TEXT,
              "end" TEXT,
              addr TEXT,
              lat DOUBLE PRECISION,
              lng DOUBLE PRECISION,
              created_at TEXT,
              user_id TEXT,
              capacity INTEGER DEFAULT 10,
              is_unlimited INTEGER DEFAULT 0
            );
        """)
        # favs
        cur.execute("""
            CREATE TABLE IF NOT EXISTS favs (
              name TEXT PRIMARY KEY,
              count INTEGER DEFAULT 1
            );
        """)
        # event_participants
        cur.execute("""
            CREATE TABLE IF NOT EXISTS event_participants (
              event_id TEXT,
              user_id TEXT,
              joined_at TEXT,
              PRIMARY KEY(event_id, user_id)
            );
        """)

init_db()

# =========================================================
# 2) 비밀번호/세션
# =========================================================
def pw_hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 150_000)
    return f"{salt}${dk.hex()}"

def pw_verify(password: str, stored: str) -> bool:
    try:
        if not stored: return False
        salt, _ = stored.split("$", 1)
        return pw_hash(password, salt) == stored
    except Exception: return False

def create_session(user_id: str) -> str:
    token = uuid.uuid4().hex
    expires = now_kst() + timedelta(hours=SESSION_HOURS)
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO sessions(token, user_id, expires_at) VALUES(%s,%s,%s)",
            (token, user_id, expires.isoformat()),
        )
    return token

def get_user_id_from_request(req: Request):
    token = req.cookies.get(COOKIE_NAME)
    if not token: return None
    with get_cursor() as cur:
        cur.execute("SELECT user_id, expires_at FROM sessions WHERE token=%s", (token,))
        row = cur.fetchone()
    if not row: return None
    user_id, exp = row
    try:
        if datetime.fromisoformat(exp) < now_kst(): return None
    except Exception: return None
    return user_id

def require_user(req: Request):
    uid = get_user_id_from_request(req)
    if not uid: raise PermissionError("로그인이 필요합니다.")
    return uid

# =========================================================
# 3) 날짜 파싱/남은 시간 (동일)
# =========================================================
_DT_FORMATS = ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y%m%d", "%Y%m%d%H%M", "%Y%m%d%H%M%S"]

def parse_dt(s, assume_end: bool = False):
    if not s: return None
    s = str(s).strip()
    if not s: return None
    if s.endswith("Z"): s = s[:-1] + "+00:00"
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
    if m:
        y, mo, d, hh, mm, ss = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
        return datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss or 0), tzinfo=KST)
    if " " in s and ("+" in s[10:] or "-" in s[10:]):
        head, tail = s.split(" ", 1)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", head): s = head + "T" + tail
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None: dt = dt.replace(tzinfo=KST)
        else: dt = dt.astimezone(KST)
        if assume_end and (re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) or re.fullmatch(r"\d{8}", s)):
            dt = dt.replace(hour=23, minute=59, second=59)
        return dt
    except: pass
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=KST)
            if assume_end and fmt in ("%Y-%m-%d", "%Y%m%d"): dt = dt.replace(hour=23, minute=59, second=59)
            return dt
        except: continue
    return None

def is_active_event(end_s, start_s=None):
    now = now_kst()
    end_s_str = '' if end_s is None else str(end_s).strip()
    if end_s_str:
        end_dt = parse_dt(end_s_str, assume_end=True)
        return end_dt >= now if end_dt else False
    start_s_str = '' if start_s is None else str(start_s).strip()
    if not start_s_str: return False
    sdt = parse_dt(start_s_str)
    if not sdt: return False
    return sdt.replace(hour=23, minute=59, second=59) >= now

def remain_text(end_s, start_s=None):
    now = now_kst()
    end_s_str = '' if end_s is None else str(end_s).strip()
    if end_s_str:
        end_dt = parse_dt(end_s_str, assume_end=True)
        if not end_dt: return "종료됨"
    else:
        start_s_str = '' if start_s is None else str(start_s).strip()
        if not start_s_str: return ""
        sdt = parse_dt(start_s_str)
        if not sdt: return ""
        end_dt = sdt.replace(hour=23, minute=59, second=59)
    delta = end_dt - now
    if delta.total_seconds() <= 0: return "종료됨"
    tm = int(delta.total_seconds() // 60)
    d, h, m = tm // 1440, (tm // 60) % 24, tm % 60
    if d > 0: return f"남음 {d}일 {h}시간"
    if h > 0: return f"남음 {h}시간 {m}분"
    return f"남음 {m}분"

def fmt_start(start_s):
    dt = parse_dt(start_s)
    return dt.strftime("%m월 %d일 %H:%M") if dt else (start_s or "").strip()

# =========================================================
# 4) 데이터 핸들링
# =========================================================
def _event_capacity_label(capacity, is_unlimited) -> str:
    if is_unlimited == 1: return "∞"
    try:
        cap_i = int(float(capacity))
        return "∞" if cap_i <= 0 else str(cap_i)
    except: return "∞"

def _get_event_counts(cur, event_ids, user_id):
    if not event_ids: return {}, {}
    counts = {}
    joined = {}
    cur.execute(
        "SELECT event_id, COUNT(*) FROM event_participants WHERE event_id = ANY(%s) GROUP BY event_id",
        (event_ids,)
    )
    for eid, cnt in cur.fetchall(): counts[eid] = int(cnt)
    if user_id:
        cur.execute(
            "SELECT event_id FROM event_participants WHERE user_id=%s AND event_id = ANY(%s)",
            (user_id, event_ids)
        )
        for (eid,) in cur.fetchall(): joined[eid] = True
    return counts, joined

def get_joined_event_id(user_id: str):
    with get_cursor() as cur:
        cur.execute(
            'SELECT p.event_id, e."end", e."start" FROM event_participants p '
            'LEFT JOIN events e ON e.id=p.event_id WHERE p.user_id=%s ORDER BY p.joined_at DESC',
            (user_id,)
        )
        rows = cur.fetchall()
    for eid, end_s, start_s in rows:
        if is_active_event(end_s, start_s): return eid
    return None

def get_event_by_id(event_id: str):
    with get_cursor() as cur:
        cur.execute('SELECT id,title,photo,"start","end",addr,lat,lng,created_at,user_id,capacity,is_unlimited FROM events WHERE id=%s', (event_id,))
        row = cur.fetchone()
    if not row: return None
    keys = ["id","title","photo","start","end","addr","lat","lng","created_at","user_id","capacity","is_unlimited"]
    return dict(zip(keys, row))

def list_active_events(limit: int = 500):
    with get_cursor() as cur:
        cur.execute('SELECT id,title,photo,"start","end",addr,lat,lng,created_at,user_id,capacity,is_unlimited FROM events ORDER BY created_at DESC LIMIT %s', (limit,))
        rows = cur.fetchall()
    keys = ["id","title","photo","start","end","addr","lat","lng","created_at","user_id","capacity","is_unlimited"]
    events = [dict(zip(keys, r)) for r in rows]
    return [e for e in events if is_active_event(e.get("end"), e.get("start"))]

def toggle_join(user_id: str, event_id: str):
    ev = get_event_by_id(event_id)
    if not ev or not is_active_event(ev.get("end"), ev.get("start")):
        return False, "유효하지 않은 이벤트입니다.", None

    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM event_participants WHERE event_id=%s AND user_id=%s", (event_id, user_id))
        if cur.fetchone():
            cur.execute("DELETE FROM event_participants WHERE event_id=%s AND user_id=%s", (event_id, user_id))
            return True, "빠지기 완료", False
        
        cur.execute("SELECT event_id FROM event_participants WHERE user_id=%s", (user_id,))
        active_joins = cur.fetchall()
        for (eid,) in active_joins:
            e_tmp = get_event_by_id(eid)
            if e_tmp and is_active_event(e_tmp.get("end"), e_tmp.get("start")):
                return False, "이미 참여 중인 활동이 있습니다.", None

        cap_label = _event_capacity_label(ev.get("capacity"), ev.get("is_unlimited"))
        if cap_label != "∞":
            cur.execute("SELECT COUNT(*) FROM event_participants WHERE event_id=%s", (event_id,))
            if cur.fetchone()[0] >= int(cap_label): return False, "정원이 가득 찼습니다.", None

        cur.execute("INSERT INTO event_participants(event_id,user_id,joined_at) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                    (event_id, user_id, now_kst().isoformat()))
        return True, "참여 완료", True

# =========================================================
# 5) 즐겨찾기 (Postgres Upsert)
# =========================================================
def get_top_favs(limit: int = 10):
    with get_cursor() as cur:
        cur.execute("SELECT name, count FROM favs ORDER BY count DESC, name ASC LIMIT %s", (limit,))
        return [{"name": r[0], "count": int(r[1])} for r in cur.fetchall()]

def bump_fav(name: str):
    name = (name or "").strip()
    if not name: return
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO favs(name, count) VALUES(%s, 1)
            ON CONFLICT (name) DO UPDATE SET count = favs.count + 1
        """, (name,))

def delete_fav(name: str):
    with get_cursor() as cur:
        cur.execute("DELETE FROM favs WHERE name=%s", (name.strip(),))

# =========================================================
# 6) Kakao & Geocode (동일)
# =========================================================
def kakao_search(keyword: str, size: int = 8):
    if not KAKAO_REST_API_KEY: return []
    try:
        r = requests.get("https://dapi.kakao.com/v2/local/search/keyword.json",
                         headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"},
                         params={"query": keyword, "size": size}, timeout=5)
        return [{"name": d['place_name'], "addr": d.get('road_address_name') or d.get('address_name'), "x": float(d['x']), "y": float(d['y'])}
                for d in r.json().get("documents", [])]
    except: return []

def kakao_geocode(addr: str):
    if not KAKAO_REST_API_KEY: return None
    try:
        r = requests.get("https://dapi.kakao.com/v2/local/search/address.json",
                         headers={"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"},
                         params={"query": addr, "size": 1}, timeout=5)
        docs = r.json().get('documents', [])
        if docs: return float(docs[0]['y']), float(docs[0]['x'])
        return None
    except: return None

def ensure_event_coords(event: dict):
    eid = event.get('id')
    if not eid or abs(float(event.get('lat') or 0)) > 0.01: return
    got = kakao_geocode(event.get('addr') or '')
    if got:
        lat, lng = got
        with get_cursor() as cur:
            cur.execute("UPDATE events SET lat=%s, lng=%s WHERE id=%s", (lat, lng, eid))
        event['lat'], event['lng'] = lat, lng

# =========================================================
# 7) FastAPI App & Auth
# =========================================================
app = FastAPI()

@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    if path == "/" or path.startswith(("/login", "/signup", "/send_email_otp", "/static", "/healthz", "/assets", "/favicon")):
        return await call_next(request)
    if not get_user_id_from_request(request):
        if path.startswith("/api/"): return JSONResponse({"ok": False, "message": "로그인 필요"}, status_code=401)
        return RedirectResponse(url="/login", status_code=302)
    return await call_next(request)

@app.get("/")
async def root(): return RedirectResponse(url="/app")

@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        with get_cursor() as cur: cur.execute("DELETE FROM sessions WHERE token=%s", (token,))
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp

# --- HTML Templates (기존과 동일하게 유지) ---
def render_safe(template: str, **kwargs) -> str:
    out = template
    for k, v in kwargs.items(): out = out.replace(f"__{k}__", str(v))
    return out

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/><title>오세요 - 로그인</title><style>body{font-family:Pretendard,sans-serif;background:#faf9f6;margin:0;}.wrap{max-width:420px;margin:48px auto;padding:0 18px;}.card{background:#fff;border:1px solid #e5e3dd;border-radius:18px;padding:22px;box-shadow:0 10px 30px rgba(0,0,0,.06);}h1{margin:0 0 6px;font-size:22px;}.muted{color:#6b7280;font-size:13px;margin:0 0 18px;}label{display:block;font-size:13px;color:#374151;margin:12px 0 6px;}input{width:100%;padding:12px;border:1px solid #e5e7eb;border-radius:12px;box-sizing:border-box;font-size:15px;}.btn{width:100%;padding:12px;border:0;border-radius:12px;background:#111;color:#fff;font-size:15px;margin-top:16px;cursor:pointer;}.link{margin-top:12px;font-size:13px;text-align:center;}.err{color:#ef4444;font-size:13px;margin:10px 0;}</style></head><body><div class="wrap"><div class="card"><h1>로그인</h1><p class="muted">오세요 서비스를 이용하려면 로그인해 주세요.</p><form method="post"><label>이메일</label><input name="email" type="email" required/><label>비밀번호</label><input name="password" type="password" required/><button class="btn">로그인</button></form>__ERROR_BLOCK__<div class="link">계정이 없으신가요? <a href="/signup">회원가입</a></div></div></div></body></html>"""

@app.get("/login")
async def login_get(err: str = ""):
    eb = f'<div class="err">{html.escape(err)}</div>' if err else ""
    return HTMLResponse(render_safe(LOGIN_HTML, ERROR_BLOCK=eb))

@app.post("/login")
async def login_post(email: str = Form(...), password: str = Form(...)):
    email = email.strip().lower()
    with get_cursor() as cur:
        cur.execute("SELECT id, pw_hash FROM users WHERE email=%s", (email,))
        row = cur.fetchone()
    if not row or not pw_verify(password, row[1]):
        return RedirectResponse(url="/login?err=" + requests.utils.quote("정보가 일치하지 않습니다."), status_code=302)
    token = create_session(row[0])
    resp = RedirectResponse(url="/app", status_code=302)
    resp.set_cookie(COOKIE_NAME, token, max_age=SESSION_HOURS*3600, httponly=True, samesite="lax", path="/")
    return resp

# --- Signup & OTP ---
@app.post("/send_email_otp")
async def send_email_otp(request: Request):
    try:
        data = await request.json()
        email = data.get("email", "").strip().lower()
        if not email: return JSONResponse({"ok": False, "message": "이메일 입력 필요"})
        otp = "".join([str(re.import_module('random').randint(0,9)) for _ in range(6)])
        exp = now_kst() + timedelta(minutes=10)
        
        with get_cursor() as cur:
            cur.execute("INSERT INTO email_otps(email, otp, expires_at) VALUES(%s,%s,%s) ON CONFLICT(email) DO UPDATE SET otp=EXCLUDED.otp, expires_at=EXCLUDED.expires_at",
                        (email, otp, exp.isoformat()))
        
        # SMTP
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(f"오세요 인증번호: {otp}", "plain", "utf-8")
        msg["Subject"] = "[오세요] 이메일 인증"
        msg["From"] = os.getenv("FROM_EMAIL")
        msg["To"] = email
        with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", 587))) as s:
            s.starttls(); s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS"))
            s.send_message(msg)
        return JSONResponse({"ok": True})
    except Exception as e: return JSONResponse({"ok": False, "message": str(e)})

@app.post("/signup")
async def signup_post(email:str=Form(...), otp:str=Form(...), password:str=Form(...), name:str=Form(...), gender:str=Form(""), birth:str=Form("")):
    email = email.strip().lower()
    with get_cursor() as cur:
        cur.execute("SELECT otp, expires_at FROM email_otps WHERE email=%s", (email,))
        row = cur.fetchone()
        if not row or row[0] != otp or datetime.fromisoformat(row[1]) < now_kst():
            return RedirectResponse(url="/signup?err=인증실패", status_code=302)
        cur.execute("SELECT 1 FROM users WHERE email=%s", (email,))
        if cur.fetchone(): return RedirectResponse(url="/signup?err=이미가입", status_code=302)
        uid = uuid.uuid4().hex
        salt = uuid.uuid4().hex[:12]
        cur.execute("INSERT INTO users(id,email,pw_hash,name,gender,birth,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (uid, email, pw_hash(password, salt), name.strip(), gender, birth, now_kst().isoformat()))
    return RedirectResponse(url="/login?err=가입완료", status_code=302)

# ... (SignUp HTML, JS 등은 이전 코드와 동일하므로 지면상 생략하거나 그대로 활용) ...

# =========================================================
# 8) JSON API (Postgres)
# =========================================================
@app.get("/api/events_json")
async def api_events_json(request: Request):
    uid = require_user(request)
    events = list_active_events(1500)
    with get_cursor() as cur:
        ids = [e["id"] for e in events]
        counts, joined = _get_event_counts(cur, ids, uid)
    my_joined_id = get_joined_event_id(uid)
    out = []
    for e in events:
        ensure_event_coords(e)
        eid = e["id"]
        cap = _event_capacity_label(e.get("capacity"), e.get("is_unlimited"))
        cnt = counts.get(eid, 0)
        joined_me = bool(joined.get(eid, False))
        is_full = (cap != "∞" and cnt >= int(cap))
        out.append({
            "id": eid, "title": e['title'], "addr": e['addr'], "lat": e['lat'], "lng": e['lng'],
            "start": e['start'], "end": e['end'], "start_fmt": fmt_start(e['start']),
            "remain": remain_text(e['end'], e['start']), "photo": e['photo'],
            "count": cnt, "cap_label": cap, "joined": joined_me,
            "can_join": (not is_full) and (my_joined_id is None or my_joined_id == eid),
            "is_full": is_full
        })
    return JSONResponse({"ok": True, "events": out})

@app.post("/api/toggle_join")
async def api_toggle_join(request: Request):
    uid = require_user(request)
    payload = await request.json()
    ok, msg, j_now = toggle_join(uid, payload.get("event_id", ""))
    return JSONResponse({"ok": ok, "message": msg, "joined": bool(j_now)})

# =========================================================
# 9) Gradio UI (주요 CRUD 함수만 Postgres용으로 수정)
# =========================================================
def encode_img_to_b64(img_np) -> str:
    if img_np is None: return ""
    im = Image.fromarray(img_np.astype("uint8")).convert("RGB")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

def save_event(title, img_np, start_d, start_h, start_m, end_d, end_h, end_m, addr_t, picked_a, cap, unlim, req: gr.Request):
    uid = require_user(req.request)
    title = (title or "").strip()
    if not title: return gr.update(value="제목 필요"), gr.update(visible=True), gr.update(visible=True), True
    
    # Coordinates & Address
    lat, lng, addr = None, None, addr_t
    if picked_a:
        p = json.loads(picked_a)
        addr, lat, lng = p.get('addr', addr), p.get('lat'), p.get('lng')
    if lat is None:
        got = kakao_geocode(addr)
        if got: lat, lng = got

    def _comb(d, h, m):
        if not d: return ""
        return f"{str(d)[:10]} {str(h).zfill(2)}:{str(m).zfill(2)}"

    start_s = _comb(start_d, start_h, start_m)
    # End date fallback
    if not end_d and (str(start_h) != str(end_h) or str(start_m) != str(end_m)): end_d = start_d
    end_s = _comb(end_d, end_h, end_m)

    photo_b64 = encode_img_to_b64(img_np)
    is_unlim = 1 if unlim else 0
    cap_val = 0 if is_unlim else max(1, min(99, int(cap or 10)))

    eid = uuid.uuid4().hex
    with get_cursor() as cur:
        cur.execute(
            'INSERT INTO events(id,title,photo,"start","end",addr,lat,lng,created_at,user_id,capacity,is_unlimited) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (eid, title, photo_b64, start_s, end_s, addr, lat, lng, now_kst().isoformat(), uid, cap_val, is_unlim)
        )
    return gr.update(value="등록 완료"), gr.update(visible=False), gr.update(visible=False), False

def delete_my_event(choice, req: gr.Request):
    uid = require_user(req.request)
    if not choice: return gr.update(value="선택 필요"), gr.update()
    m = re.search(r"\(([0-9a-f]{6})\)\s*$", choice.strip())
    if not m: return gr.update(value="ID 파싱 불가"), gr.update()
    prefix = m.group(1)
    with get_cursor() as cur:
        cur.execute("SELECT id FROM events WHERE id LIKE %s AND user_id=%s", (prefix + "%", uid))
        row = cur.fetchone()
        if not row: return gr.update(value="권한 없음"), gr.update()
        cur.execute("DELETE FROM events WHERE id=%s", (row[0],))
        cur.execute("DELETE FROM event_participants WHERE event_id=%s", (row[0],))
    return gr.update(value="삭제 완료"), gr.update(choices=my_events_for_user(uid), value=None)

# (Gradio UI 코드 블록은 기존 SQLite 버전과 동일하게 유지하되 위 함수들을 연결)
# ... 나머지 Gradio Blocks 코드는 사용자가 제공한 원본 유지 ...

# =========================================================
# 메인 실행
# =========================================================
# Gradio mount 등 마무리 (기존 코드와 동일)
# ...
if __name__ == '__main__':
    # DB Pool 상태 확인
    if db_pool:
        uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('PORT','8000')))
    else:
        print("CRITICAL: Database Pool not initialized. Check DATABASE_URL.")
