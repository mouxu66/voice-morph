# -*- coding: utf-8 -*-
"""只读诊断 ⑦：为「bf16 + Adafactor」找一个能真正更新的 lr 区间。

已确证（probe_adafactor_torch.py 真 torch 实测 + 读 torch 源码）：
  · torch Adafactor 是**相对步长**算法：alpha = max(eps2, RMS(θ)) · min(lr, 1/√t)
  · sft_8gb.py 把 **AdamW 的 lr=2e-5** 直接喂给了 Adafactor
  · 于是绝对步长 = RMS(θ)·2e-5 ≈ 2e-7~4e-6，而 bf16 的 ulp ≈ 9e-5
  · 实测：bf16 参数 60 步只动 53/4096 个元素（fp32 动 4094/4096，差 64 倍）

torch 的 Adafactor 文档自己写明：lr 只是 ρ_t 的**上限**，默认值应当是 1e-2。
本脚本扫 lr，找出「单步位移 ≥ 1 个 ulp」的临界值，给修复提供依据。
"""
import sys

import torch

sys.stdout.reconfigure(encoding="utf-8")

STEPS = 60
W_SHAPE = (64, 64)


def run(lr, dtype, seed=0, steps=STEPS):
    torch.manual_seed(seed)
    w0 = torch.randn(*W_SHAPE, dtype=torch.float32) * 0.05   # σ=0.05 ⇒ RMS≈0.05
    if dtype == torch.bfloat16:
        w0 = w0.to(torch.bfloat16)
    p = torch.nn.Parameter(w0.clone())
    opt = torch.optim.Adafactor([p], lr=lr, weight_decay=0.01)
    start = p.detach().clone().to(torch.float64)
    for _ in range(steps):
        p.grad = torch.randn(p.shape, dtype=dtype).to(p.dtype)
        opt.step()
        opt.zero_grad()
    delta = (p.detach().to(torch.float64) - start).abs()
    ulp = 2.0 ** (torch.floor(torch.log2(start.abs().clamp(min=1e-12))) - 7)
    return {
        "changed": int((delta > 0).sum()),
        "n": p.numel(),
        "mean_delta": float(delta.mean()),
        "median_ulp": float(ulp.median()),
        "rms0": float(start.pow(2).mean().sqrt()),
    }


def main():
    print("=" * 88)
    print(f"Adafactor lr 扫描（真 torch，{STEPS} 步，权重 σ=0.05 ⇒ RMS≈0.05）")
    print("=" * 88)
    print(f"{'lr':>10} {'存储':>6} {'RMS(θ)·lr':>12} {'有变化元素':>14} {'平均|Δ|':>12} {'Δ/ulp':>9} {'判定':>8}")
    print("-" * 88)

    base = run(1e-2, torch.float32, steps=1)
    rms0 = base["rms0"]
    ulp = base["median_ulp"]

    first_ok = None
    for lr in (2e-5, 1e-4, 5e-4, 1e-3, 5e-3, 1e-2, 2e-2):
        for dtype, label in ((torch.bfloat16, "bf16"), (torch.float32, "fp32")):
            r = run(lr, dtype)
            ratio = r["mean_delta"] / r["median_ulp"]
            ok = "✅" if (ratio >= 1.0 and r["changed"] > r["n"] * 0.5) else (
                "⚠️部分" if r["changed"] > 0 else "❌不动")
            if dtype == torch.bfloat16 and ratio >= 1.0 and first_ok is None:
                first_ok = lr
            print(f"{lr:>10.0e} {label:>6} {rms0*lr:>12.2e} "
                  f"{r['changed']:>6}/{r['n']:<7} {r['mean_delta']:>12.3e} "
                  f"{ratio:>9.4f} {ok:>8}")

    print("\n" + "=" * 88)
    print("判读")
    print("=" * 88)
    print(f"  本组权重 RMS(θ)≈{rms0:.4f}，bf16 ulp≈{ulp:.2e}")
    print(f"  · lr=2e-5（生产现值）：bf16 下 Δ/ulp≈{run(2e-5, torch.bfloat16)['mean_delta']/ulp:.5f}"
          f" ⇒ 步长比 ulp 小两个数量级，**权重不动**")
    print(f"  · 单步位移追上 1 个 ulp 需要 RMS(θ)·lr ≈ ulp"
          f" ⇒ lr ≈ {ulp/rms0:.2e}")
    if first_ok:
        print(f"  · 实测首个「bf16 能正常更新」的 lr = {first_ok:.0e}"
              f"（≈ 生产现值的 {first_ok/2e-5:.0f} 倍）")
    print()
    print("  ★ torch Adafactor 文档自述：lr 是 ρ_t 的**上限**，默认应为 1e-2。")
    print("    生产脚本把 AdamW 尺度的 2e-5 喂进来，等于把相对步长压低 500 倍 ——")
    print("    这是「AdamW → Adafactor」这次 8GB 适配时**没有换算量纲**造成的。")


if __name__ == "__main__":
    main()
