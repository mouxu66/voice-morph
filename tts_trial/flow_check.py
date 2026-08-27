# -*- coding: utf-8 -*-
"""整机联调收尾脚本：等挖掘完成 -> 保存音色(20s上限) -> /api/tts 合成新句。
全程结果写入 flow_result.txt，避免 PowerShell 终端编码/截断问题。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000/api"
report = []


def call(path, payload=None, timeout=600):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json; charset=utf-8"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# 0. 触发挖掘（幂等：running 时会返回 already_running）
report.append(f"[0] trigger={call('/mine/run', {})}")
# 1. 等挖掘结束
for i in range(60):
    s = call("/mine/state")
    if not s.get("running"):
        break
    time.sleep(10)
report.append(f"[1] mine stage={s.get('stage')} kept={s.get('kept')} clusters={len(s.get('clusters', []))}")
if s.get("stage") != "done" or not s.get("clusters"):
    report.append("[!] 挖掘未产出候选，终止")
else:
    c0 = s["clusters"][0]
    report.append(f"[2] 候选0 size={c0['size']} rep={c0['rep']['name']} text={c0['rep']['text'][:40]}")
    # 2. 保存（server 端会按 20s 截断）
    save = call("/mine/save", {"clip": c0["rep"]["name"], "voice_id": "my_voice_1",
                               "display_name": "我的音色", "members": c0["members"]})
    report.append(f"[3] save={save}")
    # 3. TTS 合成全新句子
    t0 = time.time()
    tts = call("/tts", {"text": "今天的天气真不错，我们一起去公园散步吧。", "voice_id": "my_voice_1"}, timeout=600)
    report.append(f"[4] tts {time.time()-t0:.1f}s url={tts.get('url')} dur={tts.get('duration_s')}s voice={tts.get('voice_id')}")
    # 4. 参考音频确认
    import os
    ref = r"D:\变声\media\voicebank\my_voice_1\reference.wav"
    report.append(f"[5] reference.wav size={os.path.getsize(ref)} bytes")

with open(r"D:\变声\tts_trial\flow_result.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(report))
print("done")
