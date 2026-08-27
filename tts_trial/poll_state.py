# -*- coding: utf-8 -*-
"""轮询 /mine/state 直到结束，结果写入 poll.txt。"""
import json
import time
import urllib.request

for i in range(45):
    try:
        s = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/mine/state"))
    except Exception as e:
        s = {"stage": "connfail", "message": str(e)}
    line = f"{i} stage={s.get('stage')} kept={s.get('kept')} clusters={len(s.get('clusters', []))} running={s.get('running')} msg={s.get('message', '')}"
    open(r"D:\变声\tts_trial\poll.txt", "w", encoding="utf-8").write(line)
    if not s.get("running"):
        break
    time.sleep(15)
print("poll done")
