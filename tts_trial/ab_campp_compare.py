# -*- coding: utf-8 -*-
"""A/B 音色相似度对比：同一 5 条文本，零样本(A) vs LoRA scale0.5(B) 与目标参考的 CAM++ 余弦。

用 .venv 跑（modelscope CAM++）。判据：sim(输出, 目标参考 ref_24k.wav)。
"""
import os, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, r"D:\变声\m2_server")
sys.stdout.reconfigure(encoding="utf-8")
import speaker_sep as SS

sys.path.insert(0, r"D:\变声\tts_trial")
from ab_evaluate import cam_emb  # 复用其长音频分段逻辑

REF = r"D:\变声\media\ft_work\ppt12\ref_24k.wav"
A = [rf"D:\变声\media\ft_work\ppt12\out_ab\A\{i:02d}.wav" for i in range(1, 6)]
B = [rf"D:\变声\media\ft_work\ppt12\out_ab\B_fixed_scale05\fx_{i:02d}.wav" for i in range(1, 6)]

def emb(p):
    v = cam_emb(p)
    if v is None:
        return None
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    return v

def cos(x, y):
    return float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y)))

ref = emb(REF)
if ref is None:
    print("目标参考 embedding 失败（过长？需分段）")
    # 分段 20s 平均
    import soundfile as sf
    a, sr = SS._read16k(REF), 16000
    a = SS._read16k(REF)
    segs = [a[i:i + 20 * 16000] for i in range(0, len(a), 20 * 16000)]
    vecs = [SS._sv_embed(s) for s in segs if len(s) >= 16000]
    vecs = [np.asarray(v, dtype=np.float32).reshape(-1) for v in vecs if v is not None]
    ref = np.mean(vecs, axis=0)
    ref = ref / np.linalg.norm(ref)
print(f"[ref] {REF}  dim={ref.shape}")

rows = []
for i, (pa, pb) in enumerate(zip(A, B), 1):
    ea, eb = emb(pa), emb(pb)
    sa = cos(ea, ref) if ea is not None else float('nan')
    sb = cos(eb, ref) if eb is not None else float('nan')
    rows.append((i, sa, sb))
    print(f"  {i}) A零样本={sa:.4f}   B_LoRA0.5={sb:.4f}   Δ(B−A)={sb - sa:+.4f}")

sa = np.mean([r[1] for r in rows])
sb = np.mean([r[2] for r in rows])
print("\n" + "=" * 60)
print(f"{'臂':<16}{'CAM++ 声纹相似度':>16}  判读")
print("-" * 60)
print(f"{'A 零样本':<16}{sa:>16.4f}")
print(f"{'B LoRA scale0.5':<16}{sb:>16.4f}   Δ={sb - sa:+.4f}")
print("=" * 60)
print("口径：8 月标定 A 臂 0.72~0.76；越高越像目标说话人")
