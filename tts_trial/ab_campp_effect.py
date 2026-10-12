# -*- coding: utf-8 -*-
"""对 ab_effect_compare 生成的同批次 wav 算 CAM++（在 .venv 跑，modelscope 在这边）。"""
import os, sys
import numpy as np
sys.path.insert(0, r"D:\变声\m2_server")
sys.path.insert(0, r"D:\变声\tts_trial")
sys.stdout.reconfigure(encoding="utf-8")
from ab_evaluate import cam_emb

REF = r"D:\变声\media\ft_work\ppt12\ref_24k.wav"
D = r"D:\变声\media\ft_work\ppt12\out_ab\ab_effect"


def emb(p):
    v = cam_emb(p)
    return None if v is None else np.asarray(v, dtype=np.float32).reshape(-1)


def cos(x, y):
    d = np.linalg.norm(x) * np.linalg.norm(y)
    return float(np.dot(x, y) / d) if d > 0 else float("nan")


ref = emb(REF)
print(f"[ref] dim={ref.shape}")
rows = []
for i in range(1, 6):
    for tag in ("base", "lora"):
        p = os.path.join(D, f"{tag}_{i}.wav")
        if not os.path.exists(p):
            print(f"missing {p}")
            continue
        v = emb(p)
        rows.append((tag, i, cos(v, ref) if v is not None else float("nan")))

for tag in ("base", "lora"):
    sims = [s for t, i, s in rows if t == tag and not np.isnan(s)]
    print(f"{tag:<6} " + "  ".join(f"{i}:{s:.4f}" for t, i, s in rows if t == tag))

sb = np.mean([s for t, i, s in rows if t == "base"])
sl = np.mean([s for t, i, s in rows if t == "lora"])
print("\n" + "=" * 56)
print(f"{'同路径同批次对比':<24}{'CAM++相似度':>12}")
print("-" * 56)
print(f"{'base（无LoRA）':<24}{sb:>12.4f}")
print(f"{'+LoRA scale0.5':<24}{sl:>12.4f}   Δ={sl - sb:+.4f}")
print("=" * 56)
