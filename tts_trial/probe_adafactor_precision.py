# -*- coding: utf-8 -*-
"""只读诊断 ⑤【决定性】：bf16 权重的相对精度 vs Adafactor 的相对步长。

读源码得到的公式（torch/optim/_adafactor.py:376-412）：

    rho_t   = min(lr, 1/sqrt(t))                              ← 相对步长上限
    alpha   = max(eps2, RMS(θ)) * rho_t                       ← 乘上参数自身 RMS
    θ_t     = θ_{t-1} - alpha * Û_t      （Û 已归一化到 RMS ≤ 1）

⇒ Adafactor 是**相对步长**算法：单步的**相对**变化量 ≈ rho_t = lr = 2e-5。
   而 bf16 只有 8 位尾数 ⇒ 相对精度 ≈ 2^-8 = 3.9e-3（半个 ulp 是 1.95e-3）。

   相对更新 2e-5  ÷  相对精度 3.9e-3  ≈  1/200
   ⇒ **权重在 bf16 里根本表示不出这一步的变化，训练原地不动。**

本脚本用真实权重把这个比值与"多步后到底动了多少"算出来。
不 import torch 之外的任何东西，只读权重，不训练。
"""
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(r"D:/变声/tts_models/qwen3-tts-1.7b-base")
LR = 2e-5          # sft_8gb.py 与官方 sft_12hz.py 的默认 lr
EPS2 = 1e-3        # torch Adafactor 默认 eps=(None, 1e-3)


def bf16_round(x: np.ndarray) -> np.ndarray:
    """float32 → bf16 舍入 → float32。"""
    b = np.ascontiguousarray(x, dtype=np.float32).view(np.uint32)
    lsb = (b >> 16) & 1
    b = (b + (0x7FFF + lsb)) & 0xFFFF0000
    return b.view(np.float32)


def load_weights(max_tensors=16):
    import torch
    from safetensors.torch import load_file

    out = []
    for shard in sorted(BASE.glob("*.safetensors")):
        sd = load_file(str(shard), device="cpu")
        for name, t in sd.items():
            if t.is_floating_point() and t.numel() >= 1 << 16:
                out.append((name, t.float().numpy()))
            if len(out) >= max_tensors:
                break
        del sd
        if len(out) >= max_tensors:
            break
    return out


def simulate_adafactor_bf16(w0: np.ndarray, steps: int, seed: int = 0):
    """按 Adafactor 的相对步长语义模拟，权重存 bf16。

    简化但不失真：每步的归一化更新 Û 的逐元素 RMS ≈ 1（这正是 line 411 的
    denom 归一化的目的），方向每步独立（不同 batch）。
    """
    rng = np.random.default_rng(seed)
    w = bf16_round(w0.astype(np.float32)).astype(np.float64)
    rms0 = float(np.sqrt(np.mean(w ** 2)))
    w_start = w.copy()
    moved_steps = 0
    for t in range(1, steps + 1):
        rho = min(LR, 1.0 / np.sqrt(t))
        alpha = max(EPS2, rms0) * rho
        u = rng.normal(0, 1, w.shape)          # 归一化更新的方向
        u /= max(1e-12, float(np.sqrt(np.mean(u ** 2))))   # 归一到 RMS=1
        w = bf16_round((w - alpha * u).astype(np.float32)).astype(np.float64)
        if not np.array_equal(w, w_start) if moved_steps == 0 else False:
            moved_steps = t
    delta = np.abs(w - w_start)
    return delta, w_start, rms0


def main():
    ws = load_weights()
    print(f"[data] {len(ws)} 个真实权重张量\n")

    print("=" * 78)
    print("① 单步：相对步长 vs bf16 相对精度")
    print("=" * 78)
    print(f"lr(相对步长上限) = {LR:.1e}      bf16 尾数 = 8 位")
    bf16_rel = 2.0 ** -8
    print(f"bf16 相对精度(1 ulp) = 2^-8 = {bf16_rel:.3e}")
    print(f"bf16 相对精度(½ ulp) =        {bf16_rel/2:.3e}")
    print(f"\n相对更新 / 相对精度 = {LR:.1e} / {bf16_rel:.3e} = "
          f"**1/{bf16_rel/LR:.0f}**")
    print("⇒ 单步相对变化比 bf16 能表示的最小相对变化还小两个数量级。")

    print("\n" + "=" * 78)
    print("② 按 Adafactor 语义实跑 480 步（≈10 epoch × 48 次真更新）")
    print("=" * 78)
    print(f"{'张量':<34} {'RMS(θ)':>10} {'单步|Δ|':>12} {'bf16 ulp':>12} {'Δ/ulp':>9} {'480步后变化':>12}")
    print("-" * 78)
    for name, w0 in ws[:12]:
        rms = float(np.sqrt(np.mean(w0.astype(np.float64) ** 2)))
        alpha = max(EPS2, rms) * LR
        ulp = 2.0 ** (np.floor(np.log2(max(rms, 1e-12))) - 7)
        delta, w_start, _ = simulate_adafactor_bf16(w0, 480, seed=7)
        n_changed = np.count_nonzero(delta > 0)
        short = name[-33:] if len(name) > 33 else name
        print(f"{short:<34} {rms:>10.5f} {alpha:>12.2e} {ulp:>12.2e} "
              f"{alpha/ulp:>9.4f} {100*n_changed/w0.size:>11.2f}%")

    print("\n" + "=" * 78)
    print("③ 同样 480 步，但参数存 fp32（= QLoRA 的 LoRA 层）")
    print("=" * 78)
    print(f"{'张量':<34} {'480步后变化元素':>16} {'平均相对净变化':>16}")
    print("-" * 78)
    for name, w0 in ws[:12]:
        rng = np.random.default_rng(7)
        w = w0.astype(np.float64).copy()
        start = w.copy()
        rms0 = float(np.sqrt(np.mean(w ** 2)))
        for t in range(1, 481):
            rho = min(LR, 1.0 / np.sqrt(t))
            alpha = max(EPS2, rms0) * rho
            u = rng.normal(0, 1, w.shape)
            u /= max(1e-12, float(np.sqrt(np.mean(u ** 2))))
            w = w - alpha * u
        delta = np.abs(w - start)
        rel = np.mean(delta / np.maximum(np.abs(start), 1e-12))
        short = name[-33:] if len(name) > 33 else name
        print(f"{short:<34} {100*np.count_nonzero(delta>0)/w0.size:>15.2f}% {rel:>15.2%}")

    print("\n[读法] ② 的『480步后变化』若全为 0.00%，即证明 bf16 权重**一步都没动**；")
    print("       ③ 同行在 fp32 下显著非零 ⇒ 同样的 lr 在 fp32 里能正常累积。")
    print("       这正好解释：生产全参(bf16) loss 冻在 14.5，QLoRA(fp32) 降到 4.85。")


if __name__ == "__main__":
    main()
