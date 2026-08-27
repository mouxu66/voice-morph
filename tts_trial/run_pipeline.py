# -*- coding: utf-8 -*-
"""触发解析流水线并等待完成，然后音色挖掘。逐步写盘 pipe_out.txt。"""
import json
import os
import time
import urllib.request

BASE = "http://127.0.0.1:8000/api"
OUT = r"D:\变声\tts_trial\pipe_out.txt"


def log(m):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(m + "\n")


def call(path, payload=None, timeout=60):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json; charset=utf-8"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


open(OUT, "w").close()
try:
    r = call("/pipeline/run", {})
    log(f"[pipeline] {r}")
    for i in range(90):
        time.sleep(10)
        s = call("/pipeline/status")
        log(f"[p{i}] {s.get('status')} {s.get('step')} {s.get('percent')}% clips={s.get('clips')} {s.get('message','')[:50]}")
        if s.get("status") != "running":
            break
    if s.get("status") != "done":
        raise RuntimeError(f"流水线未完成: {s.get('message')}, err={s.get('error','')}")
    # 挖掘
    r2 = call("/mine/run", {})
    log(f"[mine] {r2}")
    for i in range(90):
        time.sleep(15)
        m = call("/mine/state")
        if not m.get("running"):
            break
    log(f"[mine-done] stage={m.get('stage')} kept={m.get('kept')} clusters={len(m.get('clusters', []))} msg={m.get('message','')}")
except Exception as e:
    import traceback
    log("[FAIL] " + repr(e))
    log(traceback.format_exc())
log("[end]")
