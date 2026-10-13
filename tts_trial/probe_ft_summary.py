# -*- coding: utf-8 -*-
"""只读诊断 ⑥【收口】：把根因量化成一张表 —— 为什么全参 bf16 不学、LoRA fp32 能学。

已确证的三件事（来源见各行注释）：
  1. 生产 sft_8gb.py 用 Adafactor，torch 实现是**相对步长**：
        alpha = max(eps2, RMS(θ)) * min(lr, 1/√t)      (_adafactor.py:376-377)
     ⇒ 绝对步长 ≈ RMS(θ) * lr，**比 AdamW 的绝对步长 lr 小 RMS(θ) 倍**。
  2. 生产全参的权重是 bf16（实测 2.00~2.15 B/param），QLoRA 的 adapter 是 fp32
     （实测 4.00 B/param）。
  3. 真 torch 实测（probe_adafactor_torch.py）：同 lr、同步数，
     bf16 参数只动 53/4096 个元素、平均 |Δ|=9.5e-08；
     fp32 参数动 4094/4096 个、平均 |Δ|=6.1e-06（**64 倍**）。

本脚本读真实权重，把「单步绝对位移 / bf16 的 ulp」逐层算出来。
"""
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(r"D:/变声/tts_models/qwen3-tts-1.7b-base")
LR_ADAFACTOR = 2e-5      # sft_8gb.py 默认
LR_ADAMW = 2e-5          # 官方 sft_12hz.py 默认（同值，但语义不同）
EPS2 = 1e-3


def bf16_ulp(x: np.ndarray) -> np.ndarray:
    """bf16 在 |x| 处的绝对间隔（ulp）：2^(floor(log2|x|) - 7)。"""
    a = np.abs(x)
    a = np.maximum(a, np.finfo(np.float32).tiny)
    return 2.0 ** (np.floor(np.log2(a)) - 7)


def main():
    import torch
    from safetensors.torch import load_file

    print("=" * 92)
    print("逐层：单步位移 vs bf16 ulp（lr=2e-5，Adafactor 相对步长语义）")
    print("=" * 92)
    print(f"{'层':<44} {'RMS(θ)':>9} {'Adafactor步':>12} {'AdamW步':>11} {'bf16 ulp':>11} {'Ada/ulp':>9} {'Ada能学?':>9}")
    print("-" * 92)

    rows = []
    sd = load_file(str(BASE / "model.safetensors"), device="cpu")
    # 按张量名分组取样：talker 注意力和 MLP（LoRA 的目标）、嵌入、声码器
    picked = 0
    for name, t in sd.items():
        if not t.is_floating_point() or t.numel() < 1 << 16:
            continue
        if not any(k in name for k in ("talker.model.layers", "codec_embedding",
                                       "text_embedding", "speaker_encoder",
                                       "code_predictor")):
            continue
        w = t.float().numpy()
        rms = float(np.sqrt(np.mean(w.astype(np.float64) ** 2)))
        ada = max(EPS2, rms) * LR_ADAFACTOR          # 绝对步长
        ulp = float(np.median(bf16_ulp(w)))
        short = name if len(name) <= 43 else "…" + name[-42:]
        ratio = ada / ulp
        print(f"{short:<44} {rms:>9.5f} {ada:>12.2e} {LR_ADAMW:>11.2e} "
              f"{ulp:>11.2e} {ratio:>9.5f} {'否' if ratio < 0.5 else '是':>9}")
        rows.append((ada, ulp, ratio))
        picked += 1
        if picked >= 12:
            break

    ada_all = np.array([r[0] for r in rows])
    ulp_all = np.array([r[1] for r in rows])
    ratio_all = np.array([r[2] for r in rows])

    print("\n" + "=" * 92)
    print("结论")
    print("=" * 92)
    print(f"  12 层的 Adafactor 单步位移 中位 {np.median(ada_all):.2e}，"
          f"bf16 ulp 中位 {np.median(ulp_all):.2e}")
    print(f"  ⇒ 单步 / ulp 中位 = {np.median(ratio_all):.5f}"
          f"（max {ratio_all.max():.5f}）")
    print(f"  ⇒ 步长比 bf16 能表示的最小间隔还小 "
          f"{1/np.median(ratio_all):.0f} 倍 ⇒ **权重舍入后原地不动**")
    print()
    print(f"  对照 AdamW（官方脚本）：绝对步长 = lr = {LR_ADAMW:.1e}")
    print(f"  ⇒ AdamW/ulp 中位 = {LR_ADAMW/np.median(ulp_all):.5f}"
          f" ⇒ 比 Adafactor 大 {np.median(RMS_RATIO := (LR_ADAMW/ada_all)):.1f} 倍")
    print()
    print("  两个变化叠在一起，都来自「8GB 适配」这一步改动：")
    print("    ① AdamW → Adafactor：绝对步长被乘上了 RMS(θ)≈0.01~0.2（本表第 3/4 列）")
    print("    ② 全参 bf16 存储：可表示的最小相对变化是 2^-8≈3.9e-3")
    print("  ⇒ 二者相乘后，单步更新落到 bf16 的精度之下 ⇒ loss 冻结在 14.5。")


if __name__ == "__main__":
    main()
