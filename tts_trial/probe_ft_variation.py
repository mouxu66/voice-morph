# -*- coding: utf-8 -*-
"""只读诊断 ⑨【变异测试】：把「梯度方向」这一假设推到最有利的一端。

诊断 ⑧ 用**每步独立随机**的梯度方向建模。但真实训练里，同一参数的梯度跨 batch
是**相关的**（这正是训练能收敛的原因）。随机方向的位移按 √t 增长，而完全相关的
方向按 t 线性增长 —— 后者对"权重能否动起来"是**最有利**的情形。

若在"完全相关"这个最有利假设下，生产配置（Adafactor + bf16 + lr=2e-5）**仍然冻住**，
那么结论就不依赖梯度相关性这个假设了。（项目铁律：守护/诊断结论必须做变异测试。）

同时用生产的**真实步数 1940**（10 epoch × 194 step）而非 60。
"""
import sys

import torch

sys.stdout.reconfigure(encoding="utf-8")

STEPS = 1940          # = 生产 sft_run2.log 的实际总步数（10 epoch × 194）
N = 4096


def run(opt_name, lr, dtype, corr, rms_target=0.05, seed=0, steps=STEPS):
    """corr=True：每步梯度方向完全相同（最有利）；False：每步独立（随机游走）。"""
    torch.manual_seed(seed)
    w = torch.randn(N, dtype=torch.float32)
    w = w / w.pow(2).mean().sqrt() * rms_target
    if dtype == torch.bfloat16:
        w = w.to(torch.bfloat16)
    p = torch.nn.Parameter(w.clone())
    opt = (torch.optim.AdamW([p], lr=lr, weight_decay=0.01) if opt_name == "AdamW"
           else torch.optim.Adafactor([p], lr=lr, weight_decay=0.01))

    fixed = torch.randn(N, dtype=torch.float32)
    fixed = fixed / fixed.pow(2).mean().sqrt()

    start = p.detach().clone().to(torch.float64)
    for _ in range(steps):
        g = fixed if corr else torch.randn(N)
        p.grad = g.to(dtype).to(p.dtype)
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
    print("=" * 96)
    print(f"最有利假设下的变异测试：{STEPS} 步（= 生产实际总步数），RMS(θ)=0.05")
    print("=" * 96)

    for corr, desc in ((True, "梯度方向完全相关（最有利，位移随 t 线性增长）"),
                       (False, "梯度方向独立随机（随机游走，位移随 √t 增长）")):
        print(f"\n### {desc}")
        print(f"{'优化器':<12} {'存储':>6} {'lr':>9} {'有变化元素':>12} {'平均|Δ|':>12} {'Δ/ulp':>10} {'能学?':>8}")
        print("-" * 96)
        for lr in (2e-5, 1e-2):
            for opt_name in ("AdamW", "Adafactor"):
                for dtype, lab in ((torch.bfloat16, "bf16"), (torch.float32, "fp32")):
                    r = run(opt_name, lr, dtype, corr)
                    can = ("✅ 是" if r["ratio"] >= 1.0
                           else "⚠️ 弱" if r["ratio"] >= 0.1 else "❌ 否")
                    star = "  ←生产" if (opt_name == "Adafactor" and lab == "bf16"
                                        and lr == 2e-5) else ""
                    print(f"{opt_name:<12} {lab:>6} {lr:>9.0e} {r['changed']:>6}/{N:<5} "
                          f"{r['mean_delta']:>12.3e} {r['ratio']:>10.5f} {can:>8}{star}")

    print("\n" + "=" * 96)
    print("判读")
    print("=" * 96)
    prod_corr = run("Adafactor", 2e-5, torch.bfloat16, True)
    prod_rand = run("Adafactor", 2e-5, torch.bfloat16, False)
    aw_corr = run("AdamW", 2e-5, torch.bfloat16, True)
    print(f"  生产配置，最有利（相关）: Δ/ulp = {prod_corr['ratio']:.5f}，"
          f"变化 {prod_corr['changed']}/{N}")
    print(f"  生产配置，随机方向      : Δ/ulp = {prod_rand['ratio']:.5f}，"
          f"变化 {prod_rand['changed']}/{N}")
    print(f"  官方配置(AdamW)，相关   : Δ/ulp = {aw_corr['ratio']:.5f}，"
          f"变化 {aw_corr['changed']}/{N}")
    print()
    if prod_corr["ratio"] < 0.1:
        print("  ★ 即使在**最有利**的完全相关假设下、跑满 1940 步，")
        print("    生产配置的位移仍远小于一个 bf16 ulp ⇒ **权重实质冻结**，")
        print("    结论不依赖梯度相关性假设。")
    else:
        print("  ⚠ 相关假设下生产配置能动了 ⇒ 结论需要收窄，不能说「完全冻结」。")


if __name__ == "__main__":
    main()
