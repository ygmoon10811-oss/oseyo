from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import gradio as gr

from app import demo  # app.py에서 만든 gradio Blocks 객체

app = FastAPI()

# 1) 정적파일 서빙: /static 아래로 manifest, sw, icons 제공
app.mount("/static", StaticFiles(directory="static"), name="static")

# 2) PWA “껍데기” (루트 /)
#    여기서 manifest + service worker 등록하고, 실제 앱은 /app을 iframe으로 띄움
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
    html,body{height:100%;margin:0;background:#FAF9F6;}
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

# 3) Gradio 앱은 /app으로 마운트
app = gr.mount_gradio_app(app, demo, path="/app")
