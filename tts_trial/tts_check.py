# -*- coding: utf-8 -*-
"""整机联调：/api/tts 端到端验证。结果写入 tts_result.txt 避免终端编码问题。"""
import json
import traceback
import urllib.error
import urllib.request

result = []
body = json.dumps({"text": "今天的天气真不错，我们一起去公园散步吧。",
                   "voice_id": "my_voice_1"}).encode("utf-8")
req = urllib.request.Request(
    "http://127.0.0.1:8000/api/tts", data=body,
    headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result.append("OK url=%s voice=%s dur=%s" % (
        data.get("url"), data.get("voice_id"), data.get("duration_s")))
except Exception as e:
    body_txt = ""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body_txt = e.read().decode("utf-8", "replace")
        except Exception:
            pass
    result.append("FAIL %r" % e)
    result.append(body_txt)
    result.append(traceback.format_exc())

with open(r"D:\变声\tts_trial\tts_result.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(result))
print("written")
