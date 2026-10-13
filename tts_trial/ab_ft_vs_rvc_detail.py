# -*- coding: utf-8 -*-
"""逐条明细 + 稳定性：Qwen 微调 vs RVC（防止 n=3 的均值掩盖逐条差异）。"""
import os
import sys

import numpy as np

sys.path.insert(0, r"D:\变声\m2_server")
sys.path.insert(0, r"D:\变声\tts_trial")
sys.stdout.reconfigure(encoding="utf-8")

from ab_evaluate import cam_emb  # noqa: E402

PPT = r"D:\变声\media\ft_work\ppt12"


def emb(p):
    v = cam_emb(p)
    return None if v is None else np.asarray(v, dtype=np.float32).reshape(-1)


def cos(x, y):
    d = float(np.linalg.norm(x)) * float(np.linalg.norm(y))
    return float(np.dot(x, y) / d) if d > 0 else float("nan")


ROWS = [
    ("base", [f"out_ab/ab_effect_multi/base_{i}.wav" for i in (1, 2, 3)]),
    ("qwen_ft(ep09)", [f"out_ab/ab_effect_multi/ep09_{i}.wav" for i in (1, 2, 3)]),
    ("rvc", [f"myvoice_rvc_full/out_tts0{i}.wav" for i in (1, 2, 3)]),
]


def main():
    ref = emb(os.path.join(PPT, "ref_24k.wav"))
    data = {}
    print(f"{'句':<4}" + "".join(f"{n:>16}" for n, _ in ROWS))
    print("-" * 54)
    for k in range(3):
        line = f"{k + 1:<4}"
        for n, paths in ROWS:
            c = cos(emb(os.path.join(PPT, paths[k])), ref)
            data.setdefault(n, []).append(c)
            line += f"{c:>16.4f}"
        print(line)
    print("-" * 54)
    print(f"{'均值':<4}" + "".join(f"{np.mean(data[n]):>16.4f}" for n, _ in ROWS))

    print()
    for n, _ in ROWS:
        v = data[n]
        print(f"{n:<16} 逐条 {[round(x, 4) for x in v]}  "
              f"极差 {max(v) - min(v):.4f}  样本标准差 {np.std(v, ddof=1):.4f}")

    print()
    # 逐句比较：RVC 是否**每一句**都赢
    print("逐句对比 rvc − qwen_ft：")
    for k in range(3):
        d = data["rvc"][k] - data["qwen_ft(ep09)"][k]
        print(f"  第 {k + 1} 句：{d:+.4f}  {'RVC 更近' if d > 0 else '微调更近'}")
    wins = sum(1 for k in range(3) if data["rvc"][k] > data["qwen_ft(ep09)"][k])
    print(f"⇒ RVC 在 {wins}/3 句上更接近参考音")


if __name__ == "__main__":
    main()
