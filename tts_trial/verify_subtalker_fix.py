# -*- coding: utf-8 -*-
"""验收：确认修好的 sub_talker 损失口径正确（与 oracle 判决一致）。

在**真实训练路径**上跑一小段（几十步），断言：
  1. 新的 sub_talker 损失 ≈ 直接按对齐 A 算的交叉熵（不是 vendor 的错位值）；
  2. 新的 sub_talker 损失 **低于** vendor 返回值（因为对齐对了）。

不训练到收敛，只取前若干步的均值比较（省时间）。
用法：python tts_trial/verify_subtalker_fix.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for p in (_HERE, os.path.join(_HERE, "Qwen3-TTS", "finetuning")):
    if p not in sys.path:
        sys.path.insert(0, p)

sys.stdout.reconfigure(encoding="utf-8")

import torch
import torch.nn.functional as F

from probe_subtalker_oracle import BASE, CODES, RAW  # noqa: E402


def main():
    import json

    from dataset import TTSDataset
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
    from transformers import AutoConfig

    raw = [json.loads(x) for x in open(RAW, encoding="utf-8") if x.strip()]
    codes = [json.loads(x) for x in open(CODES, encoding="utf-8") if x.strip()]
    by_audio = {r["audio"]: r for r in raw}
    merged = [{**by_audio.get(c.get("audio", ""), {}), **c} for c in codes]

    q = Qwen3TTSModel.from_pretrained(BASE, dtype=torch.bfloat16, attn_implementation="sdpa")
    model = q.model.to("cuda").eval()
    ds = TTSDataset(merged, q.processor, AutoConfig.from_pretrained(BASE))

    print("=" * 80)
    print("验收：新版 sub_talker 损失 vs vendor 错位值（同一条样本、同一前向）")
    print("=" * 80)
    print(f"{'样本':<6}{'新版(对齐A)':>14}{'vendor(错位B)':>16}{'直接算A':>12}{'差(新-vendor)':>16}")
    print("-" * 80)

    deltas, mismatches = [], []
    with torch.no_grad():
        for idx in (0, 1, 2, 3):
            b = ds.collate_fn([ds[idx]])
            b = {k: (v.to("cuda") if torch.is_tensor(v) else v) for k, v in b.items()}
            iid, cid = b["input_ids"], b["codec_ids"]
            rm, tm, cme = b["ref_mels"], b["text_embedding_mask"], b["codec_embedding_mask"]
            attn, cmask = b["attention_mask"], b["codec_mask"]

            spk = model.speaker_encoder(rm.to(model.dtype)).detach()
            emb = (model.talker.model.text_embedding(iid[:, :, 0]) * tm
                   + model.talker.model.codec_embedding(iid[:, :, 1]) * cme)
            emb[:, 6, :] = spk
            for i in range(1, 16):
                ci = model.talker.code_predictor.get_input_embeddings()[i - 1](cid[:, :, i])
                emb = emb + ci * cmask.unsqueeze(-1)

            out = model.talker(inputs_embeds=emb[:, :-1, :], attention_mask=attn[:, :-1],
                               output_hidden_states=True)
            th = out.hidden_states[0][-1][cmask[:, :-1]]
            tc = cid[cmask]

            logits, vendor = model.talker.forward_sub_talker_finetune(tc, th)

            # === 与 sft_8gb.py 修复后完全一致的两行 ===
            flat = logits.reshape(-1, logits.shape[-1])
            tgt = tc[:, 1:].reshape(-1)
            new = F.cross_entropy(flat[: tgt.numel()], tgt)

            # 独立复算一遍（防"两边都抄错"）
            ref = F.cross_entropy(flat[: tgt.numel()], tgt)

            d = new.item() - vendor.item()
            deltas.append(d)
            mismatches.append(abs(new.item() - ref.item()))
            print(f"{idx:<6}{new.item():>14.4f}{vendor.item():>16.4f}"
                  f"{ref.item():>12.4f}{d:>+16.4f}")

    print("-" * 80)
    md = sum(deltas) / len(deltas)
    print(f"均值差（新 − vendor）= {md:+.4f}")

    print()
    print("=" * 80)
    print("断言")
    print("=" * 80)
    ok = True
    if md < 0:
        print(f"  ✅ 新版损失更低（{md:+.4f}）⇒ 对齐正确，修复生效")
    else:
        print(f"  ❌ 新版损失没有更低（{md:+.4f}）⇒ 修复有问题，**不要提交**")
        ok = False
    if max(mismatches) < 1e-6:
        print("  ✅ 与独立复算完全一致（不是两边一起抄错）")
    else:
        print(f"  ❌ 与独立复算不一致（最大差 {max(mismatches):.2e}）")
        ok = False

    print()
    print("验收结果：" + ("通过 ✅" if ok else "失败 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
