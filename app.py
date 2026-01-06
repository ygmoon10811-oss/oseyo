# -*- coding: utf-8 -*-
import os, uuid, base64, io, sqlite3, json, traceback
from datetime import datetime, timedelta
import requests
from PIL import Image
import gradio as gr
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

# --- [디버깅 유틸리티] ---
def log(msg):
    """터미널에 잘 보이게 출력"""
    print(f"\n[DEBUG] {datetime.now().strftime('%H:%M:%S')} 👉 {msg}")

def log_error():
    """에러 상세 내용을 터미널에 출력"""
    print("❌ [ERROR OCCURRED] -------------------------")
    print(traceback.format_exc())
    print("----------------------------------------------")

# --- [1. 환경 설정] ---
log("프로그램 시작 초기화 중...")

BASE_DIR = os.getcwd()
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "oseyo_debug.db")

log(f"DB 경로: {DB_PATH}")

def db_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

# DB 초기화
try:
    with db_conn() as con:
        con.execute("CREATE TABLE IF NOT EXISTS spaces (id TEXT PRIMARY KEY, title TEXT, photo_b64 TEXT, start_iso TEXT, end_iso TEXT, address TEXT, lat REAL, lng REAL, created_at TEXT);")
        con.execute("CREATE TABLE IF NOT EXISTS favorites (activity TEXT PRIMARY KEY, created_at TEXT);")
        con.commit()
    log("DB 테이블 체크 완료")
except:
    log_error()

# API 키 확인
KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "")
if not KAKAO_REST_API_KEY:
    log("⚠️ 경고: KAKAO_REST_API_KEY가 없습니다. 검색 기능이 제한됩니다.")

# --- [2. 로직 함수들 (로그 포함)] ---

def search_kakao(q):
    log(f"검색 요청 들어옴: '{q}'")
    if not KAKAO_REST_API_KEY:
        log("API 키 없음 - 검색 중단")
        return [], gr.update(choices=["API 키가 없습니다."])
    
    try:
        url = "https://dapi.kakao.com/v2/local/search/keyword.json"
        headers = {"Authorization": f"KakaoAK {KAKAO_REST_API_KEY}"}
        res = requests.get(url, headers=headers, params={"query": q, "size": 5})
        log(f"카카오 API 응답 코드: {res.status_code}")
        
        if res.status_code != 200:
            log(f"응답 내용: {res.text}")
            return [], gr.update(choices=[f"API 오류: {res.status_code}"])
        
        data = res.json()
        docs = data.get("documents", [])
        log(f"검색된 장소 개수: {len(docs)}")
        
        cands = [{"label": f"{d['place_name']} ({d['address_name']})", "name": d['place_name'], "y": d['y'], "x": d['x']} for d in docs]
        return cands, gr.update(choices=[x['label'] for x in cands])
    except:
        log_error()
        return [], gr.update(choices=["서버 내부 오류 발생"])

def save_data(act, st, en, img, addr_obj):
    log(f"저장 시도: 활동명='{act}', 장소객체={addr_obj}")
    
    if not act: 
        log("저장 실패: 활동명 누락")
        return "⚠️ 활동명을 입력해주세요."
    
    # 이미지 처리
    pic_str = ""
    if img is not None:
        log("이미지 변환 시작")
        try:
            im = Image.fromarray(img)
            log(f"이미지 모드: {im.mode}")
            if im.mode == 'RGBA':
                im = im.convert('RGB')
            buf = io.BytesIO()
            im.save(buf, format='JPEG')
            pic_str = base64.b64encode(buf.getvalue()).decode()
            log("이미지 변환 성공")
        except:
            log_error()
            return "❌ 이미지 처리 중 오류 발생 (로그 확인)"

    # DB 저장
    try:
        addr_name = addr_obj.get('name', '장소 미지정') if addr_obj else '장소 미지정'
        lat = addr_obj.get('y', 37.5665) if addr_obj else 37.5665
        lng = addr_obj.get('x', 126.9780) if addr_obj else 126.9780
        
        with db_conn() as con:
            con.execute("INSERT INTO spaces VALUES (?,?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex[:8], act, pic_str, st, en, addr_name, lat, lng, datetime.now().isoformat()))
            con.execute("INSERT OR IGNORE INTO favorites VALUES (?,?)", (act, datetime.now().isoformat()))
            con.commit()
        log("✅ DB INSERT 성공")
        return "✅ 저장 완료!"
    except:
        log_error()
        return "❌ DB 저장 중 오류 발생 (로그 확인)"

# --- [3. UI 구성] ---
CSS = ".modal { position: fixed; top: 5%; left: 5%; width: 90%; height: 90%; background: white; z-index: 9999; border: 2px solid red; overflow: auto; }"

with gr.Blocks(css=CSS) as demo:
    state_search = gr.State([])
    state_addr = gr.State({})

    gr.Markdown("## 🐞 디버깅 모드 실행 중")
    gr.Markdown("터미널(검은 화면)을 확인하면서 버튼을 눌러보세요.")
    
    with gr.Row():
        btn_open = gr.Button("1. 모달 열기")
        btn_test_db = gr.Button("DB 연결 테스트")

    # DB 테스트용 출력창
    debug_out = gr.Textbox(label="시스템 로그", lines=2)

    # 모달 영역
    with gr.Group(visible=False) as modal:
        gr.Markdown("### 새 모임 입력")
        t_act = gr.Textbox(label="활동명")
        t_img = gr.Image(label="사진", type="numpy", height=100)
        
        t_search = gr.Textbox(label="장소 검색어")
        b_search = gr.Button("검색")
        r_result = gr.Radio(label="결과")
        
        b_save = gr.Button("저장하기", variant="primary")
        b_close = gr.Button("닫기")

    # --- 이벤트 연결 ---
    btn_open.click(lambda: (log("모달 열기 클릭"), gr.update(visible=True)), None, [modal])
    b_close.click(lambda: (log("모달 닫기 클릭"), gr.update(visible=False)), None, [modal])
    
    def test_db_func():
        try:
            with db_conn() as con:
                cnt = con.execute("SELECT count(*) FROM spaces").fetchone()[0]
            return f"DB 정상 연결됨. 현재 데이터 개수: {cnt}"
        except:
            log_error()
            return "DB 연결 실패! 로그를 확인하세요."

    btn_test_db.click(test_db_func, None, debug_out)

    b_search.click(search_kakao, t_search, [state_search, r_result])
    
    def on_select(val, cands):
        log(f"장소 선택됨: {val}")
        sel = next((x for x in cands if x['label'] == val), {})
        return sel
        
    r_result.select(on_select, [r_result, state_search], state_addr)

    b_save.click(
        save_data, 
        [t_act, gr.Textbox(value="2024-01-01", visible=False), gr.Textbox(value="2024-01-01", visible=False), t_img, state_addr], 
        debug_out
    )

# --- [4. 실행] ---
app = FastAPI()
app = gr.mount_gradio_app(app, demo, path="/")

if __name__ == "__main__":
    log("🚀 서버 시작 중... http://localhost:8000 접속하세요.")
    uvicorn.run(app, host="0.0.0.0", port=8000)
