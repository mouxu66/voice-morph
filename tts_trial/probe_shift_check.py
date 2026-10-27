# -*- coding: utf-8 -*-
"""只读验证：sft_8gb.py 的 labels 对齐是否「双重 shift」（教成预测 j+2）。

背景：`transformers` 5.x 的 `ForCausalLMLoss` **内部**会做一次
`labels[..., 1:]`（loss_utils.py:61-64）。而 sft_8gb.py 传的是
`codec_0_labels[:, 1:]` —— 已经手移过一次。若二者叠加，模型被教成预测 **j+2**。
（同一结论 2026-10-12 已在 QLoRA 路线上确认过，见 docs/交接-AI必读.md §9.7 第 160 条 与
 官方 issue #278/#371；此处针对**生产链路**复验。）

做法：构造一个"正确答案已知"的序列，用真 `ForCausalLMLoss` 算两条路径的损失，
看哪条与"预测 j+1"完全一致。纯 CPU、毫秒级。
"""
import sys

import torch
import torch.nn.functional as F

sys.stdout.reconfigure(encoding="utf-8")

from transformers.loss.loss_utils import ForCausalLMLoss

VOCAB = 32
T = 10


def build_case():
    """造一个 T 长的目标序列；logits[k] 对第 k 个位置的分布是"完全确定"的。"""
    labels = torch.randint(1, VOCAB, (1, T))
    return labels


def logits_for(target_next: torch.Tensor):
    """构造 logits，使 logits[k] 在 target_next[k] 上取极大值（其余为 -inf 等价）。"""
    # shape [1, T-1, VOCAB]：与 inputs_embeds[:, :-1] 对齐
    n = target_next.shape[1]
    lg = torch.full((1, n, VOCAB), -30.0)
    for k in range(n):
        lg[0, k, int(target_next[0, k])] = 30.0
    return lg


def main():
    labels = build_case()
    print(f"目标标签序列 labels = {labels.tolist()[0]}")

    # logits 的每一行 k 对应「位置 k」；正确 next-token 目标是 labels[k+1]
    correct_next = labels[:, 1:]          # [1, T-1]
    lg = logits_for(correct_next)         # 让 logits[k] 恰好答对 labels[k+1]

    # --- 路径 A：sft_8gb.py 现状 —— 传已经手移过的 labels[:, 1:] ---
    loss_prod = ForCausalLMLoss(logits=lg, labels=labels[:, 1:], vocab_size=VOCAB)

    # --- 路径 B：同位 labels[:, :-1]，交给内部唯一一次 shift ---
    loss_fix = ForCausalLMLoss(logits=lg, labels=labels[:, :-1], vocab_size=VOCAB)

    # --- 参照：手动构造"预测 j+1"的交叉熵，应≈0 ---
    ref = F.cross_entropy(lg.reshape(-1, VOCAB), correct_next.reshape(-1))

    print(f"\n参照（手工 next-token 交叉熵，应≈0）        = {ref.item():.6f}")
    print(f"路径 B（labels[:, :-1]，同位 + 内部单次 shift）= {loss_fix.item():.6f}")
    print(f"路径 A（labels[:, 1:]，生产现状）            = {loss_prod.item():.6f}")

    print("\n" + "=" * 66)
    if loss_fix.item() < 1e-3 and loss_prod.item() > 1.0:
        print("★ 判定：路径 B 完全吻合「预测 j+1」；路径 A（生产现状）显著偏离")
        print("  ⇒ sft_8gb.py 确实是**双重 shift**，模型被教成预测 j+2。")
        print("  修法：labels=codec_0_labels[:, 1:]  →  codec_0_labels[:, :-1]")
    elif loss_prod.item() < 1e-3:
        print("判定：生产路径就是对的（与参照一致）⇒ 无双重 shift，不要改。")
    else:
        print("判定：两者都不吻合参照，需进一步排查（不要据此下结论）。")
    print("=" * 66)


if __name__ == "__main__":
    main()
