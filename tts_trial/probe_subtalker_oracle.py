# -*- coding: utf-8 -*-
"""决定性验证：sub_talker 的 label 到底该怎么对齐（用模型自己当裁判）。

背景
----
`sft_8gb.py` 的损失 = `outputs.loss + 0.3 * sub_talker_loss`。
其中 sub_talker 由**第三方包**的 `forward_sub_talker_finetune()` 算出
（`qwen_tts/core/models/modeling_qwen3_tts.py:1609`）：

    inputs_embeds = [talker_hidden, codec_0, codec_1, …, codec_14]   （16 个位置）
    logits[k]     = lm_head[k](hidden_states[:, k+1])                （k = 0..14）
    labels        = codec_ids[:, 1:]        ← 传给 self.loss_function
    而 loss_function = ForCausalLMLoss，**内部又会 shift 一次**（labels[..., 1:]）

若 logits[k] 的"自然正确答案"是 codec_{k+1}，那么内部 shift 后比的是
codec_{k+2} ⇒ **错位一格**（与我 10-13 对主 talker 的发现同族）。

**但"自然答案"不该靠推理断定** —— 预训练模型本身就是裁判：
把 talker 换成真模型跑一次前向，取真实 logits，分别按
    · 对齐 A：target = codec_{k+1}（= codec_ids[:, 1:]）
    · 对齐 B：target = codec_{k+2}（= codec_ids[:, 2:]，即现行双重 shift 的效果）
算直接交叉熵。**训练好的模型在正确对齐上 loss 应显著更低。**

用法（venv312，GPU）：
    python tts_trial/probe_subtalker_oracle.py
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for p in (_HERE, os.path.join(_HERE, "Qwen3-TTS", "finetuning")):
    if p not in sys.path:
        sys.path.insert(0, p)

sys.stdout.reconfigure(encoding="utf-8")

import torch
import torch.nn.functional as F

BASE = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
CODES = r"D:/变声/media/ft/pengyuyan/train_codes_short.jsonl"
RAW = r"D:/变声/media/ft/pengyuyan/train_raw_short.jsonl"


def main():
    from dataset import TTSDataset
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
    from transformers import AutoConfig

    raw = [json.loads(x) for x in open(RAW, encoding="utf-8") if x.strip()]
    codes = [json.loads(x) for x in open(CODES, encoding="utf-8") if x.strip()]
    by_audio = {r["audio"]: r for r in raw}
    merged = [{**by_audio.get(c.get("audio", ""), {}), **c} for c in codes]

    print(f"[load] {BASE}")
    q = Qwen3TTSModel.from_pretrained(BASE, dtype=torch.bfloat16, attn_implementation="sdpa")
    model = q.model.to("cuda").eval()
    cfg = AutoConfig.from_pretrained(BASE)

    ds = TTSDataset(merged, q.processor, cfg)

    n_vocab = model.talker.code_predictor.config.vocab_size
    print(f"[cfg] code_predictor vocab = {n_vocab}")

    rows = []
    with torch.no_grad():
        for idx in (0, 1, 2):
            b = ds.collate_fn([ds[idx]])
            b = {k: (v.to("cuda") if torch.is_tensor(v) else v) for k, v in b.items()}
            input_ids, codec_ids = b["input_ids"], b["codec_ids"]
            ref_mels, tmask, cmask_e = b["ref_mels"], b["text_embedding_mask"], b["codec_embedding_mask"]
            attn, cmask = b["attention_mask"], b["codec_mask"]

            spk = model.speaker_encoder(ref_mels.to(model.dtype)).detach()
            ite = model.talker.model.text_embedding(input_ids[:, :, 0]) * tmask
            ice = model.talker.model.codec_embedding(input_ids[:, :, 1]) * cmask_e
            ice[:, 6, :] = spk
            emb = ite + ice
            for i in range(1, 16):
                ci = model.talker.code_predictor.get_input_embeddings()[i - 1](codec_ids[:, :, i])
                emb = emb + ci * cmask.unsqueeze(-1)

            out = model.talker(inputs_embeds=emb[:, :-1, :], attention_mask=attn[:, :-1],
                               output_hidden_states=True)
            hidden = out.hidden_states[0][-1]
            th = hidden[cmask[:, :-1]]
            tc = codec_ids[cmask]

            # 真模型算 sub_talker logits（同时拿到 vendor 内部的 loss 作为对照）
            logits, vendor_loss = model.talker.forward_sub_talker_finetune(tc, th)
            V = logits.shape[-1]
            lg = logits.reshape(-1, V)

            # 对齐 A：logits[k] ← codec_{k+1}
            tgtA = tc[:, 1:].reshape(-1)
            # 对齐 B：logits[k] ← codec_{k+2}（现行双重 shift 的实际效果）
            tgtB = tc[:, 2:].reshape(-1)
            # 对齐 C：logits[k] ← codec_k（同位，若内部不 shift 才对）
            tgtC = tc[:, :-1].reshape(-1)

            lossA = F.cross_entropy(lg[: tgtA.numel()], tgtA)
            lossB = F.cross_entropy(lg[: tgtB.numel()], tgtB)
            lossC = F.cross_entropy(lg[: tgtC.numel()], tgtC)
            rows.append((idx, tc.shape[0], lossA.item(), lossB.item(), lossC.item(),
                         vendor_loss.item()))

    print()
    print("=" * 84)
    print("真模型上的 sub_talker 交叉熵（越低 = 对齐越正确）")
    print("=" * 84)
    print(f"{'样本':<6}{'码帧':>6}{'A: codec_{k+1}':>16}{'B: codec_{k+2}':>16}"
          f"{'C: codec_k':>14}{'vendor 内部':>14}")
    print("-" * 84)
    for idx, n, a, bb, c, vl in rows:
        print(f"{idx:<6}{n:>6}{a:>16.4f}{bb:>16.4f}{c:>14.4f}{vl:>14.4f}")

    mA = sum(r[2] for r in rows) / len(rows)
    mB = sum(r[3] for r in rows) / len(rows)
    mC = sum(r[4] for r in rows) / len(rows)
    mV = sum(r[5] for r in rows) / len(rows)
    print("-" * 84)
    print(f"{'均值':<6}{'':>6}{mA:>16.4f}{mB:>16.4f}{mC:>14.4f}{mV:>14.4f}")

    import math
    print(f"\n[参照] 随机线 ln({n_vocab}) = {math.log(n_vocab):.4f}")

    print()
    print("=" * 84)
    print("判决")
    print("=" * 84)
    best = min((mA, "A: codec_{k+1}"), (mB, "B: codec_{k+2}"), (mC, "C: codec_k"))
    print(f"  最低 = {best[1]}  ({best[0]:.4f})")
    print(f"  vendor 内部 loss = {mV:.4f}")
    if abs(mV - mB) < 0.05:
        print("  ⇒ vendor 内部的 loss **等于对齐 B** ⇒ 确认它多 shift 了一次（错位一格）")
    if best[1].startswith("A"):
        print("  ⇒ 正确对齐是 A（codec_{k+1}）⇒ 修法：直接用 codec_ids[:, 1:] 算直接交叉熵，")
        print("     不要再喂给会内部 shift 的 ForCausalLMLoss。")
    elif best[1].startswith("B"):
        print("  ⇒ 模型本身就更匹配 B ⇒ **现行写法是对的**，不该改。")
    else:
        print("  ⇒ 意外：最低是对齐 C，需重新分析（先别改代码）。")


if __name__ == "__main__":
    main()
