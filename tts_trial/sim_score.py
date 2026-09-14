# -*- coding: utf-8 -*-
"""20 秒样本客观打分：用 feidudu_merged 人声做参考生成 TTS，再算声纹余弦相似度。

基线：
  self  = 参考音频前半 vs 后半（同一人不同内容的典型相似度）
  cross = 参考 vs 另一个说话人视频（典型的"不是同一个人"）
结果写 sim_out.txt。
"""
import io
import json
import os
import time
import urllib.request

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "sim_out.txt")
WORKER = "http://127.0.0.1:8001"

REF = r"D:\变声\media\vocals\feidudu_merged.wav"          # 用户 19.4s 样本
CROSS = r"D:\变声\media\vocals\第三方素材_neg.wav"          # 异说话人负样本（片名已脱敏，issue #3）
SELF_A = os.path.join(HERE, "sim_ref_a.wav")
SELF_B = os.path.join(HERE, "sim_ref_b.wav")
GEN1 = os.path.join(HERE, "sim_gen_1.wav")
GEN2 = os.path.join(HERE, "sim_gen_2.wav")

TEXT1 = "今天早上我出门的时候，发现楼下的猫已经等在门口了。"
TEXT2 = "这个问题其实很简单，只要把步骤拆开一步一步做就行。"


def log(m):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(m + "\n")


open(OUT, "w").close()


def post(path: str, payload: dict, timeout: int = 600):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(WORKER + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def emb_of(path: str) -> np.ndarray:
    body = json.loads(post("/emb", {"path": path}).decode("utf-8"))
    if "error" in body:
        raise RuntimeError(f"emb {path}: {body['error']}")
    return np.asarray(body["emb"], dtype=np.float32)


def cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a / (np.linalg.norm(a) + 1e-9), b / (np.linalg.norm(b) + 1e-9)))


log(f"[start] {time.strftime('%H:%M:%S')}")

# 等 worker 就绪
for i in range(60):
    try:
        urllib.request.urlopen(WORKER + "/health", timeout=3)
        log("[worker] ready")
        break
    except Exception:
        time.sleep(2)
else:
    log("[FAIL] worker 8001 未就绪")
    raise SystemExit

# 自一致基线：把 19.4s 参考切成前后两半
audio, sr = sf.read(REF)
n = audio.shape[0] // 2
sf.write(SELF_A, audio[:n], sr)
sf.write(SELF_B, audio[n:], sr)
log(f"[ref] {os.path.basename(REF)} dur={audio.shape[0]/sr:.1f}s")

# 生成两段 TTS（x-vector 声纹模式）
t0 = time.time()
wav_bytes = post("/tts", {"text": TEXT1, "language": "Chinese",
                          "ref_audio": REF, "ref_text": ""})
open(GEN1, "wb").write(wav_bytes)
gen1, gsr = sf.read(io.BytesIO(wav_bytes))
log(f"[gen1] {gen1.shape[0]/gsr:.1f}s in {time.time()-t0:.0f}s")

t0 = time.time()
wav_bytes = post("/tts", {"text": TEXT2, "language": "Chinese",
                          "ref_audio": REF, "ref_text": ""})
open(GEN2, "wb").write(wav_bytes)
gen2, _ = sf.read(io.BytesIO(wav_bytes))
log(f"[gen2] {gen2.shape[0]/gsr:.1f}s in {time.time()-t0:.0f}s")

# 打分
emb_ref = emb_of(REF)
e_a, e_b = emb_of(SELF_A), emb_of(SELF_B)
e_g1, e_g2 = emb_of(GEN1), emb_of(GEN2)
e_cross = emb_of(CROSS)

rows = [
    ("self 一致性（同人前半vs后半）", cos(e_a, e_b)),
    ("cross 负基线（参考vs他人视频）", cos(emb_ref, e_cross)),
    ("TTS第1段 vs 你的20秒样本", cos(e_g1, emb_ref)),
    ("TTS第2段 vs 你的20秒样本", cos(e_g2, emb_ref)),
]
log("")
log("[scores] 余弦相似度")
for name, s in rows:
    log(f"  {name}: {s:.3f}")
log("")
log("[判读] TTS 分数接近 self 基线≈很像；靠近 cross 基线≈不像。一般同人 >0.75，跨人 <0.4")
log(f"[out] {GEN1}\n[out] {GEN2}")
log(f"[end] {time.strftime('%H:%M:%S')}")
