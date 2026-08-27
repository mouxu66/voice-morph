# -*- coding: utf-8 -*-
"""直接验证 /api/tts（声纹模式）。每步立刻写盘。"""
import json
import os
import time
import urllib.request

RESULT = r"D:\变声\tts_trial\tts_xvec_result.txt"


def log(msg):
    with open(RESULT, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


if os.path.exists(RESULT):
    os.remove(RESULT)
body = json.dumps({"text": "今天的天气真不错，我们一起去公园散步吧。",
                   "voice_id": "my_voice_1"}).encode("utf-8")
req = urllib.request.Request("http://127.0.0.1:8000/api/tts", data=body,
                             headers={"Content-Type": "application/json; charset=utf-8"},
                             method="POST")
try:
    t0 = time.time()
    log(f"[start] {time.strftime('%H:%M:%S')}")
    with urllib.request.urlopen(req, timeout=1200) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    log(f"[tts] {time.time() - t0:.1f}s url={data.get('url')} dur={data.get('duration_s')}s")
except Exception as e:
    body_txt = ""
    if hasattr(e, "read"):
        try:
            body_txt = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
    log(f"[FAIL] {time.time() - t0:.1f}s {e!r} {body_txt}")
log("[end]")
