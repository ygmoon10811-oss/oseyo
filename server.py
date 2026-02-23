# server.py
from fastapi import FastAPI
from fastapi.responses import HTMLResponse  # ✅ 수정됨 (responses에서 가져와야 함)
from fastapi.staticfiles import StaticFiles
import gradio as gr
# app.py에서 정의한 app과 demo를 가져옵니다.
from app import app, demo

# 1) 정적 파일 서빙 (manifest.webmanifest, sw.js 등이 있는 폴더)
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except:
    pass

# 2) PWA “껍데기” (루트 / 접속 시)
@app.get("/", response_class=HTMLResponse)
async def pwa_shell():
    return """
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no" />
  <meta name="theme-color" content="#111111" />
  <link rel="manifest" href="/static/manifest.webmanifest" />
  <link rel="apple-touch-icon" href="/static/icons/icon-192.png" />
  <title>오세요</title>
  <style>
    html,body{height:100%;margin:0;background:#FAF9F6;overflow:hidden;}
    iframe{border:0;width:100%;height:100%;vertical-align:bottom;}
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

# 3) Gradio 앱 마운트
app = gr.mount_gradio_app(app, demo, path="/app")
