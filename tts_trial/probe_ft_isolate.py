# -*- coding: utf-8 -*-
"""只读诊断 ⑧【定因】：分离「优化器量纲」与「bf16 存储」两个因素。

生产 sft_8gb.py 相对官方 sft_12hz.py 改了两处（见文件头注释）：
    ① AdamW → Adafactor（为省显存）
    ② 未改 lr：仍是 AdamW 尺度的 2e-5

已知（读 torch 源码 + 实测）：
    AdamW      绝对步长 ≈ lr
    Adafactor  绝对步长 ≈ RMS(θ) · lr      ← 少了一个 RMS(θ) 因子（≈0.01~0.2）

本脚本做 2×2×lr 对照，逐个隔离：
    {AdamW, Adafactor} × {bf16 参数, fp32 参数} × {lr=2e-5, lr=1e-2}

判据：60 步后「平均位移 / bf16 ulp」。要能正常学习，该比值应 ≳ 1。
"""
import sys

import torch

sys.stdout.reconfigure(encoding="utf-8")

STEPS = 60
N = 4096


def run(opt_name, lr, dtype, rms_target=0.05, seed=0, steps=STEPS):
    torch.manual_seed(seed)
    # 构造 RMS 匹配真实权重量级的张量（真实 RMS 中位 ≈ 0.01~0.05）
    w = torch.randn(N, dtype=torch.float32)
    w = w / w.pow(2).mean().sqrt() * rms_target
    if dtype == torch.bfloat16:
        w = w.to(torch.bfloat16)
    p = torch.nn.Parameter(w.clone())
    opt = (torch.optim.AdamW([p], lr=lr, weight_decay=0.01) if opt_name == "AdamW"
           else torch.optim.Adafactor([p], lr=lr, weight_decay=0.01))

    start = p.detach().clone().to(torch.float64)
    for _ in range(steps):
        p.grad = torch.randn(p.shape, dtype=dtype).to(p.dtype)
        opt.step()
        opt.zero_grad()

    delta = (p.detach().to(torch.float64) - start).abs()
    ulp = 2.0 ** (torch.floor(torch.log2(start.abs().clamp(min=1e-12))) - 7)
    return {
        "changed": int((delta > 0).sum()),
        "mean_delta": float(delta.mean()),
        "ulp": float(ulp.median()),
        "ratio": float(delta.mean() / ulp.median()),
    }


def main():
    print("=" * 90)
    print(f"2×2×lr 对照：{STEPS} 步，RMS(θ)={0.05}（真实权重中位量级），N={N}")
    print("=" * 90)
    for lr in (2e-5, 1e-2):
        print(f"\n### lr = {lr:.0e}"
              + ("   ← 官方 sft_12hz.py 默认值（也是生产沿用的值）" if lr == 2e-5
                 else "   ← torch Adafactor 文档默认值"))
        print(f"{'优化器':<12} {'参数存储':>8} {'有变化元素':>12} {'平均|Δ|':>12} {'bf16 ulp':>11} {'Δ/ulp':>9} {'能学?':>7}")
        print("-" * 90)
        for opt_name in ("AdamW", "Adafactor"):
            for dtype, lab in ((torch.bfloat16, "bf16"), (torch.float32, "fp32")):
                r = run(opt_name, lr, dtype)
                can = "✅ 是" if r["ratio"] >= 1.0 else ("⚠️ 弱" if r["ratio"] >= 0.1 else "❌ 否")
                print(f"{opt_name:<12} {lab:>8} {r['changed']:>6}/{N:<5} "
                      f"{r['mean_delta']:>12.3e} {r['ulp']:>11.3e} "
                      f"{r['ratio']:>9.4f} {can:>7}")

    print("\n" + "=" * 90)
    print("判读")
    print("=" * 90)
    prod = run("Adafactor", 2e-5, torch.bfloat16)
    aw_bf = run("AdamW", 2e-5, torch.bfloat16)
    aw_fp = run("AdamW", 2e-5, torch.float32)
    ad_fp = run("Adafactor", 2e-5, torch.float32)
    print(f"  生产组合 Adafactor+bf16+2e-5 : Δ/ulp = {prod['ratio']:.5f}  ← 冻结在 14.5 的就是它")
    print(f"  仅换 fp32 参数               : Δ/ulp = {ad_fp['ratio']:.5f}"
          f"  （×{ad_fp['ratio']/prod['ratio']:.0f}）")
    print(f"  仅换 AdamW（仍 bf16）        : Δ/ulp = {aw_bf['ratio']:.5f}"
          f"  （×{aw_bf['ratio']/prod['ratio']:.0f}）")
    print(f"  两者都换（官方原版配置）      : Δ/ulp = {aw_fp['ratio']:.5f}"
          f"  （×{aw_fp['ratio']/prod['ratio']:.0f}）")
    print()
    print("  ⇒ 两个因素**各自都不够**：")
    print("     · bf16 存储：把 Δ 压掉约一个数量级（fp32 同 lr 下 Δ 大得多）")
    print("     · Adafactor 量纲：绝对步长又比 AdamW 小 RMS(θ) 倍")
    print("     · 二者叠加才让 Δ/ulp ≪ 1 ⇒ 权重原地不动")
    print()
    print("  ⇒ 也解释了为什么 QLoRA 路线没事：它是 **fp32 的 LoRA 层**，")
    print("     且 peft 的 LoRA 初始化尺度让 RMS 很小，避开了这条死路。")


if __name__ == "__main__":
    main()
