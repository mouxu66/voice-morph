# -*- coding: utf-8 -*-
"""判据：LoRA 到底学到没有？用 teacher-forcing loss 对比 base vs base+LoRA。

纯前向、不生成，完全绕开 generate() 的入参/采样问题。
判读：
  LoRA loss 明显 < base loss   -> adapter 学到了，问题在推理/生成路径
  LoRA loss ≈ base loss        -> adapter 没学到（训练无效）
"""
import gc
import json
import os
import sys

import numpy as np
import torch
from peft import PeftModel

sys.path.insert(0, r"D:\变声\tts_trial\Qwen3-TTS\finetuning")
sys.stdout.reconfigure(encoding="utf-8")

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
PPT = r"D:\变声\media\ft_work\ppt12"
REF = os.path.join(PPT, "ref_24k.wav")
SPK_ID = 3000
ADAPTERS = {
    "ppt_ga1(本次,修好)": "D:/变声/media/ft_work/ppt12/out_ga1/adapter-final",
    "ppt_ga8(本次,初版)": "D:/变声/media/ft_work/ppt12/out_ppt12/adapter-final",
}


def log(m):
    print(m, flush=True)


def set_anchor(base, ref):
    import librosa
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


def make():
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
    q = Qwen3TTSModel.from_pretrained(BASE, torch_dtype=torch.bfloat16,
                                      attn_implementation="sdpa")
    b = q.model.to("cuda")
    b.get_input_embeddings = (lambda self: self.talker.model.codec_embedding).__get__(b)
    b.set_input_embeddings = (lambda self, v: setattr(self.talker.model, "codec_embedding", v)).__get__(b)
    set_anchor(b, REF)
    return q, b


def tf_loss(model, q, samples, tag):
    from sft_12hz_qlora_aligned import build_prefill_embeds
    base = model.get_base_model() if hasattr(model, "get_base_model") else model
    dev = torch.device("cuda")
    sid = torch.tensor([[SPK_ID]], device=dev, dtype=torch.long)
    model.eval()
    ls = []
    with torch.no_grad():
        for s in samples:
            ac = torch.tensor(s["audio_codes"], dtype=torch.long, device=dev)[:200]
            iid = q._tokenize_texts([q._build_assistant_text(s["text"])])[0].to(dev)
            if iid.dim() == 1:
                iid = iid.unsqueeze(0)
            spk = base.talker.get_input_embeddings()(sid)
            prefill = build_prefill_embeds(base, iid, spk)
            c0 = ac[:, 0]
            emb = base.talker.get_input_embeddings()(c0.unsqueeze(0))
            ie = torch.cat([prefill, emb], dim=1)
            P, N = prefill.shape[1], c0.shape[0]
            lab = torch.full((1, P + N), -100, dtype=torch.long, device=dev)
            lab[0, P - 1] = c0[0]
            if N > 1:
                lab[0, P:P + N - 1] = c0[1:]
            lab[0, P + N - 1] = base.config.talker_config.codec_eos_token_id
            attn = torch.ones((1, P + N), dtype=torch.long, device=dev)
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
                out = base.talker(inputs_embeds=ie, attention_mask=attn, labels=lab)
            ls.append(float(out.loss))
    rng = float(np.log(3072))
    m = float(np.mean(ls))
    log(f"  {tag:<26} loss={m:7.4f}   距随机线 {m-rng:+.4f}   (n={len(ls)})")
    return m


def main():
    rows = [json.loads(l) for l in open(os.path.join(PPT, "train_with_codes.jsonl"),
                                        encoding="utf-8")]
    samples = rows[:8]
    log(f"随机线 ln(3072) = {np.log(3072):.4f}\n")
    log(f"{'模型':<26} {'loss':>9}")

    q, b = make()
    base_loss = tf_loss(b, q, samples, "纯 base (bf16)")
    del q, b
    gc.collect(); torch.cuda.empty_cache()

    for name, path in ADAPTERS.items():
        if not os.path.exists(path):
            log(f"  {name}: adapter 不存在")
            continue
        q, b = make()
        m = PeftModel.from_pretrained(b, path)
        lora_loss = tf_loss(m, q, samples, f"base+LoRA {name}")
        log(f"      -> 相对 base 下降 {base_loss - lora_loss:+.4f}"
            f"  {'★学到了' if base_loss - lora_loss > 0.5 else '✗ 几乎没学到'}")
        del m, q, b
        gc.collect(); torch.cuda.empty_cache()


if __name__ == "__main__":
    main()
