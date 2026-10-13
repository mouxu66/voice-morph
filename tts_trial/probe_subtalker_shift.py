# -*- coding: utf-8 -*-
"""只读验证：sub_talker（code_predictor）那条损失是否也有错位。

`sft_8gb.py` 的损失 = `outputs.loss + 0.3 * sub_talker_loss`，其中
`forward_sub_talker_finetune()`（modeling_qwen3_tts.py:1609）内部构造
    inputs_embeds = [talker_hidden, codec_0, codec_1, …, codec_14]   （16 个位置）
    logits[i-1]   = lm_head[i-1](hidden_states[:, i])                （i = 1..15）
    labels        = codec_ids[:, 1:]                                 （codec_1..codec_15）
并按 `ForCausalLMLoss` 算损失 —— 而后者**内部会再 shift 一次**。

问题：`logits[k]` 到底该预测 codec_{k+1} 还是 codec_{k+2}？
本脚本用"答案完全确定"的构造把两条路径都算出来，对参照说话。
纯 CPU、毫秒级。
"""
import sys

import torch
import torch.nn.functional as F

sys.stdout.reconfigure(encoding="utf-8")

from transformers.loss.loss_utils import ForCausalLMLoss

VOCAB = 32
G = 16      # num_code_groups


def main():
    torch.manual_seed(0)
    codec_ids = torch.randint(1, VOCAB, (1, G))

    # 模拟 forward_finetune 里的 logits 构造：
    #   logits[:, k] 由 hidden_states[:, k+1] 产出，而位置 k+1 刚消费过 codec_k。
    # 因此 logits[:, k] 的"自然正确答案"是 codec_{k+1}。
    n_logits = G - 1
    natural_next = codec_ids[:, 1:1 + n_logits]        # codec_1 .. codec_15

    lg = torch.full((1, n_logits, VOCAB), -30.0)
    for k in range(n_logits):
        lg[0, k, int(natural_next[0, k])] = 30.0       # 让每个位置都"答对"

    # 官方代码传的 labels：codec_ids[:, 1:]
    labels_official = codec_ids[:, 1:]
    loss_official = ForCausalLMLoss(logits=lg, labels=labels_official, vocab_size=VOCAB)

    # 参照：手工 next-token 交叉熵（logits[k] vs codec_{k+1}）
    ref = F.cross_entropy(lg.reshape(-1, VOCAB), natural_next.reshape(-1))

    # 若把 labels 整体前移一位（= 抵消内部那一次 shift）
    labels_alt = codec_ids[:, :n_logits]
    loss_alt = ForCausalLMLoss(logits=lg, labels=labels_alt, vocab_size=VOCAB)

    print(f"codec_ids          = {codec_ids.tolist()[0]}")
    print(f"logits 各位置的正确答案 = {natural_next.tolist()[0]}  (codec_1..codec_15)")
    print()
    print(f"参照（手工 next-token，应≈0）              = {ref.item():.6f}")
    print(f"官方 labels[:, 1:]（现役）                 = {loss_official.item():.6f}")
    print(f"若改成 labels[:, :15]（抵消内部 shift）     = {loss_alt.item():.6f}")
    print()
    if loss_official.item() < 1e-3:
        print("判定：官方写法与「预测 codec_{k+1}」一致 ⇒ sub_talker 这条路**没有**错位。")
    elif loss_alt.item() < 1e-3:
        print("判定：★ 官方写法与参照不符、改成前移一位才吻合")
        print("      ⇒ sub_talker 损失**也**被 ForCausalLMLoss 多 shift 了一次（错位一格）。")
    else:
        print("判定：两者都不吻合，需进一步排查（不要据此下结论）。")


if __name__ == "__main__":
    main()
