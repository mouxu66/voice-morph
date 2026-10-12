# -*- coding: utf-8 -*-
"""综合评测（.venv）：CAM++ 声纹相似度 + ASR 可懂度，base vs 3 个 adapter。

读 ab_effect_multi 生成的 wav（同批次、同路径、同参考音、同 5 条文本）。
"""
import os, sys
import numpy as np
sys.path.insert(0, r"D:\变声\m2_server")
sys.path.insert(0, r"D:\变声\tts_trial")
sys.stdout.reconfigure(encoding="utf-8")
from ab_evaluate import cam_emb
from faster_whisper import WhisperModel
try:
    import zhconv
except ImportError:
    zhconv = None

REF = r"D:\变声\media\ft_work\ppt12\ref_24k.wav"
D = r"D:\变声\media\ft_work\ppt12\out_ab\ab_effect_multi"
TEXTS = [
    "这一节我们讲三个概念，先看第一个。",
    "把步骤拆开，一步一步做就不会乱。",
    "注意这里的条件，符号写反了结论就完全不同。",
    "我们下节课继续讲第二部分的内容。",
    "这道题的关键是先判断函数的单调性。",
]
ARMS = ["base", "lr2e5_ep10", "lr2e6_ep10", "v2fix"]


def emb(p):
    v = cam_emb(p)
    return None if v is None else np.asarray(v, dtype=np.float32).reshape(-1)


def cos(x, y):
    d = np.linalg.norm(x) * np.linalg.norm(y)
    return float(np.dot(x, y) / d) if d > 0 else float("nan")


def overlap(a, b):
    if zhconv is not None:
        a, b = zhconv.convert(a or "", "zh-cn"), zhconv.convert(b or "", "zh-cn")
    A, B = set(a or ""), set(b or "")
    return len(A & B) / max(len(A | B), 1) if A and B else 0.0


ref = emb(REF)
wm = WhisperModel("small", device="cuda", compute_type="float16")

print(f"{'臂':<14}{'CAM++相似度':>12}{'ASR可懂度':>11}{'平均peak':>10}{'时长s':>8}")
print("-" * 58)
res = {}
for arm in ARMS:
    sims, ovs, peaks, durs = [], [], [], []
    for i in range(1, 6):
        p = os.path.join(D, f"{arm}_{i}.wav")
        if not os.path.exists(p):
            print(f"missing {p}")
            continue
        v = emb(p)
        sims.append(cos(v, ref) if v is not None else float("nan"))
        segs, _ = wm.transcribe(p, language="zh", vad_filter=False)
        back = "".join(s.text for s in segs)
        ovs.append(overlap(back, TEXTS[i - 1]))
        import soundfile as sf
        a, sr = sf.read(p)
        peaks.append(float(np.max(np.abs(a))))
        durs.append(len(a) / sr)
    res[arm] = (float(np.mean(sims)), float(np.mean(ovs)))
    print(f"{arm:<14}{np.mean(sims):>12.4f}{np.mean(ovs):>11.3f}"
          f"{np.mean(peaks):>10.3f}{np.mean(durs):>8.2f}")

print("-" * 58)
b_sim, b_ov = res["base"]
print(f"{'Δ vs base':<14}")
for arm in ARMS[1:]:
    s, o = res[arm]
    print(f"  {arm:<12} CAM++ {s - b_sim:+.4f}   ASR {o - b_ov:+.3f}")
