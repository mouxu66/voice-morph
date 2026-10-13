# -*- coding: utf-8 -*-
"""跨 seed 稳健性：RVC 的两套 seed 结果 vs Qwen 微调。

项目铁律（犯错指南 #167）：**固定 seed ≠ 结论稳健** —— 单 seed 的点不能当结论，
要报区间。RVC 那条线已跑过 seed=1234 与 seed=4321 两轮（同一模型、同一输入），
本脚本把两轮与 Qwen 微调放在一起，看结论是否跨 seed 成立。
"""
import os
import sys

import numpy as np

sys.path.insert(0, r"D:\变声\m2_server")
sys.path.insert(0, r"D:\变声\tts_trial")
sys.stdout.reconfigure(encoding="utf-8")

from ab_evaluate import cam_emb  # noqa: E402

PPT = r"D:\变声\media\ft_work\ppt12"
DIRS = [
    ("RVC seed=1234", os.path.join(PPT, "myvoice_rvc_full")),
    ("RVC seed=4321", os.path.join(PPT, "myvoice_rvc_full_s4321")),
]


def emb(p):
    v = cam_emb(p)
    return None if v is None else np.asarray(v, dtype=np.float32).reshape(-1)


def cos(x, y):
    d = float(np.linalg.norm(x)) * float(np.linalg.norm(y))
    return float(np.dot(x, y) / d) if d > 0 else float("nan")


def main():
    ref = emb(os.path.join(PPT, "ref_24k.wav"))

    # Qwen 微调（同 3 句）
    ft = [cos(emb(os.path.join(PPT, f"out_ab/ab_effect_multi/ep09_{i}.wav")), ref)
          for i in (1, 2, 3)]
    print(f"Qwen LoRA ep09（3 句逐条）：{[round(x, 4) for x in ft]}")
    print(f"  均值 {np.mean(ft):.4f}\n")

    print(f"{'RVC 轮次':<16}{'逐条':<40}{'均值':>8}")
    print("-" * 68)
    means = []
    for label, d in DIRS:
        vals = []
        for i in (1, 2, 3):
            p = os.path.join(d, f"out_tts0{i}.wav")
            vals.append(cos(emb(p), ref) if os.path.exists(p) else float("nan"))
        m = float(np.nanmean(vals))
        means.append(m)
        print(f"{label:<16}{str([round(x, 4) for x in vals]):<40}{m:>8.4f}")

    lo, hi = min(means), max(means)
    print("\n" + "=" * 68)
    print("结论（按项目口径报区间）")
    print("=" * 68)
    print(f"  RVC        ：{lo:.4f} ~ {hi:.4f}   （跨 seed）")
    print(f"  Qwen 微调  ：{np.mean(ft):.4f}      （单轮，同批次可比）")
    print(f"  差值       ：RVC − 微调 = {lo - np.mean(ft):+.4f} ~ {hi - np.mean(ft):+.4f}")
    if lo > np.mean(ft):
        print("  ⇒ 两个 seed 的 RVC 都高于微调，**区间不重叠** ⇒ 结论跨 seed 稳健。")
    else:
        print("  ⇒ 有重叠 ⇒ 只能说「多数情况下更高」，不能报成确定优势。")

    # 顺带：RVC 的天花板（真人原音过同一模型）
    for label, d in DIRS:
        p = os.path.join(d, "out_real.wav")
        if os.path.exists(p):
            print(f"\n  {label} 音色天花板（真人原音→RVC）：{cos(emb(p), ref):.4f}")


if __name__ == "__main__":
    main()
