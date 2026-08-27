# -*- coding: utf-8 -*-
"""保存候选音色(20s截断) -> /api/tts 合成新句验证。每步立刻写盘。"""
import json
import os
import time
import urllib.request

BASE = "http://127.0.0.1:8000/api"
RESULT = r"D:\变声\tts_trial\save_tts_result.txt"


def log(msg):
    with open(RESULT, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def call(path, payload=None, timeout=600):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json; charset=utf-8"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


if os.path.exists(RESULT):
    os.remove(RESULT)
try:
    s = call("/mine/state")
    log(f"[state] stage={s.get('stage')} clusters={len(s.get('clusters', []))}")
    c0 = s["clusters"][0]
    save = call("/mine/save", {"clip": c0["rep"]["name"], "voice_id": "my_voice_1",
                               "display_name": "我的音色", "members": c0["members"]})
    log(f"[save] {save}")
    ref = r"D:\变声\media\voicebank\my_voice_1\reference.wav"
    log(f"[ref] {os.path.getsize(ref)} bytes")
    t0 = time.time()
    tts = call("/tts", {"text": "今天的天气真不错，我们一起去公园散步吧。",
                        "voice_id": "my_voice_1"}, timeout=900)
    log(f"[tts] {time.time() - t0:.1f}s url={tts.get('url')} dur={tts.get('duration_s')}s")
except Exception as e:
    import traceback
    log("[FAIL] " + repr(e))
    log(traceback.format_exc())
log("[end]")
