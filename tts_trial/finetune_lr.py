# -*- coding: utf-8 -*-
"""微调学习率的【唯一事实来源】—— 按优化器分别给默认值，并守住 bf16 下限。

为什么需要这个模块（2026-10-13 定位，见 docs/犯错指南.md #170）：

  `tts_trial/sft_8gb.py` 是官方 `sft_12hz.py` 的 **8GB 显存适配版**，改了两处：
      ① AdamW → Adafactor（全参 AdamW 状态要 ~14GB，8GB 卡放不下）
      ② flash_attention_2 → sdpa
  **但它沿用了官方的 lr=2e-5，没有换算量纲。** 两者步长语义完全不同：

      AdamW      绝对步长 ≈ lr
      Adafactor  绝对步长 ≈ RMS(θ) · min(lr, 1/√t)      ← torch/optim/_adafactor.py:376

  RMS(θ) 实测在 0.001~0.2 量级 ⇒ Adafactor 的绝对步长比 AdamW 小 1~2 个数量级。
  torch 的 Adafactor 文档（_adafactor.py:271）自己写明：Noam Shazeer 原文用的是
  **常数 0.01** 作为 ρ_t 的上限 —— 即 lr 的"原生尺度"是 1e-2，不是 2e-5。

  再叠加第二个因素：全参权重以 **bf16 存储**（8GB 卡只能如此），而 bf16 只有 8 位尾数
  ⇒ 相对精度 2^-8 ≈ 3.9e-3。单步相对变化 `lr=2e-5` 比它还小两个数量级
  ⇒ **权重舍入后原地不动，loss 完全冻结**。

  实测（真 torch，1940 步 = 生产实际总步数，最有利的"梯度完全相关"假设）：
      Adafactor + bf16 + lr=2e-5 : 位移/ulp = 0.017，4096 个元素只有 31 个动过  ❌
      Adafactor + bf16 + lr=1e-2 : 位移/ulp = 12.6，几乎全部元素正常更新        ✅
      AdamW     + bf16 + lr=2e-5 : 位移/ulp = 8.84，同样正常                    ✅
  ⇒ 问题**不在 bf16 本身**，而在「AdamW 尺度的 lr 喂给 Adafactor」这一步错配。

  真实训练路径的受控 A/B（同数据/同脚本/同 epoch，唯一变量 = lr）：
      A 臂 lr=2e-5：1 epoch 均值 14.84 → 14.81（−0.03，等于没学）
      B 臂 lr=1e-2：1 epoch 均值 13.46 → 8.47（−4.99，健康下降）
  生产历史基线（10 epoch / 1940 步）也只从 14.56 动到 14.51 —— 与此完全吻合。
"""

#: Adafactor 的原生 lr 尺度（= Shazeer 原文 ρ_t 上限 0.01；torch 文档同值）。
#: bf16 全参微调用它，实测 1 epoch 内 loss 从 13.5 降到 8.5。
ADAFACTOR_LR = 1e-2

#: AdamW 的常用 lr（官方 sft_12hz.py 默认值）。**不要**把它喂给 Adafactor。
ADAMW_LR = 2e-5

#: bf16 参数能真正更新的最低 lr 经验下限。
#: 推导：单步位移 RMS(θ)·lr 要追上 bf16 的 ulp ≈ 2^(floor(log2 RMS)-7)，
#: 即需要 lr ≳ 2^-8 ≈ 3.9e-3；实测 5e-3 起才有过半元素开始更新（见上表）。
#: 低于此值的 lr 在 bf16 权重上会被舍入吃掉 —— 不报错、不警告，loss 只是不动。
MIN_LR_FOR_BF16 = 5e-3


def check_lr(lr: float, optimizer: str = "Adafactor") -> str | None:
    """返回一条人类可读的告警（没问题时返回 None）。

    只做诊断不做阻断：把 lr 设小是用户的自由，但必须让他看见"这样等于不训练"。
    """
    if optimizer.lower() != "adafactor":
        return None
    if lr < MIN_LR_FOR_BF16:
        return (
            f"lr={lr:g} 对 Adafactor 而言过小：它的绝对步长是 RMS(θ)·lr，"
            f"在 bf16 权重上会被舍入吃掉（需 ≳ {MIN_LR_FOR_BF16:g}，"
            f"原生尺度 {ADAFACTOR_LR:g}）。实测此值下 1940 步 loss 仅动 −0.05，"
            f"等于没训练。若确实想用 AdamW 尺度的 {ADAMW_LR:g}，请改用 AdamW。"
        )
    return None
