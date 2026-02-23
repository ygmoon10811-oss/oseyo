# server.py
from fastapi import HTMLResponse
from fastapi.staticfiles import StaticFiles
import gradio as gr
# app.py에서 정의한 app(FastAPI 객체)과 demo(Gradio 객체)를 가져옵니다.
from app import app, demo

# 1) 정적 파일 서빙 (manifest, sw.js 등이 있는 폴더)
#    /static 경로로 들어오는 요청을 static 폴더와 연결
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except:
    pass # 이미 마운트되어 있을 경우 에러 방지

# 2) PWA “껍데기” (루트 / 접속 시)
#    기존 app.py의 @app.get("/")를 덮어쓰게 됩니다.
@app.get("/", response_class=HTMLResponse)
def pwa_shell():
    return """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <meta name="theme-color" content="#111111" />
  <link rel="manifest" href="/static/manifest.webmanifest" />
  <link rel="apple-touch-icon" href="/static/icons/icon-192.png" />
  <title>오세요</title>
  <style>
    html,body{height:100%;margin:0;background:#FAF9F6;overflow:hidden;}
    iframe{border:0;width:100%;height:100%;}
  </style>
</head>
<body>
  <iframe src="/app" title="오세요"></iframe>
  <script>
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/static/sw.js");
    }
  </script>
</body>
</html>
"""

# 3) Gradio 앱 마운트 (마지막에 한 번만 실행)
#    이미 app.py 끝에서 실행되고 있을 수 있지만, server.py가 최종 실행 파일이므로 명시
app = gr.mount_gradio_app(app, demo, path="/app")
