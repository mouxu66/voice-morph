# -*- coding: utf-8 -*-
"""决定性实验：用真 torch 跑 Adafactor，看 bf16 参数到底动没动。

为什么不用 numpy 模拟：探针 ⑤ 的 numpy 模拟给出「7~40% 元素有变化」，与解析
预期（单步位移 |Δ|≈3.7e-6 vs 半个 ulp 2.44e-4 ⇒ 应恒为 0）矛盾。**度量本身可疑时
先验度量**（项目铁律：守护测试必须做变异测试），所以这里换成 ground truth——
真 torch、真 Adafactor、真 bf16 参数对象，只造一个假梯度。

三组对照（同一份权重、同一个 lr、同一个 Adafactor）：
  A. 参数 bf16 + 梯度 bf16     ← 生产 sft_8gb.py 的实际情况
  B. 参数 fp32 + 梯度 fp32     ← 若生产改用 fp32 主权重
  C. 参数 bf16 + 梯度 fp32     ← 混合

全程 CPU，秒级，不加载大模型。
"""
import sys

import torch

sys.stdout.reconfigure(encoding="utf-8")

LR = 2e-5
STEPS = 60


def run(dtype, grad_dtype, seed=0, lr=LR, steps=STEPS):
    torch.manual_seed(seed)
    # 用一个真实量级的权重矩阵（§实测：中位 |w| ≈ 0.0113）
    w0 = (torch.randn(64, 64, dtype=torch.float32) * 0.05)
    if dtype == torch.bfloat16:
        w0 = w0.to(torch.bfloat16)
    p = torch.nn.Parameter(w0.clone())
    opt = torch.optim.Adafactor([p], lr=lr, weight_decay=0.01)

    start = p.detach().clone().to(torch.float64)
    for _ in range(steps):
        # torch 要求 grad.dtype == param.dtype，所以先按参数 dtype 造再转
        p.grad = torch.randn(p.shape, dtype=grad_dtype).to(p.dtype)
        opt.step()
        opt.zero_grad()

    end = p.detach().to(torch.float64)
    delta = (end - start).abs()
    ulp = 2.0 ** (torch.floor(torch.log2(start.abs().clamp(min=1e-12))) - 7)
    return {
        "n": p.numel(),
        "changed": int((delta > 0).sum()),
        "mean_abs_delta": float(delta.mean()),
        "mean_rel_delta": float((delta / start.abs().clamp(min=1e-12)).mean()),
        "median_ulp": float(ulp.median()),
        "param_dtype": p.dtype,
    }


def main():
    print("=" * 74)
    print(f"真 torch Adafactor，lr={LR:.0e}，{STEPS} 步（权重 64×64，σ=0.05）")
    print("=" * 74)
    print(f"{'组':<28} {'参数dtype':>10} {'有变化元素':>12} {'平均|Δ|':>12} {'中位ulp':>12} {'Δ/ulp':>9}")
    print("-" * 74)

    cases = [
        ("A 生产现状 bf16参数/bf16梯度", torch.bfloat16, torch.bfloat16),
        ("B 若用 fp32 参数", torch.float32, torch.float32),
        ("C bf16参数/fp32梯度", torch.bfloat16, torch.float32),
    ]
    rows = {}
    for label, pd_, gd_ in cases:
        r = run(pd_, gd_)
        rows[label] = r
        ratio = r["mean_abs_delta"] / r["median_ulp"]
        print(f"{label:<28} {str(r['param_dtype']):>10} "
              f"{r['changed']:>5}/{r['n']:<6} {r['mean_abs_delta']:>12.3e} "
              f"{r['median_ulp']:>12.3e} {ratio:>9.5f}")

    print("\n" + "=" * 74)
    print("判读")
    print("=" * 74)
    a = rows["A 生产现状 bf16参数/bf16梯度"]
    b = rows["B 若用 fp32 参数"]
    if a["changed"] == 0:
        print("★ A 组【一个元素都没动】⇒ 生产全参微调在 bf16 下**权重完全不更新**，")
        print("  这直接解释了 loss 冻结在 14.5（10 epoch 仅 −0.05，且那是 batch 噪声）。")
    else:
        print(f"A 组有 {a['changed']} 个元素变化（平均 |Δ|={a['mean_abs_delta']:.2e}），"
              f"相对 ulp 比 {a['mean_abs_delta']/a['median_ulp']:.5f}")
    print(f"B 组（fp32 参数）变化 {b['changed']}/{b['n']}，"
          f"平均 |Δ|={b['mean_abs_delta']:.3e} ⇒ 同样 lr 在 fp32 下正常累积。")
    print("\n（A 与 B 唯一的差别就是参数/梯度的存储精度，其余完全相同。）")


if __name__ == "__main__":
    main()
