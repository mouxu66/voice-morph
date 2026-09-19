"""验证 Qwen3-TTS worker：直接拉起服务 -> /health -> /tts 生成袋鼠音。"""

import json
import os
import subprocess
import time
import urllib.request

# 全部相对于本文件推导项目根，去掉机器专属硬编码（与 config.py 一致，便于换机）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # m2_server 的上一级 = 项目根
VENV312 = os.path.join(_ROOT, "tts_trial", "venv312", "Scripts", "python.exe")
WORKER = os.path.join(_ROOT, "m2_server", "qwen3_tts_service.py")
OUT = os.path.join(_ROOT, "tts_trial", "verify_kangaroo.wav")

p = subprocess.Popen([VENV312, WORKER], cwd=os.path.dirname(WORKER))
ready = False
for _ in range(150):
    time.sleep(2)
    try:
        urllib.request.urlopen("http://127.0.0.1:8001/health", timeout=2)
        print("[ok] worker ready")
        ready = True
        break
    except Exception:
        if p.poll() is not None:
            print("[fail] worker exited early")
            break
if not ready:
    p.terminate()
    raise SystemExit(1)

data = json.dumps(
    {"text": "大家好，我是袋鼠，现在变声工坊的语音已经换成我啦。", "text_language": "zh"}
).encode()
req = urllib.request.Request(
    "http://127.0.0.1:8001/tts", data=data, headers={"Content-Type": "application/json"}
)
wav = urllib.request.urlopen(req, timeout=180).read()
with open(OUT, "wb") as f:
    f.write(wav)
print(f"[ok] wav bytes = {len(wav)} -> {OUT}")
p.terminate()
