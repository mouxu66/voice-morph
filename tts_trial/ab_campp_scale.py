# -*- coding: utf-8 -*-
"""scale 与音色相似度的关系：tf5.16 下 v2fix 各 scale 产物 vs 目标 ref 的 CAM++。

文本与 A/01 相同（「这一节我们讲三个概念，先看第一个。」），故 A/01 可作同句零样本基线。
"""
import os, sys
import numpy as np
sys.path.insert(0, r"D:\变声\m2_server")
sys.path.insert(0, r"D:\变声\tts_trial")
sys.stdout.reconfigure(encoding="utf-8")
from ab_evaluate import cam_emb

REF = r"D:\变声\media\ft_work\ppt12\ref_24k.wav"
A01 = r"D:\变声\media\ft_work\ppt12\out_ab\A\01.wav"
SWEEP = r"D:\变声\media\ft_work\ppt12\out_ab\scale_sweep"


def emb(p):
    v = cam_emb(p)
    return None if v is None else np.asarray(v, dtype=np.float32).reshape(-1)


def cos(x, y):
    return float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y)))


ref = emb(REF)
rows = [("A 零样本(同句)", A01)]
for s in ("0.0", "0.4", "0.5", "1.0", "2.0"):
    p = os.path.join(SWEEP, f"s{s}.wav")
    if os.path.exists(p):
        rows.append((f"LoRA scale {s}", p))

print(f"{'配置':<18}{'CAM++ 相似度':>14}")
print("-" * 34)
for name, p in rows:
    e = emb(p)
    if e is None:
        print(f"{name:<18}{'None':>14}")
        continue
    print(f"{name:<18}{cos(e, ref):>14.4f}")
