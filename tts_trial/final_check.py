# -*- coding: utf-8 -*-
"""肥嘟嘟素材收尾：试听 -> 清旧档 -> 保存为 my_voice_1 -> /tts 验证。逐步写盘 final_out.txt。"""
import json
import os
import shutil
import time
import urllib.request

BASE = "http://127.0.0.1:8000/api"
OUT = r"D:\变声\tts_trial\final_out.txt"


def log(m):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(m + "\n")


def call(path, payload=None, timeout=900):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json; charset=utf-8"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


open(OUT, "w").close()
try:
    s = call("/mine/state")
    c0 = s["clusters"][0]
    log(f"[cand] size={c0['size']} rep={c0['rep']['name']}")
    pv = call("/mine/preview", {"clip": c0["rep"]["name"], "text": ""})
    log(f"[preview] text={pv.get('text')} url={pv.get('url')} dur={pv.get('duration_s')}s")
    vb = r"D:\变声\media\voicebank\my_voice_1"
    if os.path.isdir(vb):
        shutil.rmtree(vb)
        log("[cleaned] old my_voice_1 removed")
    sv = call("/mine/save", {"clip": c0["rep"]["name"], "voice_id": "my_voice_1",
                             "display_name": "我的音色", "members": c0["members"]})
    log(f"[save] {sv}")
    t0 = time.time()
    tt = call("/tts", {"text": "今天的天气真不错，我们一起去公园散步吧。",
                       "voice_id": "my_voice_1"})
    log(f"[tts] {time.time() - t0:.1f}s url={tt.get('url')} dur={tt.get('duration_s')}s")
except Exception as e:
    import traceback
    log("[FAIL] " + repr(e))
    log(traceback.format_exc())
log("[end]")
