# -*- coding: utf-8 -*-
print("### DEPLOY MARKER: UI_FIX_V11 ###", flush=True)
import os
import io
import re
import uuid
import json
import base64
import sqlite3
import hashlib
import html
from datetime import datetime, timedelta, timezone

import uvicorn

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response

import requests
from PIL import Image


# --- BEGIN: Render-safe gradio_client bool-schema hotfix (patch schema parser safely) ---
# Render 환경에서 gradio / gradio_client 조합에 따라 JSON Schema에 boolean schema(True/False)가 섞여 나오면
# gradio_client.utils가 이를 dict로 가정하고 api_info 생성 과정에서 500을 냅니다.
# (예: APIInfoParseError: Cannot parse schema True)
#
# 아래 패치는 "함수 시그니처(인자 개수)" 차이로 또 터지지 않게 *args/**kwargs로 감싸서
# schema가 bool이거나 파싱이 실패하면 안전하게 "Any"로 떨어뜨립니다.
try:
    from gradio_client import utils as _gc_utils  # type: ignore

    if not getattr(_gc_utils, "_OSEYO_PATCHED_BOOL_SCHEMA", False):
        _APIInfoParseError = getattr(_gc_utils, "APIInfoParseError", Exception)

        def _schema_from(args, kwargs):
            if "schema" in kwargs:
                return kwargs.get("schema")
            return args[0] if args else None

        def _wrap(orig):
            def _wrapped(*args, **kwargs):
                schema = _schema_from(args, kwargs)
                # JSON Schema boolean schema(True/False) 방어
                if isinstance(schema, bool):
                    return "Any"
                try:
                    return orig(*args, **kwargs)
                except _APIInfoParseError:
                    # api_info 생성에서 타입 힌트용이므로, 파싱 실패시에도 앱은 살아있게 둠
                    return "Any"
                except TypeError:
                    # 시그니처가 다른 버전 대비: 최소 인자로 재시도
                    try:
                        return orig(schema)
                    except Exception:
                        return "Any"
                except Exception:
                    raise
            return _wrapped

        # Patch public + private helpers if present
        if hasattr(_gc_utils, "json_schema_to_python_type"):
            _gc_utils.json_schema_to_python_type = _wrap(_gc_utils.json_schema_to_python_type)  # type: ignore
        if hasattr(_gc_utils, "_json_schema_to_python_type"):
            _gc_utils._json_schema_to_python_type = _wrap(_gc_utils._json_schema_to_python_type)  # type: ignore
        if hasattr(_gc_utils, "get_type"):
            _gc_utils.get_type = _wrap(_gc_utils.get_type)  # type: ignore

        _gc_utils._OSEYO_PATCHED_BOOL_SCHEMA = True
except Exception:
    pass
# --- END ---

import gradio as gr

# =========================================================
# 0) 시간/키
# =========================================================
KST = timezone(timedelta(hours=9))


def now_kst():
    return datetime.now(KST)


KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_JAVASCRIPT_KEY = os.getenv("KAKAO_JAVASCRIPT_KEY", "").strip()

COOKIE_NAME = "oseyo_session"
SESSION_HOURS = 24 * 7  # 7일


# =========================================================
# 1) DB 경로 (기존 DB 최대한 유지)
# =========================================================
def render_safe(template: str, **kwargs) -> str:
    """
    template 안에 __KEY__ 형태의 토큰을 kwargs 값으로 치환한다.
    .format()을 쓰지 않으므로 CSS { } 때문에 터지지 않는다.
    """
    out = template
    for k, v in kwargs.items():
        out = out.replace(f"__{k}__", str(v))
    return out


def pick_db_path():
    candidates_dirs = ["/var/data", "/tmp"]
    legacy_names = [
        "oseyo_final_email_v1.db",
        "oseyo_final.db",
        "oseyo_final_join_v1.db",
        "oseyo.db",
    ]

    # 1) 먼저 "이미 존재하는" DB를 우선 사용 (데이터 보존)
    for d in candidates_dirs:
        for name in legacy_names:
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p

    # 2) 없으면, 쓰기 가능한 위치에 기본 DB 생성
    for d in candidates_dirs:
        try:
            os.makedirs(d, exist_ok=True)
            test = os.path.join(d, ".writetest")
            with open(test, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(test)
            return os.path.join(d, legacy_names[0])
        except Exception:
            continue

    return os.path.join("/tmp", legacy_names[0])


DB_PATH = pick_db_path()
print(f"[DB] Using: {DB_PATH}")


def db_conn():
    # timeout을 줘서 동시 접근시 'database is locked'로 바로 죽지 않게 함
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)


def _col_exists(con, table: str, col: str) -> bool:
    cur = con.execute(f"PRAGMA table_info({table});")
    cols = [r[1] for r in cur.fetchall()]
    return col in cols


def init_db():
    with db_conn() as con:
        # Render(동시요청)에서 SQLite 잠금 완화
        try:
            con.execute("PRAGMA journal_mode=WAL;")
            con.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass
        # users
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              id TEXT PRIMARY KEY,
              email TEXT UNIQUE,
              pw_hash TEXT,
              name TEXT,
              gender TEXT,
              birth TEXT,
              created_at TEXT
            );
            """
        )

        # users 테이블이 "이미 존재"하는 경우(구버전 DB) 컬럼이 없을 수 있음 → 보강
        for c, ddl in [
            ("email", "ALTER TABLE users ADD COLUMN email TEXT;"),
            ("pw_hash", "ALTER TABLE users ADD COLUMN pw_hash TEXT;"),
            ("name", "ALTER TABLE users ADD COLUMN name TEXT;"),
            ("gender", "ALTER TABLE users ADD COLUMN gender TEXT;"),
            ("birth", "ALTER TABLE users ADD COLUMN birth TEXT;"),
            ("created_at", "ALTER TABLE users ADD COLUMN created_at TEXT;"),
        ]:
            if not _col_exists(con, "users", c):
                try:
                    con.execute(ddl)
                except Exception:
                    pass

        # sessions
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              token TEXT PRIMARY KEY,
              user_id TEXT,
              expires_at TEXT
            );
            """
        )

        # email otp - 테이블 생성
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS email_otps (
              email TEXT PRIMARY KEY,
              otp TEXT,
              expires_at TEXT
            );
            """
        )
        
        # ⭐ email_otps 테이블 컬럼 보강 (추가)
        if not _col_exists(con, "email_otps", "otp"):
            try:
                con.execute("ALTER TABLE email_otps ADD COLUMN otp TEXT;")
            except Exception:
                pass
        
        if not _col_exists(con, "email_otps", "expires_at"):
            try:
                con.execute("ALTER TABLE email_otps ADD COLUMN expires_at TEXT;")
            except Exception:
                pass

        # events
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY,
              title TEXT,
              photo TEXT,
              start TEXT,
              end TEXT,
              addr TEXT,
              lat REAL,
              lng REAL,
              created_at TEXT,
              user_id TEXT
            );
            """
        )

        if not _col_exists(con, "events", "user_id"):
            try:
                con.execute("ALTER TABLE events ADD COLUMN user_id TEXT;")
            except Exception:
                pass

        if not _col_exists(con, "events", "capacity"):
            try:
                con.execute("ALTER TABLE events ADD COLUMN capacity INTEGER;")
            except Exception:
                pass
        if not _col_exists(con, "events", "is_unlimited"):
            try:
                con.execute("ALTER TABLE events ADD COLUMN is_unlimited INTEGER;")
            except Exception:
                pass

        # Fix legacy mismatch: our logic stores capacity=0 when unlimited=1.
        # So if capacity>0 but is_unlimited=1, the flag is wrong → normalize.
        try:
            con.execute("UPDATE events SET is_unlimited=0 WHERE is_unlimited=1 AND capacity IS NOT NULL AND CAST(capacity AS INTEGER) > 0;")
        except Exception:
            pass


        # favorites
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS favs (
              name TEXT PRIMARY KEY,
              count INTEGER DEFAULT 1
            );
            """
        )

        # participants
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS event_participants (
              event_id TEXT,
              user_id TEXT,
              joined_at TEXT,
              PRIMARY KEY(event_id, user_id)
            );
            """
        )

        con.commit()

init_db()


# =========================================================
# 2) 비밀번호/세션
# =========================================================
def pw_hash(password: str, salt: str) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), 150_000
    )
    return f"{salt}${dk.hex()}"


def pw_verify(password: str, stored: str) -> bool:
    try:
        if not stored:
            return False
        salt, _ = stored.split("$", 1)
        return pw_hash(password, salt) == stored
    except Exception:
        return False


def create_session(user_id: str) -> str:
    token = uuid.uuid4().hex
    expires = now_kst() + timedelta(hours=SESSION_HOURS)
    with db_conn() as con:
        con.execute(
            "INSERT INTO sessions(token, user_id, expires_at) VALUES(?,?,?)",
            (token, user_id, expires.isoformat()),
        )
        con.commit()
    return token


def get_user_id_from_request(req: Request):
    token = req.cookies.get(COOKIE_NAME)
    if not token:
        return None
    with db_conn() as con:
        row = con.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token=?",
            (token,),
        ).fetchone()
    if not row:
        return None
    user_id, exp = row
    try:
        if datetime.fromisoformat(exp) < now_kst():
            return None
    except Exception:
        return None
    return user_id


def require_user(req: Request):
    uid = get_user_id_from_request(req)
    if not uid:
        raise PermissionError("로그인이 필요합니다.")
    return uid


# =========================================================
# 3) 날짜 파싱/남은 시간
# =========================================================
_DT_FORMATS = [
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%dT%H:%M:%S",
]


def parse_dt(s):
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=KST)
        return dt.astimezone(KST)
    except Exception:
        pass
    for fmt in _DT_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=KST)
        except Exception:
            continue
    return None


def is_active_event(end_s):
    end_dt = parse_dt(end_s)
    if end_dt is None:
        return True
    return end_dt >= now_kst()


def remain_text(end_s):
    end_dt = parse_dt(end_s)
    if end_dt is None:
        return ""
    delta = end_dt - now_kst()
    if delta.total_seconds() <= 0:
        return "종료됨"
    total_min = int(delta.total_seconds() // 60)
    days = total_min // (60 * 24)
    hours = (total_min // 60) % 24
    mins = total_min % 60
    if days > 0:
        return f"남음 {days}일 {hours}시간"
    if hours > 0:
        return f"남음 {hours}시간 {mins}분"
    return f"남음 {mins}분"


def fmt_start(start_s):
    dt = parse_dt(start_s)
    if not dt:
        return (start_s or "").strip()
    return dt.strftime("%m월 %d일 %H:%M")


# =========================================================
# 4) 이벤트/참여 데이터
# =========================================================
def _event_capacity_label(capacity, is_unlimited) -> str:
    """정원 표시용 라벨.

    - unlimited는 DB에 1/0으로 저장하지만, 과거 데이터/마이그레이션 중 is_unlimited가 1로 잘못 들어간 경우가 있음.
    - 우리 로직상 unlimited=True이면 capacity를 0으로 저장하므로, capacity>0이면 정원값을 우선한다.
    """
    try:
        # unlimited 플래그가 켜져 있어도 capacity가 양수면 capacity를 신뢰
        if is_unlimited == 1:
            if capacity is None:
                return "∞"
            try:
                cap_i = int(float(capacity))
            except Exception:
                return "∞"
            return "∞" if cap_i <= 0 else str(cap_i)

        if capacity is None:
            return "∞"
        cap_i = int(float(capacity))
        if cap_i <= 0:
            return "∞"
        return str(cap_i)
    except Exception:
        return "∞"


def _get_event_counts(con, event_ids, user_id):
    if not event_ids:
        return {}, {}
    q_marks = ",".join(["?"] * len(event_ids))
    counts = {}
    joined = {}
    for eid, cnt in con.execute(
        f"SELECT event_id, COUNT(*) FROM event_participants WHERE event_id IN ({q_marks}) GROUP BY event_id",
        tuple(event_ids),
    ).fetchall():
        counts[eid] = int(cnt)
    if user_id:
        for (eid,) in con.execute(
            f"SELECT event_id FROM event_participants WHERE user_id=? AND event_id IN ({q_marks})",
            (user_id, *event_ids),
        ).fetchall():
            joined[eid] = True
    return counts, joined


def cleanup_ended_participation(user_id: str):
    with db_conn() as con:
        rows = con.execute(
            "SELECT p.event_id, e.end FROM event_participants p LEFT JOIN events e ON e.id=p.event_id WHERE p.user_id=?",
            (user_id,),
        ).fetchall()
        to_delete = []
        for eid, end_s in rows:
            if not is_active_event(end_s):
                to_delete.append(eid)
        if to_delete:
            for eid in to_delete:
                con.execute(
                    "DELETE FROM event_participants WHERE event_id=? AND user_id=?",
                    (eid, user_id),
                )
            con.commit()


def get_joined_event_id(user_id: str):
    """
    종료된 이벤트 참여는 'DB에서 매번 삭제'하지 말고, 조회 시 active만 골라서 반환한다.
    (지도 iframe이 2.5초마다 호출할 때도 쓰기 트랜잭션이 발생하지 않게 해서 네트워크 오류/잠금 감소)
    """
    with db_conn() as con:
        rows = con.execute(
            "SELECT p.event_id, p.joined_at, e.end "
            "FROM event_participants p LEFT JOIN events e ON e.id=p.event_id "
            "WHERE p.user_id=? ORDER BY p.joined_at DESC",
            (user_id,),
        ).fetchall()
    for eid, _joined_at, end_s in rows:
        if is_active_event(end_s):
            return eid
    return None


def get_event_by_id(event_id: str):
    with db_conn() as con:
        row = con.execute(
            "SELECT id,title,photo,start,end,addr,lat,lng,created_at,user_id,capacity,is_unlimited FROM events WHERE id=?",
            (event_id,),
        ).fetchone()
    if not row:
        return None
    keys = [
        "id",
        "title",
        "photo",
        "start",
        "end",
        "addr",
        "lat",
        "lng",
        "created_at",
        "user_id",
        "capacity",
        "is_unlimited",
    ]
    return dict(zip(keys, row))


def list_active_events(limit: int = 500):
    with db_conn() as con:
        rows = con.execute(
            "SELECT id,title,photo,start,end,addr,lat,lng,created_at,user_id,capacity,is_unlimited FROM events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    keys = [
        "id",
        "title",
        "photo",
        "start",
        "end",
        "addr",
        "lat",
        "lng",
        "created_at",
        "user_id",
        "capacity",
        "is_unlimited",
    ]
    events = [dict(zip(keys, r)) for r in rows]
    return [e for e in events if is_active_event(e.get("end"))]


def events_for_page(user_id: str, page: int, page_size: int):
    all_events = list_active_events(limit=1000)
    start = page * page_size
    chunk = all_events[start : start + page_size]

    with db_conn() as con:
        ids = [e["id"] for e in chunk]
        counts, joined = _get_event_counts(con, ids, user_id)

    my_joined_id = get_joined_event_id(user_id)

    for e in chunk:
        eid = e["id"]
        e["count"] = counts.get(eid, 0)
        e["joined"] = bool(joined.get(eid, False))
        cap_label = _event_capacity_label(e.get("capacity"), e.get("is_unlimited"))
        e["cap_label"] = cap_label

        is_full = False
        if cap_label != "∞":
            try:
                is_full = e["count"] >= int(cap_label)
            except Exception:
                is_full = False
        e["is_full"] = is_full
        e["can_join"] = (not is_full) and (my_joined_id is None or my_joined_id == eid)

    total_pages = (len(all_events) + page_size - 1) // page_size
    return chunk, total_pages, my_joined_id


def toggle_join(user_id: str, event_id: str):
    cleanup_ended_participation(user_id)
    ev = get_event_by_id(event_id)
    if not ev:
        return False, "이벤트를 찾을 수 없습니다.", None
    if not is_active_event(ev.get("end")):
        return False, "이미 종료된 이벤트입니다.", None

    with db_conn() as con:
        already = con.execute(
            "SELECT 1 FROM event_participants WHERE event_id=? AND user_id=?",
            (event_id, user_id),
        ).fetchone()

        if already:
            con.execute(
                "DELETE FROM event_participants WHERE event_id=? AND user_id=?",
                (event_id, user_id),
            )
            con.commit()
            return True, "빠지기 완료", False

        row = con.execute(
            "SELECT event_id FROM event_participants WHERE user_id=? LIMIT 1",
            (user_id,),
        ).fetchone()
        if row and row[0] != event_id:
            return (
                False,
                "다른 활동에 참여중입니다. 먼저 빠지기 후 참여할 수 있습니다.",
                None,
            )

        cap_label = _event_capacity_label(ev.get("capacity"), ev.get("is_unlimited"))
        if cap_label != "∞":
            cnt = con.execute(
                "SELECT COUNT(*) FROM event_participants WHERE event_id=?",
                (event_id,),
            ).fetchone()[0]
            if cnt >= int(cap_label):
                return False, "정원이 가득 찼습니다.", None

        con.execute(
            "INSERT OR IGNORE INTO event_participants(event_id,user_id,joined_at) VALUES(?,?,?)",
            (event_id, user_id, now_kst().isoformat()),
        )
        con.commit()
        return True, "참여 완료", True


# =========================================================
# 5) 즐겨찾기
# =========================================================
def get_top_favs(limit: int = 10):
    with db_conn() as con:
        rows = con.execute(
            "SELECT name, count FROM favs ORDER BY count DESC, name ASC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"name": r[0], "count": int(r[1])} for r in rows]


def bump_fav(name: str):
    name = (name or "").strip()
    if not name:
        return
    with db_conn() as con:
        row = con.execute("SELECT count FROM favs WHERE name=?", (name,)).fetchone()
        if row:
            con.execute("UPDATE favs SET count=count+1 WHERE name=?", (name,))
        else:
            con.execute("INSERT INTO favs(name,count) VALUES(?,1)", (name,))
        con.commit()


def delete_fav(name: str):
    name = (name or "").strip()
    if not name:
        return
    with db_conn() as con:
        con.execute("DELETE FROM favs WHERE name=?", (name,))
        con.commit()


# =========================================================
# 6) Kakao 주소 검색 (REST)
# =========================================================
def kakao_search(keyword: str, size: int = 8):
    if not KAKAO_REST_API_KEY:
        return []
    kw = (keyword or "").strip()
    if not kw:
        return []
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
    params = {"query": kw, "size": size}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=8)
        if r.status_code != 200:
            return []
        data = r.json()
        out = []
        for d in data.get("documents", []):
            out.append(
                {
                    "name": d.get("place_name") or "",
                    "addr": d.get("road_address_name") or d.get("address_name") or "",
                    "x": float(d.get("x") or 0),
                    "y": float(d.get("y") or 0),
                }
            )
        return out
    except Exception:
        return []




# Kakao 주소→좌표(geocode) (REST)
def kakao_geocode(addr: str):
    """도로명/지번 주소를 좌표로 변환. 실패하면 None 반환.

    지도에 마커가 안 찍히는 대부분의 원인이 lat/lng가 NULL(또는 0)인 경우라
    사용자가 검색 결과를 선택하지 않고 주소만 입력했을 때도 좌표를 채워준다.
    """
    if not KAKAO_REST_API_KEY:
        return None
    q = (addr or '').strip()
    if not q:
        return None
    url = 'https://dapi.kakao.com/v2/local/search/address.json'
    headers = {'Authorization': f'KakaoAK {KAKAO_REST_API_KEY}'}
    try:
        r = requests.get(url, headers=headers, params={'query': q, 'size': 1}, timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        docs = data.get('documents') or []
        if not docs:
            # 주소로 안 되면 키워드로 한번 더
            docs2 = kakao_search(q, size=1)
            if docs2:
                d = docs2[0]
                return float(d.get('y') or 0), float(d.get('x') or 0)
            return None
        d0 = docs[0]
        # kakao address API는 x=lng, y=lat
        lat = float(d0.get('y') or 0)
        lng = float(d0.get('x') or 0)
        if abs(lat) < 0.01 or abs(lng) < 0.01:
            return None
        return lat, lng
    except Exception:
        return None


# 이벤트 좌표 보강(프로세스 내 1회만 시도)
_GEOCODE_TRIED = set()

def ensure_event_coords(event: dict):
    try:
        eid = event.get('id')
        if not eid or eid in _GEOCODE_TRIED:
            return
        lat = event.get('lat')
        lng = event.get('lng')
        # 0/None은 미설정으로 취급
        if lat and lng and abs(float(lat)) > 0.01 and abs(float(lng)) > 0.01:
            return
        got = kakao_geocode(event.get('addr') or '')
        _GEOCODE_TRIED.add(eid)
        if not got:
            return
        lat2, lng2 = got
        with db_conn() as con:
            con.execute('UPDATE events SET lat=?, lng=? WHERE id=?', (lat2, lng2, eid))
            con.commit()
        event['lat'] = lat2
        event['lng'] = lng2
    except Exception:
        return
# =========================================================
# 7) FastAPI 앱 (로그인/회원가입/지도/JSON API)
# =========================================================
app = FastAPI()


@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/app", status_code=302)



@app.get("/healthz")
async def healthz():
    # Render health check endpoint (must return 200 without auth)
    return {"ok": True}
PUBLIC_PATH_PREFIXES = (
    "/login",
    "/signup",
    "/send_email_otp",
    "/static",
    "/healthz",

)


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path

    if path.startswith("/api/"):
        uid = get_user_id_from_request(request)
        if not uid:
            return JSONResponse({"ok": False, "message": "로그인이 필요합니다."}, status_code=401)
        return await call_next(request)

    if path.startswith(PUBLIC_PATH_PREFIXES):
        return await call_next(request)

    if path.startswith("/assets") or path.startswith("/favicon"):
        return await call_next(request)

    uid = get_user_id_from_request(request)
    if not uid:
        return RedirectResponse(url="/login", status_code=302)
    return await call_next(request)


# -------------------------
# Logout
# -------------------------
@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(COOKIE_NAME)
    if token:
        try:
            with db_conn() as con:
                con.execute("DELETE FROM sessions WHERE token=?", (token,))
                con.commit()
        except Exception:
            pass
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# -------------------------
# Login page
# -------------------------
LOGIN_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>오세요 - 로그인</title>
<style>
  body{font-family:Pretendard,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#faf9f6;margin:0;}
  .wrap{max-width:420px;margin:48px auto;padding:0 18px;}
  .card{background:#fff;border:1px solid #e5e3dd;border-radius:18px;padding:22px;box-shadow:0 10px 30px rgba(0,0,0,.06);}
  h1{margin:0 0 6px;font-size:22px;}
  .muted{color:#6b7280;font-size:13px;margin:0 0 18px;}
  label{display:block;font-size:13px;color:#374151;margin:12px 0 6px;}
  input{width:100%;padding:12px 12px;border:1px solid #e5e7eb;border-radius:12px;font-size:15px;outline:none;}
  input:focus{border-color:#111827;}
  .btn{width:100%;padding:12px 14px;border:0;border-radius:12px;background:#111;color:#fff;font-size:15px;margin-top:16px;cursor:pointer;}
  .link{margin-top:12px;font-size:13px;text-align:center;color:#6b7280;}
  .link a{color:#111;text-decoration:none;font-weight:600;}
  .err{color:#ef4444;font-size:13px;margin:10px 0 0;white-space:pre-wrap;}
</style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>로그인</h1>
      <p class="muted">오세요 서비스를 이용하려면 로그인해 주세요.</p>
      <form method="post" action="/login">
        <label>이메일</label>
        <input name="email" type="email" required placeholder="you@example.com" />
        <label>비밀번호</label>
        <input name="password" type="password" required placeholder="비밀번호" />
        <button class="btn" type="submit">로그인</button>
      </form>

      __ERROR_BLOCK__

      <div class="link">계정이 없으신가요? <a href="/signup">회원가입</a></div>
    </div>
  </div>
</body>
</html>
"""


@app.get("/login")
async def login_get(request: Request):
    err = request.query_params.get("err", "")
    error_block = f'<div class="err">{html.escape(err)}</div>' if err else ""
    return HTMLResponse(render_safe(LOGIN_HTML, ERROR_BLOCK=error_block))


@app.post("/login")
async def login_post(email: str = Form(...), password: str = Form(...)):
    email = (email or "").strip().lower()

    with db_conn() as con:
        try:
            row = con.execute(
                "SELECT id, pw_hash FROM users WHERE email=?",
                (email,),
            ).fetchone()
        except sqlite3.OperationalError:
            row = con.execute(
                "SELECT id, pw_hash FROM users WHERE id=?",
                (email,),
            ).fetchone()

    if not row:
        return RedirectResponse(
            url="/login?err=" + requests.utils.quote("존재하지 않는 계정입니다."),
            status_code=302,
        )

    uid, ph = row
    if not pw_verify(password, ph):
        return RedirectResponse(
            url="/login?err=" + requests.utils.quote("비밀번호가 올바르지 않습니다."),
            status_code=302,
        )

    token = create_session(uid)
    resp = RedirectResponse(url="/app", status_code=302)
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_HOURS * 3600,
        httponly=True,
        samesite="lax",
        path="/",
        # secure=True,  # HTTPS 강제하려면 켜도 됨
    )
    return resp


# -------------------------
# Signup page  (★ script 태그는 반드시 HTML 문자열 안에!)
# -------------------------
SIGNUP_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>오세요 - 회원가입</title>
<style>
  body{font-family:Pretendard,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#faf9f6;margin:0;}
  .wrap{max-width:520px;margin:36px auto;padding:0 18px;}
  .card{background:#fff;border:1px solid #e5e3dd;border-radius:18px;padding:22px;box-shadow:0 10px 30px rgba(0,0,0,.06);}
  h1{margin:0 0 4px;font-size:22px;text-align:center;}
  .muted{color:#6b7280;font-size:13px;margin:0 0 18px;text-align:center;}
  label{display:block;font-size:13px;color:#374151;margin:12px 0 6px;}
  input, select{width:100%;padding:12px 12px;border:1px solid #e5e7eb;border-radius:12px;font-size:15px;outline:none;background:#fff;}
  input:focus, select:focus{border-color:#111827;}
  .row{display:flex;gap:10px;align-items:center;}
  .row > *{flex:1;}
  .at{flex:0 0 auto;font-weight:700;color:#6b7280;}
  .btn{width:100%;padding:12px 14px;border:0;border-radius:12px;background:#111;color:#fff;font-size:15px;margin-top:16px;cursor:pointer;}
  .btn-ghost{width:100%;padding:12px 14px;border:1px solid #e5e7eb;border-radius:12px;background:#f3f4f6;color:#111;font-size:14px;cursor:pointer;}
  .link{margin-top:12px;font-size:13px;text-align:center;color:#6b7280;}
  .link a{color:#111;text-decoration:none;font-weight:600;}
  .err{color:#ef4444;font-size:13px;margin:10px 0 0;white-space:pre-wrap;text-align:center;}
  .ok{color:#10b981;font-size:13px;margin:10px 0 0;white-space:pre-wrap;text-align:center;}

  /* 약관 박스 정렬 */
  .terms{border:1px solid #e5e7eb;border-radius:14px;overflow:hidden;margin-top:10px;}
  .terms .trow{display:flex;align-items:center;gap:12px;padding:12px 14px;border-top:1px solid #f1f5f9;}
  .terms .trow:first-child{border-top:0;}
  .terms .left{display:flex;align-items:center;gap:10px;flex:1;min-width:0;}
  .terms input[type="checkbox"]{width:18px;height:18px;margin:0;}
  .terms .label{font-size:14px;color:#111827;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .terms .badge{font-size:12px;padding:4px 8px;border-radius:999px;background:#e5e7eb;color:#111827;flex:0 0 auto;}
  .terms .badge.req{background:#d1fae5;color:#065f46;font-weight:700;}
  .terms .badge.opt{background:#e0e7ff;color:#3730a3;font-weight:700;}
  .terms .sub{color:#6b7280;font-size:12px;margin:0 0 6px;}
  .split{display:flex;gap:10px;}
  .split > *{flex:1;}
</style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>회원가입</h1>
      <p class="muted">이메일 인증 후 가입을 완료해 주세요.</p>

      <form method="post" action="/signup" onsubmit="return validateSignup();">
        <label>이메일</label>
        <div class="row">
          <input id="email_id" name="email_id" placeholder="아이디" required />
          <div class="at">@</div>
          <select id="email_domain_sel" name="email_domain_sel" onchange="onDomainChange()" required>
            <option value="" selected disabled>선택해주세요</option>
            <option value="gmail.com">gmail.com</option>
            <option value="naver.com">naver.com</option>
            <option value="daum.net">daum.net</option>
            <option value="hanmail.net">hanmail.net</option>
            <option value="kakao.com">kakao.com</option>
            <option value="_custom">직접입력</option>
          </select>
        </div>
        <div id="custom_domain_wrap" style="display:none;margin-top:10px;">
          <input id="email_domain_custom" placeholder="도메인 직접입력 (예: example.com)" />
        </div>

        <input type="hidden" id="email_full" name="email" />

        <button type="button" class="btn-ghost" onclick="sendOtp()">이메일 인증하기</button>
        <div id="otp_status" class="__OTP_CLASS__">__OTP_MSG__</div>

        <label>인증번호</label>
        <input name="otp" required placeholder="인증번호 6자리" inputmode="numeric" />

        <label>비밀번호</label>
        <input name="password" type="password" required placeholder="비밀번호" />
        <div class="sub">영문/숫자 포함 8자 이상 권장</div>

        <label>비밀번호 확인</label>
        <input name="password2" type="password" required placeholder="비밀번호 확인" />

        <label>이름</label>
        <input name="name" required placeholder="이름" />

        <div class="split">
          <div>
            <label>성별</label>
            <select name="gender">
              <option value="" selected>선택해주세요</option>
              <option value="F">여</option>
              <option value="M">남</option>
              <option value="X">선택안함</option>
            </select>
          </div>
          <div>
            <label>생년월일</label>
            <input name="birth" type="date" />
          </div>
        </div>

        <label>약관동의</label>
        <div class="terms">
          <div class="trow">
            <div class="left">
              <input id="t_all" type="checkbox" onchange="toggleAllTerms(this)" />
              <div class="label"><b>전체 동의</b></div>
            </div>
            <div class="badge opt">선택항목 포함</div>
          </div>

          <div class="trow">
            <div class="left">
              <input class="t_req" type="checkbox" />
              <div class="label">만 14세 이상입니다</div>
            </div>
            <div class="badge req">필수</div>
          </div>

          <div class="trow">
            <div class="left">
              <input class="t_req" type="checkbox" />
              <div class="label">이용약관 동의</div>
            </div>
            <div class="badge req">필수</div>
          </div>

          <div class="trow">
            <div class="left">
              <input class="t_req" type="checkbox" />
              <div class="label">개인정보 처리방침 동의</div>
            </div>
            <div class="badge req">필수</div>
          </div>

          <div class="trow">
            <div class="left">
              <input class="t_opt" type="checkbox" />
              <div class="label">마케팅 수신 동의</div>
            </div>
            <div class="badge opt">선택</div>
          </div>
        </div>

        <button class="btn" type="submit">회원가입하기</button>
      </form>

      <div class="link">이미 아이디가 있으신가요? <a href="/login">로그인</a></div>
      __ERROR_BLOCK__
    </div>
  </div>

  <script src="/static/signup.js"></script>
</body>
</html>
"""

SIGNUP_JS = r"""
function onDomainChange() {
  const sel = document.getElementById("email_domain_sel").value;
  document.getElementById("custom_domain_wrap").style.display = (sel === "_custom") ? "block" : "none";
}

function buildEmail() {
  const id = (document.getElementById("email_id").value || "").trim();
  const sel = document.getElementById("email_domain_sel").value;
  let domain = sel;
  if (sel === "_custom") {
    domain = (document.getElementById("email_domain_custom").value || "").trim();
  }
  const email = (id && domain) ? (id + "@" + domain) : "";
  const hidden = document.getElementById("email_full");
  if (hidden) hidden.value = email;
  return email;
}

async function sendOtp() {
  const email = buildEmail();
  const box = document.getElementById("otp_status");
  if (!box) { alert("otp_status 요소가 없습니다."); return; }

  if (!email || email.indexOf("@") < 1) {
    box.className = "err";
    box.textContent = "이메일을 올바르게 입력해 주세요.";
    return;
  }

  box.className = "muted";
  box.textContent = "인증번호를 발송 중입니다...";

  try {
    const res = await fetch("/send_email_otp", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({email: email})
    });

    const text = await res.text();
    let data = null;
    try { data = JSON.parse(text); } catch (e) {}

    // 서버는 200으로 내려주는 게 정상 (ok=false로 실패 표시)
    if (!data) {
      box.className = "err";
      box.textContent = "서버 응답을 해석할 수 없습니다.";
      return;
    }

    if (!data.ok) {
      box.className = "err";
      box.textContent = data.message || "인증번호 발송 실패";
      return;
    }

    box.className = "ok";
    box.textContent = "인증번호를 이메일로 발송했습니다.";
  } catch (e) {
    box.className = "err";
    box.textContent = "네트워크 오류로 인증번호 발송에 실패했습니다.";
  }
}

function toggleAllTerms(el) {
  const checked = !!el.checked;
  document.querySelectorAll(".terms input[type=checkbox]").forEach(cb => {
    if (cb.id !== "t_all") cb.checked = checked;
  });
}

function validateSignup() {
  const email = buildEmail();
  if (!email) {
    alert("이메일을 입력해 주세요.");
    return false;
  }
  const reqs = Array.from(document.querySelectorAll(".t_req"));
  const ok = reqs.every(cb => cb.checked);
  if (!ok) {
    alert("필수 약관에 동의해 주세요.");
    return false;
  }
  return true;
}
"""


@app.get("/static/signup.js")
async def signup_js():
    return Response(content=SIGNUP_JS, media_type="application/javascript; charset=utf-8")


@app.get("/signup")
async def signup_get(request: Request):
    err = request.query_params.get("err", "")
    ok = request.query_params.get("ok", "")

    if ok:
        error_block = f'<div class="ok">{html.escape(ok)}</div>'
        otp_class = "ok"
        otp_msg = html.escape(ok)
    else:
        error_block = f'<div class="err">{html.escape(err)}</div>' if err else ""
        otp_class = "muted"
        otp_msg = ""

    html_out = render_safe(
        SIGNUP_HTML,
        ERROR_BLOCK=error_block,
        OTP_CLASS=otp_class,
        OTP_MSG=otp_msg,
    )
    return HTMLResponse(html_out)


def _gen_otp() -> str:
    import random
    return "".join(str(random.randint(0, 9)) for _ in range(6))

@app.post("/send_email_otp")
async def send_email_otp(request: Request):
    try:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "message": "요청 JSON이 올바르지 않습니다."}, 
                status_code=200
            )

        email = (payload.get("email") or "").strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return JSONResponse(
                {"ok": False, "message": "이메일 형식이 올바르지 않습니다."}, 
                status_code=200
            )

        otp = _gen_otp()
        expires = now_kst() + timedelta(minutes=10)

        # SMTP 환경변수 확인
        SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
        SMTP_USER = os.getenv("SMTP_USER", "").strip()
        SMTP_PASS = os.getenv("SMTP_PASS", "").strip()
        FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER).strip()
        
        SMTP_PORT_RAW = (os.getenv("SMTP_PORT", "587") or "587").strip()
        try:
            SMTP_PORT = int(SMTP_PORT_RAW)
        except ValueError:
            return JSONResponse(
                {"ok": False, "message": "SMTP_PORT가 숫자가 아닙니다."}, 
                status_code=200
            )

        # 환경변수가 설정되지 않은 경우
        if not (SMTP_HOST and SMTP_USER and SMTP_PASS and FROM_EMAIL):
            return JSONResponse(
                {"ok": False, "message": "SMTP 환경변수가 설정되지 않았습니다."}, 
                status_code=200
            )

        # OTP를 DB에 저장
        with db_conn() as con:
            con.execute(
                """
                INSERT INTO email_otps(email, otp, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(email)
                DO UPDATE SET otp=excluded.otp, expires_at=excluded.expires_at
                """,
                (email, otp, expires.isoformat()),
            )
            con.commit()

        # 이메일 발송
        try:
            import smtplib
            from email.mime.text import MIMEText

            msg = MIMEText(
                f"오세요 인증번호는 {otp} 입니다. (10분간 유효)", 
                "plain", 
                "utf-8"
            )
            msg["Subject"] = "[오세요] 이메일 인증번호"
            msg["From"] = FROM_EMAIL
            msg["To"] = email

            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASS)
                s.send_message(msg)

        except Exception as e:
            print(f"[SMTP Error] {str(e)}")  # 로그 출력
            return JSONResponse(
                {"ok": False, "message": "메일 발송에 실패했습니다."}, 
                status_code=200
            )

        return JSONResponse({"ok": True}, status_code=200)

    except Exception as e:
        print(f"[OTP Error] {str(e)}")  # 로그 출력
        return JSONResponse(
            {"ok": False, "message": "서버 내부 오류로 인증번호 발송에 실패했습니다."}, 
            status_code=200
        )

@app.post("/signup")
async def signup_post(
    email: str = Form(...),
    otp: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    name: str = Form(...),
    gender: str = Form(""),
    birth: str = Form(""),
):
    email = (email or "").strip().lower()
    otp = (otp or "").strip()

    if password != password2:
        return RedirectResponse(
            url="/signup?err=" + requests.utils.quote("비밀번호 확인이 일치하지 않습니다."),
            status_code=302
        )

    with db_conn() as con:
        row = con.execute(
            "SELECT otp, expires_at FROM email_otps WHERE email=?",
            (email,),
        ).fetchone()

        if not row:
            return RedirectResponse(
                url="/signup?err=" + requests.utils.quote("이메일 인증을 먼저 진행해 주세요."),
                status_code=302
            )

        db_otp, exp = row

        try:
            if datetime.fromisoformat(exp) < now_kst():
                return RedirectResponse(
                    url="/signup?err=" + requests.utils.quote("인증번호가 만료되었습니다."),
                    status_code=302
                )
        except Exception:
            pass

        if otp != db_otp:
            return RedirectResponse(
                url="/signup?err=" + requests.utils.quote("인증번호가 올바르지 않습니다."),
                status_code=302
            )

        # ✅ 여기로 나와 있어야 한다 (들여쓰기 중요)
        exists = con.execute(
            "SELECT 1 FROM users WHERE email=?",
            (email,),
        ).fetchone()
        if exists:
            return RedirectResponse(
                url="/signup?err=" + requests.utils.quote("이미 가입된 이메일입니다."),
                status_code=302
            )

        uid = uuid.uuid4().hex
        salt = uuid.uuid4().hex[:12]
        ph = pw_hash(password, salt)

        con.execute(
            "INSERT INTO users(id,email,pw_hash,name,gender,birth,created_at) VALUES(?,?,?,?,?,?,?)",
            (uid, email, ph, name.strip(), gender.strip(), birth.strip(), now_kst().isoformat()),
        )
        # 1회용 OTP 정리
        con.execute("DELETE FROM email_otps WHERE email=?", (email,))
        con.commit()

    return RedirectResponse(
        url="/login?err=" + requests.utils.quote("가입이 완료되었습니다. 로그인해 주세요."),
        status_code=302
    )


# =========================================================
# JSON API
# =========================================================
@app.get("/api/events_json")
async def api_events_json(request: Request):
    uid = require_user(request)
    # cleanup_ended_participation(uid)  # 빈번 호출에서 쓰기 발생 → 잠금/네트워크 오류 원인
    events = list_active_events(limit=1500)
    with db_conn() as con:
        ids = [e["id"] for e in events]
        counts, joined = _get_event_counts(con, ids, uid)
    my_joined_id = get_joined_event_id(uid)

    out = []
    for e in events:
        ensure_event_coords(e)
        eid = e["id"]
        cap_label = _event_capacity_label(e.get("capacity"), e.get("is_unlimited"))
        cnt = counts.get(eid, 0)
        is_full = False
        if cap_label != "∞":
            try:
                is_full = cnt >= int(cap_label)
            except Exception:
                is_full = False
        joined_me = bool(joined.get(eid, False))
        can_join = (not is_full) and (my_joined_id is None or my_joined_id == eid)
        out.append(
            {
                "id": eid,
                "title": e.get("title") or "",
                "addr": e.get("addr") or "",
                "lat": (e.get("lat") if e.get("lat") is not None else None),
                "lng": (e.get("lng") if e.get("lng") is not None else None),
                "start": e.get("start") or "",
                "end": e.get("end") or "",
                "start_fmt": fmt_start(e.get("start")),
                "remain": remain_text(e.get("end")),
                "photo": e.get("photo") or "",
                "count": cnt,
                "cap_label": cap_label,
                "joined": joined_me,
                "can_join": can_join,
                "is_full": is_full,
            }
        )
    return JSONResponse({"ok": True, "events": out})


@app.get("/api/my_join")
async def api_my_join(request: Request):
    uid = require_user(request)
    # cleanup_ended_participation(uid)  # 빈번 호출에서 쓰기 발생 → 잠금/네트워크 오류 원인
    eid = get_joined_event_id(uid)
    if not eid:
        return JSONResponse({"ok": True, "joined": False})
    e = get_event_by_id(eid)
    if not e or not is_active_event(e.get("end")):
        return JSONResponse({"ok": True, "joined": False})
    with db_conn() as con:
        cnt = con.execute(
            "SELECT COUNT(*) FROM event_participants WHERE event_id=?", (eid,)
        ).fetchone()[0]
        cap_label = _event_capacity_label(e.get("capacity"), e.get("is_unlimited"))
    return JSONResponse(
        {
            "ok": True,
            "joined": True,
            "event": {
                "id": eid,
                "title": e.get("title") or "",
                "addr": e.get("addr") or "",
                "start_fmt": fmt_start(e.get("start")),
                "remain": remain_text(e.get("end")),
                "photo": e.get("photo") or "",
                "count": int(cnt),
                "cap_label": cap_label,
            },
        }
    )


@app.post("/api/toggle_join")
async def api_toggle_join(request: Request):
    uid = require_user(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    event_id = (payload.get("event_id") or "").strip()
    if not event_id:
        return JSONResponse({"ok": False, "message": "event_id가 필요합니다."})
    ok, msg, joined_now = toggle_join(uid, event_id)
    if not ok:
        return JSONResponse({"ok": False, "message": msg})
    return JSONResponse({"ok": True, "message": msg, "joined": bool(joined_now)})


# -------------------------
# Map page (Kakao)
# -------------------------
@app.get("/map")
async def map_page(request: Request):
    if not KAKAO_JAVASCRIPT_KEY:
        return HTMLResponse("<h3 style='font-family:sans-serif'>KAKAO_JAVASCRIPT_KEY가 설정되지 않았습니다.</h3>")

    MAP_HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>오세요 지도</title>
<style>
  html, body { height:100%; margin:0; }
  #map { width:100%; height:100%; }
  .iw {
    font-family:Pretendard,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    width:260px;
    position:relative;
  }
  .iw .img {
    width:100%;
    height:140px;
    border-radius:14px;
    object-fit:cover;
    background:#f3f4f6;
    border:1px solid #e5e7eb;
  }
  .iw h3 { margin:10px 0 6px; font-size:16px; }
  .iw .meta { color:#6b7280; font-size:12px; line-height:1.4; }
  .iw .row { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-top:10px; }
  .iw .cap { font-size:12px; color:#111827; }
  .iw button {
    border:0; border-radius:999px; padding:8px 12px; cursor:pointer;
    font-size:13px; font-weight:700;
    background:#111; color:#fff;
  }
  .iw button[disabled]{ background:#9ca3af; cursor:not-allowed; }
  .iw .closebtn{position:absolute;top:8px;right:8px;width:26px;height:26px;border-radius:999px;background:rgba(0,0,0,.78);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:900;line-height:1;cursor:pointer;}
  .iw .closebtn:hover{background:rgba(0,0,0,.88);}
</style>
</head>
<body>
<div id="map"></div>

<script src="//dapi.kakao.com/v2/maps/sdk.js?appkey=__APPKEY__"></script>
<script>
  const DEFAULT_CENTER = new kakao.maps.LatLng(36.0190, 129.3435);
  const map = new kakao.maps.Map(document.getElementById('map'), {
    center: DEFAULT_CENTER,
    level: 6
  });

  let markers = new Map();
  let eventsById = new Map();
  let openIw = null;

  function closeIw(){
    if (openIw){ openIw.close(); openIw = null; }
  }


  function esc(s) {
    return (s||"").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
  }

  function renderInfo(e) {
    const photo = e.photo ? `<img class="img" src="data:image/jpeg;base64,${e.photo}" />` : `<div class="img"></div>`;
    const remain = e.remain ? ` · <b>${esc(e.remain)}</b>` : "";
    const cap = `${e.count}/${esc(e.cap_label)}`;
    let btnText = e.joined ? "빠지기" : "참여하기";
    let disabled = (!e.joined && !e.can_join) ? "disabled" : "";
    if (!e.joined && e.is_full) btnText = "정원마감";
    return `
      <div class="iw">
        <div class="closebtn" onclick="closeIw()">×</div>
        ${photo}
        <h3>${esc(e.title)}</h3>
        <div class="meta">⏰ ${esc(e.start_fmt)}${remain}</div>
        <div class="meta">📍 ${esc(e.addr)}</div>
        <div class="row">
          <div class="cap">👥 ${cap}</div>
          <button ${disabled} onclick="toggleJoin('${e.id}')">${btnText}</button>
        </div>
      </div>
    `;
  }

  async function fetchEvents() {
    const r = await fetch('/api/events_json', {credentials:'include'});
    const d = await r.json();
    if (!d.ok) throw new Error(d.message || 'fetch failed');
    return d.events || [];
  }

  function upsertMarker(e) {
    if (!e.lat || !e.lng) return;
    const pos = new kakao.maps.LatLng(e.lat, e.lng);
    if (!markers.has(e.id)) {
      const m = new kakao.maps.Marker({ position: pos });
      m.setMap(map);
      markers.set(e.id, m);
      kakao.maps.event.addListener(m, 'click', () => {
        if (openIw) openIw.close();
        const iw = new kakao.maps.InfoWindow({
          content: renderInfo(e),
          removable: false
        });
        iw.__eid = e.id;
        iw.open(map, m);
        openIw = iw;
      });
    } else {
      markers.get(e.id).setPosition(pos);
    }
  }

  function pruneMarkers(validIds) {
    for (const [eid, m] of markers.entries()) {
      if (!validIds.has(eid)) {
        m.setMap(null);
        markers.delete(eid);
      }
    }
  }

  async function refresh() {
    try {
      const events = await fetchEvents();
      eventsById = new Map(events.map(e => [e.id, e]));
      const valid = new Set(events.map(e => e.id));
      pruneMarkers(valid);
      events.forEach(upsertMarker);

      if (!window.__oseyo_centered && events.length) {
        // 첫 이벤트 좌표로 중심 이동
        const first = events.find(e => e.lat && e.lng);
        if (first) {
          map.setCenter(new kakao.maps.LatLng(first.lat, first.lng));
          window.__oseyo_centered = true;
        }
      }

      if (openIw && openIw.__eid) {
        const eid = openIw.__eid;
        const cur = eventsById.get(eid);
        if (cur) openIw.setContent(renderInfo(cur));
        else { openIw.close(); openIw = null; }
      }

      // NOTE: 부모(Gradio)로 sync 메시지를 보내면 Gradio가 계속 새로고침되어 로딩이 반복됨
      //       그래서 지도는 사용자 액션(참여/빠지기) 때만 갱신한다.
    } catch (e) {
      console.warn(e);
    }
  }

  async function toggleJoin(eid) {
    try {
      const r = await fetch('/api/toggle_join', {
        method:'POST',
        credentials:'include',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({event_id:eid})
      });
      const d = await r.json();
      if (!d.ok) { alert(d.message || '오류'); return; }
      await refresh();
    } catch (e) {
      alert('네트워크 오류');
    }
  }

  refresh();
  // 자동 폴링 제거(정신사납게 로딩 반복 방지)
  // setInterval(refresh, 2500);
</script>
</body>
</html>
"""
    return HTMLResponse(render_safe(MAP_HTML, APPKEY=KAKAO_JAVASCRIPT_KEY))


# =========================================================
# 8) Gradio UI (/app)  ※ 아래는 네 코드 그대로 유지(오류 없는 상태)
# =========================================================
CSS = r"""
:root {
  --bg:#FAF9F6; --ink:#1F2937; --muted:#6B7280; --line:#E5E3DD; --accent:#111;
  --card:#ffffffcc; --danger:#ef4444;
}

html, body, .gradio-container { background: var(--bg) !important; }
.gradio-container { width:100% !important; max-width:1100px !important; margin:0 auto !important; }

a { color: inherit; }

.header { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; margin-top:8px; }
.header h1 { font-size:26px; margin:0; }
.header p { margin:4px 0 0; color:var(--muted); font-size:13px; }

.logout a { text-decoration:none; color: var(--muted); font-size:13px; }

.section-title { font-weight:800; margin: 12px 0 6px; }
.helper { color: var(--muted); font-size:12px; margin: 0 0 10px; }

#fab_btn {
  position: fixed; right: 22px; bottom: 22px; z-index: 9999;
}
#fab_btn, #fab_btn button, button#fab_btn {
  width: 56px !important; height: 56px !important;
  min-width: 56px !important; max-width: 56px !important;
  padding: 0 !important;
  border-radius: 999px !important;
  background: #111 !important; color: #fff !important;
  box-shadow: 0 10px 24px rgba(0,0,0,.22) !important;
  display: flex !important; align-items: center !important; justify-content: center !important;
  font-size: 26px !important; line-height: 1 !important;
}

.overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,.55);
  z-index: 60;
}

.main-modal {
  position: fixed; left:50%; top:50%; transform: translate(-50%,-50%);
  width: min(520px, calc(100vw - 20px));
  height: min(760px, calc(100vh - 20px));
  background: #fff;
  border-radius: 18px;
  border: 1px solid var(--line);
  box-shadow: 0 18px 60px rgba(0,0,0,.25);
  z-index: 70;
  overflow:hidden;
}
.modal-header { padding: 16px 18px; border-bottom: 1px solid var(--line); font-weight:800; text-align:center; }
.modal-body { padding: 14px 16px; overflow-y:auto; }
.modal-footer { padding: 12px 16px; border-top: 1px solid var(--line); display:flex; gap:10px; }
.modal-footer .btn-close button { background:#eee !important; color:#111 !important; border-radius:12px !important; }
.modal-footer .btn-primary button { background:#111 !important; color:#fff !important; border-radius:12px !important; }
.modal-footer .btn-danger button { background: var(--danger) !important; color:#fff !important; border-radius:12px !important; }

.note { color: var(--muted); font-size:12px; line-height:1.4; white-space: normal; }

.fav-grid { display:grid; grid-template-columns: 1fr 1fr; gap:10px; margin-top: 6px; }
.fav-item { display:flex; align-items:stretch; gap:6px; }
.fav-item .fav-main button { width:100% !important; border-radius:12px !important; background:#f3f4f6 !important; color:#111 !important; border:1px solid #e5e7eb !important; }
.fav-item .fav-del button { width:38px !important; min-width:38px !important; padding:0 !important; border-radius:12px !important; background:#fff !important; color:#111 !important; border:1px solid #e5e7eb !important; }
.fav-item .fav-del button:hover { background:#fee2e2 !important; border-color:#fecaca !important; color:#b91c1c !important; }

.event-card { background: rgba(255,255,255,.7); border:1px solid var(--line); border-radius:18px; padding:14px; box-shadow:0 8px 22px rgba(0,0,0,.06); }
.event-img img { width:100% !important; border-radius:16px !important; object-fit:cover !important; height:220px !important; }
@media (min-width: 900px) { .event-img img { height:180px !important; } }

.join-btn button { border-radius:999px !important; background:#111 !important; color:#fff !important; font-weight:800 !important; }
.join-btn button[disabled] { background:#9ca3af !important; }

.joined-box { background: rgba(255,255,255,.8); border:1px solid var(--line); border-radius:18px; padding:14px; box-shadow:0 8px 22px rgba(0,0,0,.06); }
.joined-img img { width:100% !important; border-radius:16px !important; object-fit:cover !important; height:180px !important; }

.map-iframe iframe { width:100%; height: 70vh; min-height:520px; border:0; border-radius:18px; box-shadow:0 8px 22px rgba(0,0,0,.06); }
"""

# ---- 이하 Gradio/이벤트 생성/지도/즐겨찾기 로직은 네 코드 그대로 붙여도 된다.
# (너가 올린 코드가 너무 길어서 여기서부터는 "변경 없음"이 맞다.)
# 이 파일로 그대로 쓰려면: 네가 올린 app.py의 "encode_img_to_b64" 이하 부분을 그대로 이어붙이면 된다.
# ------------------------------------------------------------
# ★ 중요: 위에서 고친 것들(1) SIGNUP_HTML 밖에 있던 <script> 제거
#         (2) send_email_otp 500 방지
#         (3) signup_post exists 들여쓰기 버그 수정
#         (4) /logout 추가
# ------------------------------------------------------------

# =========================================================
# 8) Gradio UI (/app)  ※ 아래는 네 코드 그대로 유지
# =========================================================

# =========================================================
# 8) Gradio UI (/app)
# =========================================================

def encode_img_to_b64(img_np) -> str:
    if img_np is None:
        return ""
    try:
        im = Image.fromarray(img_np.astype("uint8"))
        im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return ""


def decode_photo(photo_b64: str):
    try:
        if not photo_b64:
            return None
        data = base64.b64decode(photo_b64)
        im = Image.open(io.BytesIO(data)).convert("RGB")
        return im
    except Exception:
        return None


def _dt_to_store(v):
    """Gradio DateTime/Textbox 모두 처리해서 DB에 저장할 문자열로."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        dt = v
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        else:
            dt = dt.astimezone(KST)
        return dt.isoformat(timespec="minutes")
    return str(v).strip()


def _combine_date_time(date_v, hh: str, mm: str) -> str:
    """date_v(날짜) + hh/mm(문자열)를 'YYYY-MM-DD HH:MM'로 합친다. 초는 저장하지 않는다."""
    if not date_v:
        return ''
    # date_v can be date/datetime/str
    try:
        import datetime as _dt
        if isinstance(date_v, _dt.datetime):
            d = date_v.date().isoformat()
        elif isinstance(date_v, _dt.date):
            d = date_v.isoformat()
        else:
            d = str(date_v).strip()[:10]
    except Exception:
        d = str(date_v).strip()[:10]
    if not d:
        return ''
    hh = (str(hh).zfill(2))[:2]
    mm = (str(mm).zfill(2))[:2]
    return f"{d} {hh}:{mm}"



def card_md(e: dict):
    title = html.escape((e.get("title") or "").strip())
    addr = html.escape((e.get("addr") or "").strip())
    start = html.escape(fmt_start(e.get("start")))
    rem = remain_text(e.get("end"))
    rem_txt = f" · **{html.escape(rem)}**" if rem and rem != "종료됨" else ""
    cap = f"{e.get('count',0)}/{html.escape(e.get('cap_label','∞'))}"
    title_md = f"### {title}"
    meta_md = f"⏰ {start}{rem_txt}\n\n📍 {addr}\n\n👥 {cap}"
    return title_md, meta_md


def get_joined_view(user_id: str):
    eid = get_joined_event_id(user_id)
    if not eid:
        return False, None, "", ""
    e = get_event_by_id(eid)
    if not e or not is_active_event(e.get("end")):
        return False, None, "", ""
    with db_conn() as con:
        cnt = con.execute("SELECT COUNT(*) FROM event_participants WHERE event_id=?", (eid,)).fetchone()[0]
    cap_label = _event_capacity_label(e.get("capacity"), e.get("is_unlimited"))
    start = fmt_start(e.get("start"))
    rem = remain_text(e.get("end"))
    addr = e.get("addr") or ""
    info = f"**{e.get('title','')}**\n\n⏰ {start} · **{rem}**\n\n📍 {addr}\n\n👥 {cnt}/{cap_label}"
    return True, (decode_photo(e.get("photo")) if e.get("photo") else None), info, eid


# 홈에서 스크롤로 보여줄 카드 개수 (너무 많으면 느려져서 적당히)
MAX_CARDS = 60


def events_for_view(user_id: str, limit: int = MAX_CARDS):
    # 종료된 이벤트 제외 + 최신순
    all_events = list_active_events(limit=1200)
    # 너무 많으면 UI가 무거워서 상한
    chunk = all_events[:limit]

    with db_conn() as con:
        ids = [e["id"] for e in chunk]
        counts, joined = _get_event_counts(con, ids, user_id)

    my_joined_id = get_joined_event_id(user_id)

    for e in chunk:
        eid = e["id"]
        e["count"] = counts.get(eid, 0)
        e["joined"] = bool(joined.get(eid, False))
        cap_label = _event_capacity_label(e.get("capacity"), e.get("is_unlimited"))
        e["cap_label"] = cap_label

        is_full = False
        if cap_label != "∞":
            try:
                is_full = e["count"] >= int(cap_label)
            except Exception:
                is_full = False
        e["is_full"] = is_full
        e["can_join"] = (not is_full) and (my_joined_id is None or my_joined_id == eid)

    return chunk


def _empty_refresh():
    updates = []
    for _ in range(MAX_CARDS):
        updates.extend([
            gr.update(visible=False),
            gr.update(value=None),
            gr.update(value=""),
            gr.update(value=""),
            gr.update(value=""),
            gr.update(value="참여하기", interactive=False),
        ])
    return (
        gr.update(visible=False), gr.update(value=None), gr.update(value=""), gr.update(value=""),
        gr.update(visible=False), gr.update(value=None), gr.update(value=""), gr.update(value=""),
        *updates,
        gr.update(value=""),
    )


def refresh_view(req: gr.Request):
    try:
        uid = require_user(req.request)
    except Exception:
        return _empty_refresh()

    j_vis, j_img, j_info, j_eid = get_joined_view(uid)
    j2_vis, j2_img, j2_info, j2_eid = j_vis, j_img, j_info, j_eid

    events = events_for_view(uid, limit=MAX_CARDS)

    updates = []
    for i in range(MAX_CARDS):
        if i < len(events):
            e = events[i]
            title_md, meta_md = card_md(e)
            btn_label = "빠지기" if e["joined"] else ("정원마감" if (not e["joined"] and e["is_full"]) else "참여하기")
            interactive = True
            if not e["joined"] and (not e["can_join"]):
                interactive = False
            if not e["joined"] and e["is_full"]:
                interactive = False

            img = decode_photo(e.get("photo") or "")
            updates.extend([
                gr.update(visible=True),
                gr.update(value=img),
                gr.update(value=title_md),
                gr.update(value=meta_md),
                gr.update(value=e["id"]),
                gr.update(value=btn_label, interactive=interactive),
            ])
        else:
            updates.extend([
                gr.update(visible=False),
                gr.update(value=None),
                gr.update(value=""),
                gr.update(value=""),
                gr.update(value=""),
                gr.update(value="참여하기", interactive=False),
            ])

    msg = ""

    return (
        gr.update(visible=j_vis),
        gr.update(value=j_img) if j_img else gr.update(value=None),
        gr.update(value=j_info),
        gr.update(value=j_eid or ""),
        gr.update(visible=j2_vis),
        gr.update(value=j2_img) if j2_img else gr.update(value=None),
        gr.update(value=j2_info),
        gr.update(value=j2_eid or ""),
        *updates,
        gr.update(value=msg),
    )


def toggle_join_and_refresh(event_id: str, req: gr.Request):
    uid = require_user(req.request)
    ok, msg, _ = toggle_join(uid, (event_id or "").strip())
    out = list(refresh_view(req))
    out[-1] = gr.update(value=msg)
    return tuple(out)


# --------- 즐겨찾기 UI helpers ---------

def my_events_for_user(user_id: str):
    with db_conn() as con:
        rows = con.execute(
            "SELECT id, title, created_at, capacity, is_unlimited FROM events WHERE user_id=? ORDER BY created_at DESC LIMIT 200",
            (user_id,),
        ).fetchall()
    out = []
    for rid, title, created_at, cap, unlim in rows:
        cap_label = _event_capacity_label(cap, unlim)
        out.append(f"{title}  ·  정원 {cap_label}  ·  {created_at[:16]}  ·  ({rid[:6]})")
    return out


def parse_my_event_choice(choice: str | None):
    if not choice:
        return None
    m = re.search(r"\(([0-9a-f]{6})\)\s*$", choice.strip())
    if not m:
        return None
    prefix = m.group(1)
    with db_conn() as con:
        row = con.execute("SELECT id FROM events WHERE id LIKE ? LIMIT 1", (prefix + "%",)).fetchone()
    return row[0] if row else None


def delete_my_event(choice: str, req: gr.Request):
    uid = require_user(req.request)
    eid = parse_my_event_choice(choice)
    if not eid:
        return gr.update(value="삭제할 이벤트를 선택해 주세요."), gr.update(choices=my_events_for_user(uid), value=None)

    with db_conn() as con:
        row = con.execute("SELECT user_id FROM events WHERE id=?", (eid,)).fetchone()
        if not row or row[0] != uid:
            return gr.update(value="삭제 권한이 없습니다."), gr.update(choices=my_events_for_user(uid), value=None)
        con.execute("DELETE FROM events WHERE id=?", (eid,))
        con.execute("DELETE FROM event_participants WHERE event_id=?", (eid,))
        con.commit()

    return gr.update(value="삭제 완료"), gr.update(choices=my_events_for_user(uid), value=None)


def search_addr(keyword: str):
    docs = kakao_search(keyword, size=8)
    if not docs:
        return gr.update(choices=[]), gr.update(value="검색 결과가 없습니다.")
    choices = [f"{d['name']} | {d['addr']} | ({d['y']:.5f},{d['x']:.5f})" for d in docs]
    return gr.update(choices=choices), gr.update(value=f"{len(choices)}건 검색됨")


def pick_addr(choice: str):
    if not choice:
        return gr.update(value=""), gr.update(value="")
    parts = [p.strip() for p in choice.split("|")]
    if len(parts) < 3:
        return gr.update(value=""), gr.update(value="")
    name, addr, ll = parts[0], parts[1], parts[2]
    m = re.search(r"\(([-0-9.]+),\s*([-0-9.]+)\)", ll)
    if not m:
        return gr.update(value=addr), gr.update(value="")
    lat = float(m.group(1)); lng = float(m.group(2))
    payload = {"addr": addr, "lat": lat, "lng": lng}
    return gr.update(value=addr), gr.update(value=json.dumps(payload, ensure_ascii=False))


def cap_toggle(is_unlimited):
    try:
        is_unlimited_bool = bool(is_unlimited)
    except Exception:
        is_unlimited_bool = False
    return gr.update(interactive=not is_unlimited_bool)


def close_main_modal():
    return gr.update(visible=False), gr.update(visible=False), False


def open_main_modal(req: gr.Request):
    uid = require_user(req.request)
    favs = get_top_favs(10)

    now = now_kst()
    today = now.date().isoformat()
    hh = f"{now.hour:02d}"
    mm = f"{(now.minute // 5) * 5:02d}"

    return (
        gr.update(visible=True),  # overlay
        gr.update(visible=True),  # main_modal
        *fav_updates(favs),
        gr.update(choices=my_events_for_user(uid), value=None),
        gr.update(value=""),  # fav_msg
        gr.update(value=""),  # del_msg
        gr.update(value=None),  # photo_preview
        gr.update(value=""),  # title
        gr.update(value=today),  # start_date
        gr.update(value=hh),     # start_hour
        gr.update(value=mm),     # start_min
        gr.update(value=None),   # end_date
        gr.update(value=hh),     # end_hour
        gr.update(value=mm),     # end_min
        gr.update(value=10, interactive=True),  # cap_slider
        gr.update(value=False),  # cap_unlimited
        gr.update(value=""),    # addr_text
        gr.update(value=""),    # picked_addr
        gr.update(value=""),    # save_msg
        True,
    )



def select_fav(name: str):
    name = (name or "").strip()
    if name.startswith("⭐"):
        name = name.replace("⭐", "").strip()
    bump_fav(name)
    return gr.update(value=name)


def add_fav(new_name: str):
    new_name = (new_name or "").strip()
    if not new_name:
        favs = get_top_favs(10)
        return gr.update(value="이름을 입력해 주세요."), *fav_updates(favs)
    bump_fav(new_name)
    favs = get_top_favs(10)
    return gr.update(value="추가 완료"), *fav_updates(favs)


def delete_fav_click(name: str):
    delete_fav(name)
    favs = get_top_favs(10)
    return gr.update(value="삭제 완료"), *fav_updates(favs)


def fav_updates(favs):
    out = []
    for i in range(10):
        if i < len(favs):
            name = favs[i]['name']
            out.extend([
                gr.update(visible=True),
                gr.update(value=f"⭐ {name}"),
                gr.update(value='−', interactive=True),
                gr.update(value=name),
            ])
        else:
            out.extend([
                gr.update(visible=False),
                gr.update(value=''),
                gr.update(value='−', interactive=False),
                gr.update(value=''),
            ])
    return tuple(out)



# ---- 사진 업로드 모달 ----

def open_img_modal():
    return gr.update(visible=True)


def close_img_modal():
    return gr.update(visible=False)


def confirm_img(img_np):
    return gr.update(visible=False), gr.update(value=img_np)


# ---- 장소 검색(서브 모달) ----

def open_place_modal():
    return gr.update(visible=True), gr.update(visible=True), gr.update(value=""), gr.update(choices=[]), gr.update(value="")


def close_place_modal():
    return gr.update(visible=False), gr.update(visible=False)


def confirm_place(addr_preview: str, picked_json: str):
    return gr.update(value=addr_preview), gr.update(value=picked_json), gr.update(visible=False), gr.update(visible=False)


def save_event(
    title: str,
    img_np,
    start_date,
    start_hour: str,
    start_min: str,
    end_date,
    end_hour: str,
    end_min: str,
    addr_text: str,
    picked_addr,
    capacity,
    unlimited,
    req: gr.Request,
):
    uid = require_user(req.request)
    title = (title or "").strip()
    if not title:
        return gr.update(value="이벤트명을 입력해 주세요."), gr.update(visible=True), gr.update(visible=True), True

    addr = (addr_text or "").strip()

    # picked_addr는 JSON 문자열
    lat = None
    lng = None
    picked = None
    if picked_addr:
        try:
            picked = json.loads(str(picked_addr))
        except Exception:
            picked = None

    if picked and isinstance(picked, dict):
        addr = picked.get("addr") or addr
        lat = picked.get("lat")
        lng = picked.get("lng")

    if not addr:
        return gr.update(value="장소를 선택해 주세요."), gr.update(visible=True), gr.update(visible=True), True

    # 좌표가 없으면 geocode로 보강
    if (lat is None or lng is None) and addr:
        got = kakao_geocode(addr)
        if got:
            lat, lng = got

    start_s = _combine_date_time(start_date, start_hour, start_min)
    end_s = _combine_date_time(end_date, end_hour, end_min)

    sdt = parse_dt(start_s)
    edt = parse_dt(end_s)
    if sdt and edt and edt <= sdt:
        return gr.update(value="종료일시는 시작일시 이후여야 합니다."), gr.update(visible=True), gr.update(visible=True), True

    photo_b64 = encode_img_to_b64(img_np)

    # 정원
    is_unlim = 1 if bool(unlimited) else 0
    if is_unlim:
        cap_val = 0
    else:
        try:
            cap_val = int(float(capacity))
            cap_val = max(1, min(99, cap_val))
        except Exception:
            cap_val = 10

    eid = uuid.uuid4().hex
    with db_conn() as con:
        con.execute(
            """
            INSERT INTO events(id,title,photo,start,end,addr,lat,lng,created_at,user_id,capacity,is_unlimited)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (eid, title, photo_b64, start_s, end_s, addr, lat, lng, now_kst().isoformat(), uid, cap_val, is_unlim),
        )
        con.commit()

    bump_fav(title)
    return gr.update(value="등록 완료"), gr.update(visible=False), gr.update(visible=False), False


# =========================================================
# Gradio UI 정의
# =========================================================

CSS = r"""
:root {
  --bg:#FAF9F6; --ink:#1F2937; --muted:#6B7280; --line:#E5E3DD; --accent:#111;
  --card:#ffffffcc; --danger:#ef4444;
}

html, body, .gradio-container { background: var(--bg) !important; }
.gradio-container { width:100% !important; max-width:1100px !important; margin:0 auto !important; }

a { color: inherit; }

.header { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; margin-top:8px; }
.header h1 { font-size:26px; margin:0; }
.header p { margin:4px 0 0; color:var(--muted); font-size:13px; }

.logout a { text-decoration:none; color: var(--muted); font-size:13px; }

.section-title { font-weight:800; margin: 12px 0 6px; }
.helper { color: var(--muted); font-size:12px; margin: 0 0 10px; }

/* Floating + */
.fab-wrap { position:fixed; right:22px; bottom:22px; z-index:50; }
#fab_btn { width:56px !important; height:56px !important; min-width:56px !important; border-radius:999px !important; padding:0 !important; }
#fab_btn button, #fab_btn .gr-button { width:56px !important; height:56px !important; border-radius:999px !important; background:#111 !important; color:#fff !important; font-size:26px !important; box-shadow: 0 10px 24px rgba(0,0,0,.22) !important; }

.overlay { position: fixed; inset: 0; background: rgba(0,0,0,.55); z-index: 60; }
.overlay2 { z-index: 80; }

.main-modal { position: fixed; left:50%; top:50%; transform: translate(-50%,-50%);
  width: min(520px, calc(100vw - 20px));
  height: min(760px, calc(100vh - 20px));
  background: #fff; border-radius: 18px; border: 1px solid var(--line);
  box-shadow: 0 18px 60px rgba(0,0,0,.25);
  z-index: 70; overflow:hidden;
}

.sub-modal { position: fixed; left:50%; top:50%; transform: translate(-50%,-50%);
  width: min(520px, calc(100vw - 20px));
  height: min(560px, calc(100vh - 20px));
  background: #fff; border-radius: 18px; border: 1px solid var(--line);
  box-shadow: 0 18px 60px rgba(0,0,0,.25);
  z-index: 90; overflow:hidden;
}

.modal-header { padding: 16px 18px; border-bottom: 1px solid var(--line); font-weight:800; text-align:center; }
.modal-body { padding: 14px 16px; overflow-y:auto; height: calc(100% - 110px); }
.modal-footer { padding: 12px 16px; border-top: 1px solid var(--line); display:flex; gap:10px; }
.modal-footer .btn-close button { background:#eee !important; color:#111 !important; border-radius:12px !important; }
.modal-footer .btn-primary button { background:#111 !important; color:#fff !important; border-radius:12px !important; }
.modal-footer .btn-danger button { background: var(--danger) !important; color:#fff !important; border-radius:12px !important; }

.note { color: var(--muted); font-size:12px; line-height:1.4; white-space: normal; }

/* Favorites: delete button as small circle on top-right */
.fav-grid { display:grid; grid-template-columns: 1fr 1fr; gap:10px; margin-top: 6px; }
.fav-box { position:relative; }
.fav-box .fav-main button { width:100% !important; border-radius:12px !important; background:#f3f4f6 !important; color:#111 !important; border:1px solid #e5e7eb !important; padding-right:36px !important; }
.fav-box .fav-del { position:absolute; top:6px; right:6px; }
.fav-box .fav-del button {
  width:22px !important; height:22px !important; min-width:22px !important; padding:0 !important;
  border-radius:999px !important; background:#fff !important; border:1px solid #e5e7eb !important;
  color:#b91c1c !important; font-weight:900 !important; line-height:1 !important;
}
.fav-box .fav-del button:hover { background:#fee2e2 !important; border-color:#fecaca !important; }


/* Events list (page scrolls) */
.events-scroll { padding-right: 6px; }

.event-card { background: rgba(255,255,255,.7); border:1px solid var(--line); border-radius:18px; padding:14px; box-shadow:0 8px 22px rgba(0,0,0,.06); margin-bottom:12px; }
.event-img img { width:100% !important; border-radius:16px !important; object-fit:cover !important; height:220px !important; }
@media (min-width: 900px) { .event-img img { height:180px !important; } }

.join-btn button { border-radius:999px !important; background:#111 !important; color:#fff !important; font-weight:800 !important; }
.join-btn button[disabled] { background:#9ca3af !important; }

.joined-box { background: rgba(255,255,255,.8); border:1px solid var(--line); border-radius:18px; padding:14px; box-shadow:0 8px 22px rgba(0,0,0,.06); }
.joined-img img { width:100% !important; border-radius:16px !important; object-fit:cover !important; height:180px !important; }

.map-iframe iframe { width:100%; height: 70vh; min-height:520px; border:0; border-radius:18px; box-shadow:0 8px 22px rgba(0,0,0,.06); }
"""

# Date/Time controls (no seconds)
DateComp = getattr(gr, 'Date', None)
HOUR_CHOICES = [f"{i:02d}" for i in range(24)]
MIN_CHOICES = [f"{i:02d}" for i in range(0, 60, 5)]


with gr.Blocks(css=CSS, title='오세요') as demo:
    with gr.Row(elem_classes=['header']):
        with gr.Column(scale=8):
            gr.Markdown('## 지금, 열려 있습니다')
            gr.Markdown("<span style='color:#6b7280;font-size:13px'>편하면 오셔도 됩니다</span>")
        with gr.Column(scale=2, elem_classes=['logout']):
            gr.HTML("<div style='text-align:right'><a href='/logout'>로그아웃</a></div>")

    tabs = gr.Tabs()

    with tabs:
        with gr.Tab('탐색'):
            gr.Markdown('### 열려 있는 활동', elem_classes=['section-title'])
            gr.Markdown('참여하기는 1개 활동만 가능하다. 다른 활동에 참여하려면 먼저 빠지기를 해야 한다.', elem_classes=['helper'])

            joined_wrap = gr.Column(visible=False, elem_classes=['joined-box'])
            with joined_wrap:
                joined_img = gr.Image(visible=True, interactive=False, elem_classes=['joined-img'])
                joined_info = gr.Markdown()
                joined_eid = gr.Textbox(visible=False)
                joined_leave = gr.Button('빠지기', variant='stop', elem_classes=['join-btn'])

            gr.Markdown('### 전체 활동', elem_classes=['section-title'])

            cards_wrap = gr.Column(elem_classes=['events-scroll'])
            cards = []; card_imgs=[]; card_titles=[]; card_metas=[]; card_ids=[]; card_btns=[]
            with cards_wrap:
                for i in range(MAX_CARDS):
                    box = gr.Column(visible=False, elem_classes=['event-card'])
                    with box:
                        img = gr.Image(interactive=False, elem_classes=['event-img'])
                        title_md = gr.Markdown()
                        meta_md = gr.Markdown()
                        hid = gr.Textbox(visible=False)
                        btn = gr.Button('참여하기', elem_classes=['join-btn'])
                    cards.append(box); card_imgs.append(img); card_titles.append(title_md); card_metas.append(meta_md); card_ids.append(hid); card_btns.append(btn)

            msg_box = gr.Markdown()

        with gr.Tab('지도'):
            joined_wrap2 = gr.Column(visible=False, elem_classes=['joined-box'])
            with joined_wrap2:
                joined_img2 = gr.Image(visible=True, interactive=False, elem_classes=['joined-img'])
                joined_info2 = gr.Markdown()
                joined_eid2 = gr.Textbox(visible=False)
                joined_leave2 = gr.Button('빠지기', variant='stop', elem_classes=['join-btn'])

            map_html = gr.HTML("<div class='map-iframe'><iframe src='/map' loading='lazy'></iframe></div>", elem_classes=['map-iframe'])

    # Floating action button
    with gr.Row(elem_classes=['fab-wrap']):
        fab = gr.Button('+', elem_id='fab_btn')

    overlay = gr.HTML("<div class='overlay'></div>", visible=False, elem_classes=['overlay'])
    overlay2 = gr.HTML("<div class='overlay overlay2'></div>", visible=False, elem_classes=['overlay','overlay2'])

    main_open = gr.State(False)

    # --- Main create modal ---
    main_modal = gr.Column(visible=False, elem_classes=['main-modal'], elem_id='main_modal')
    with main_modal:
        gr.HTML("<div class='modal-header'>새 이벤트 만들기</div>", elem_classes=['modal-header'])
        with gr.Column(elem_classes=['modal-body']):
            with gr.Tabs():
                with gr.Tab('작성하기'):
                    gr.Markdown('#### ⭐ 자주하는 활동')
                    gr.HTML("<div class='note'>버튼을 누르면 이벤트명에 바로 입력됩니다.</div>")

                    fav_boxes=[]; fav_select_btns=[]; fav_del_btns=[]; fav_hidden_names=[]
                    with gr.Column(elem_classes=['fav-grid']):
                        for i in range(10):
                            box = gr.Column(visible=False, elem_classes=['fav-box'])
                            with box:
                                b_main = gr.Button('', elem_classes=['fav-main'])
                                b_del = gr.Button('−', elem_classes=['fav-del'])
                                h_name = gr.Textbox(visible=False)
                            fav_boxes.append(box)
                            fav_select_btns.append(b_main)
                            fav_del_btns.append(b_del)
                            fav_hidden_names.append(h_name)

                    with gr.Row():
                        new_fav = gr.Textbox(placeholder='즐겨찾기 추가', scale=2)
                        fav_add_btn = gr.Button('추가', scale=1)
                    fav_msg = gr.Markdown()

                    title = gr.Textbox(label='이벤트명', placeholder='예: 30분 산책, 조용히 책 읽기')

                    photo_preview = gr.Image(label='사진(미리보기)', interactive=False, height=160)
                    with gr.Row():
                        photo_add_btn = gr.Button('사진 업로드', variant='secondary')
                        photo_clear_btn = gr.Button('사진 제거', variant='secondary')                    # 날짜/시간 선택 (초 없음)
                    with gr.Row():
                        start_date = (DateComp(label='시작 날짜') if DateComp is not None else gr.Textbox(label='시작 날짜', placeholder='YYYY-MM-DD'))
                        start_hour = gr.Dropdown(choices=HOUR_CHOICES, value='18', label='시', scale=1)
                        start_min = gr.Dropdown(choices=MIN_CHOICES, value='00', label='분', scale=1)
                    with gr.Row():
                        end_date = (DateComp(label='종료 날짜(선택)') if DateComp is not None else gr.Textbox(label='종료 날짜(선택)', placeholder='YYYY-MM-DD'))
                        end_hour = gr.Dropdown(choices=HOUR_CHOICES, value='18', label='시', scale=1)
                        end_min = gr.Dropdown(choices=MIN_CHOICES, value='00', label='분', scale=1)

                    with gr.Row():
                        cap_slider = gr.Slider(1, 99, value=10, step=1, label='정원(1~99)')
                        cap_unlimited = gr.Checkbox(label='제한없음', value=False)

                    gr.Markdown('#### 장소')
                    with gr.Row():
                        addr_text = gr.Textbox(label='선택된 장소', placeholder='장소 검색으로 선택해 주세요.', scale=3)
                        place_open_btn = gr.Button('장소 검색', variant='secondary', scale=1)

                    picked_addr = gr.State('')
                    save_msg = gr.Markdown()

                with gr.Tab('내 글 관리'):
                    my_list = gr.Radio(label='내가 만든 이벤트', choices=[], value=None)
                    del_btn = gr.Button('삭제', variant='stop')
                    del_msg = gr.Markdown()

        with gr.Row(elem_classes=['modal-footer']):
            close_btn = gr.Button('닫기', elem_classes=['btn-close'])
            create_btn = gr.Button('등록하기', elem_classes=['btn-primary'])

    # --- Photo modal ---
    img_modal = gr.Column(visible=False, elem_classes=['sub-modal'], elem_id='img_modal')
    with img_modal:
        gr.HTML("<div class='modal-header'>사진 업로드</div>")
        with gr.Column(elem_classes=['modal-body']):
            img_uploader = gr.Image(label='이미지 선택', type='numpy')
        with gr.Row(elem_classes=['modal-footer']):
            img_cancel = gr.Button('닫기', elem_classes=['btn-close'])
            img_confirm = gr.Button('확인', elem_classes=['btn-primary'])

    # --- Place search sub-modal ---
    place_modal = gr.Column(visible=False, elem_classes=['sub-modal'], elem_id='place_modal')
    with place_modal:
        gr.HTML("<div class='modal-header'>장소 검색</div>")
        with gr.Column(elem_classes=['modal-body']):
            addr_kw = gr.Textbox(label='검색어', placeholder='예: 영일대, 카페, 도서관')
            addr_search_btn = gr.Button('검색')
            addr_choices = gr.Dropdown(label='검색 결과', choices=[])
            addr_status = gr.Markdown()
            addr_preview = gr.Textbox(label='선택된 장소', interactive=False)
            picked_tmp = gr.State('')
        with gr.Row(elem_classes=['modal-footer']):
            place_cancel = gr.Button('닫기', elem_classes=['btn-close'])
            place_confirm = gr.Button('선택', elem_classes=['btn-primary'])

    # --- 초기 상태 강제 ---
    def _reset_on_load():
        return (
            gr.update(visible=False), gr.update(visible=False),
            gr.update(visible=False), gr.update(visible=False),
            gr.update(visible=False), gr.update(visible=False),
            False,
        )

    demo.load(
        fn=_reset_on_load,
        inputs=None,
        outputs=[overlay, main_modal, overlay2, place_modal, overlay2, img_modal, main_open],
    )

    # --- 이벤트 뷰 로드 ---
    demo.load(
        fn=refresh_view,
        inputs=None,
        outputs=[
            joined_wrap, joined_img, joined_info, joined_eid,
            joined_wrap2, joined_img2, joined_info2, joined_eid2,
            *sum([[cards[i], card_imgs[i], card_titles[i], card_metas[i], card_ids[i], card_btns[i]] for i in range(MAX_CARDS)], []),
            msg_box,
        ],
    )

    # --- 카드/참여 버튼 ---
    for i in range(MAX_CARDS):
        card_btns[i].click(
            fn=toggle_join_and_refresh,
            inputs=[card_ids[i]],
            outputs=[
                joined_wrap, joined_img, joined_info, joined_eid,
                joined_wrap2, joined_img2, joined_info2, joined_eid2,
                *sum([[cards[j], card_imgs[j], card_titles[j], card_metas[j], card_ids[j], card_btns[j]] for j in range(MAX_CARDS)], []),
                msg_box,
            ],
        )

    joined_leave.click(
        fn=toggle_join_and_refresh,
        inputs=[joined_eid],
        outputs=[
            joined_wrap, joined_img, joined_info, joined_eid,
            joined_wrap2, joined_img2, joined_info2, joined_eid2,
            *sum([[cards[j], card_imgs[j], card_titles[j], card_metas[j], card_ids[j], card_btns[j]] for j in range(MAX_CARDS)], []),
            msg_box,
        ],
    )

    joined_leave2.click(
        fn=toggle_join_and_refresh,
        inputs=[joined_eid2],
        outputs=[
            joined_wrap, joined_img, joined_info, joined_eid,
            joined_wrap2, joined_img2, joined_info2, joined_eid2,
            *sum([[cards[j], card_imgs[j], card_titles[j], card_metas[j], card_ids[j], card_btns[j]] for j in range(MAX_CARDS)], []),
            msg_box,
        ],
    )

    # --- 메인 모달 열기/닫기 ---
    fab.click(
        fn=open_main_modal,
        inputs=None,
        outputs=[
            overlay, main_modal,
            *sum([[fav_boxes[i], fav_select_btns[i], fav_del_btns[i], fav_hidden_names[i]] for i in range(10)], []),
            my_list,
            fav_msg, del_msg,
            photo_preview,
            title,
            start_date, start_hour, start_min,
            end_date, end_hour, end_min,
            cap_slider, cap_unlimited,
            addr_text, picked_addr,
            save_msg,
            main_open,
        ],
    )

    close_btn.click(fn=close_main_modal, inputs=None, outputs=[overlay, main_modal, main_open])

    # 즐겨찾기 선택/삭제
    for i in range(10):
        fav_select_btns[i].click(fn=select_fav, inputs=[fav_hidden_names[i]], outputs=[title])
        fav_del_btns[i].click(
            fn=delete_fav_click,
            inputs=[fav_hidden_names[i]],
            outputs=[fav_msg, *sum([[fav_boxes[j], fav_select_btns[j], fav_del_btns[j], fav_hidden_names[j]] for j in range(10)], [])],
        )

    fav_add_btn.click(
        fn=add_fav,
        inputs=[new_fav],
        outputs=[fav_msg, *sum([[fav_boxes[j], fav_select_btns[j], fav_del_btns[j], fav_hidden_names[j]] for j in range(10)], [])],
    )

    cap_unlimited.change(fn=cap_toggle, inputs=[cap_unlimited], outputs=[cap_slider])

    # 사진 업로드
    photo_add_btn.click(fn=lambda: (gr.update(visible=True), gr.update(visible=True)), inputs=None, outputs=[overlay2, img_modal])
    img_cancel.click(fn=lambda: (gr.update(visible=False), gr.update(visible=False)), inputs=None, outputs=[overlay2, img_modal])
    img_confirm.click(fn=confirm_img, inputs=[img_uploader], outputs=[img_modal, photo_preview]).then(
        fn=lambda: gr.update(visible=False), inputs=None, outputs=[overlay2]
    )
    photo_clear_btn.click(fn=lambda: None, inputs=None, outputs=[photo_preview])

    # 장소 검색 서브모달
    place_open_btn.click(
        fn=open_place_modal,
        inputs=None,
        outputs=[overlay2, place_modal, addr_kw, addr_choices, addr_status],
    )
    place_cancel.click(fn=close_place_modal, inputs=None, outputs=[overlay2, place_modal])

    addr_search_btn.click(fn=search_addr, inputs=[addr_kw], outputs=[addr_choices, addr_status])
    addr_choices.change(fn=pick_addr, inputs=[addr_choices], outputs=[addr_preview, picked_tmp])

    place_confirm.click(
        fn=confirm_place,
        inputs=[addr_preview, picked_tmp],
        outputs=[addr_text, picked_addr, overlay2, place_modal],
    )

    # 내 글 삭제
    del_btn.click(fn=delete_my_event, inputs=[my_list], outputs=[del_msg, my_list])

    # 등록
    create_btn.click(
        fn=save_event,
        inputs=[title, photo_preview, start_date, start_hour, start_min, end_date, end_hour, end_min, addr_text, picked_addr, cap_slider, cap_unlimited],
        outputs=[save_msg, overlay, main_modal, main_open],
    ).then(
        fn=refresh_view,
        inputs=None,
        outputs=[
            joined_wrap, joined_img, joined_info, joined_eid,
            joined_wrap2, joined_img2, joined_info2, joined_eid2,
            *sum([[cards[i], card_imgs[i], card_titles[i], card_metas[i], card_ids[i], card_btns[i]] for i in range(MAX_CARDS)], []),
            msg_box,
        ],
    )

app = gr.mount_gradio_app(app, demo, path='/app', show_api=False)

if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=int(os.getenv('PORT','8000')))
