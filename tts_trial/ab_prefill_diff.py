# -*- coding: utf-8 -*-
"""根因验证：训练用的 build_prefill_embeds vs 推理 generate() 真正构造的 prefill。

若两者形状/数值不一致 -> 训练/推理分布错位确认（LoRA 学到的修正套不上推理）
若一致 -> 错位假设被否，问题在别处
"""
import os
import sys

import librosa
import numpy as np
import torch

sys.path.insert(0, r"D:\变声\tts_trial\Qwen3-TTS\finetuning")
sys.stdout.reconfigure(encoding="utf-8")

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
PPT = r"D:\变声\media\ft_work\ppt12"
REF = os.path.join(PPT, "ref_24k.wav")
SPK_ID = 3000
SPK = "spk"
TEXT = "这一节我们讲三个概念，先看第一个。"


def log(m):
    print(m, flush=True)


def set_anchor(base, ref):
    base.speaker_encoder = base.speaker_encoder.to(getattr(base, "dtype", torch.bfloat16))
    a, sr = librosa.load(ref, sr=None, mono=True)
    tgt = base.speaker_encoder_sample_rate
    if int(sr) != int(tgt):
        a = librosa.resample(y=a.astype(np.float32), orig_sr=int(sr), target_sr=int(tgt))
    e = base.extract_speaker_embedding(audio=a.astype(np.float32), sr=int(tgt))
    e = e.unsqueeze(0) if e.dim() == 1 else e
    ce = base.talker.model.codec_embedding
    with torch.no_grad():
        ce.weight[SPK_ID] = e.detach().to(torch.bfloat16)[0].to(ce.weight.dtype).to(ce.weight.device)
    return e.detach().to(torch.bfloat16)


def main():
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
    from sft_12hz_qlora_aligned import build_prefill_embeds

    q = Qwen3TTSModel.from_pretrained(BASE, torch_dtype=torch.bfloat16, attn_implementation="sdpa")
    b = q.model.to("cuda")
    b.get_input_embeddings = (lambda self: self.talker.model.codec_embedding).__get__(b)
    b.set_input_embeddings = (lambda self, v: setattr(self.talker.model, "codec_embedding", v)).__get__(b)
    spk_emb = set_anchor(b, REF)

    tc = b.config.talker_config
    tc.spk_id = {SPK: SPK_ID}
    tc.spk_is_dialect = {SPK: False}
    b.supported_speakers = tc.spk_id.keys()

    # --- 训练侧 prefill ---
    tk = q._tokenize_texts([q._build_assistant_text(TEXT)])
    iid2d = tk[0] if torch.is_tensor(tk[0]) else torch.tensor(tk[0], dtype=torch.long)
    if iid2d.dim() == 1:
        iid2d = iid2d.unsqueeze(0)
    iid2d = iid2d.to("cuda")
    train_prefill = build_prefill_embeds(b, iid2d, spk_emb)
    log(f"[train] build_prefill_embeds   shape={tuple(train_prefill.shape)}")

    # --- 推理侧 prefill（hook talker 第一次 forward 的 inputs_embeds） ---
    captured = {}

    def hook(mod, args, kwargs):
        ie = kwargs.get("inputs_embeds", None)
        if ie is None and args:
            ie = args[0]
        if ie is not None and "emb" not in captured:
            captured["emb"] = ie.detach().clone()

    h = b.talker.register_forward_pre_hook(hook, with_kwargs=True)
    try:
        tk2 = q._tokenize_texts([q._build_assistant_text(TEXT)])
        tk2 = [x.to("cuda") if torch.is_tensor(x) else torch.tensor(x, dtype=torch.long, device="cuda")
               for x in tk2]
        codes, _ = b.generate(input_ids=tk2, languages=["Auto"], speakers=[SPK],
                              non_streaming_mode=True, do_sample=False, max_new_tokens=8)
    finally:
        h.remove()

    if "emb" not in captured:
        log("[infer] 未捕获到 talker 的 inputs_embeds")
        return
    infer_prefill = captured["emb"]
    log(f"[infer] generate() 首帧 prefill  shape={tuple(infer_prefill.shape)}")

    # --- 比对 ---
    P_t = train_prefill.shape[1]
    P_i = infer_prefill.shape[1]
    log(f"\n[对比] 训练 P={P_t}  推理 P={P_i}  {'★形状一致' if P_t == P_i else '✗ 形状不同'}")
    n = min(P_t, P_i)
    a1 = train_prefill[0, :n].float()
    a2 = infer_prefill[0, :n].float()
    diff = (a1 - a2).abs()
    cos = torch.nn.functional.cosine_similarity(a1.flatten(), a2.flatten(), dim=0).item()
    log(f"[对比] 前 {n} 行: 平均绝对差={diff.mean().item():.5f}  最大={diff.max().item():.5f}")
    log(f"[对比] 余弦相似度={cos:.5f}  {'★完全一致' if cos > 0.9999 else '✗ 不一致'}")
    # 逐行差异
    if cos <= 0.9999:
        rd = diff.mean(dim=1)
        log("\n[逐行] 平均绝对差（行号: 差值）:")
        for i in range(n):
            flag = "  <-- 差异大" if rd[i].item() > rd.mean().item() * 2 else ""
            log(f"   {i:3d}: {rd[i].item():.5f}{flag}")


if __name__ == "__main__":
    main()
