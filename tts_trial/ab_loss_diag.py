# -*- coding: utf-8 -*-
"""决定性诊断：adapter 到底学到没有 + merge 是否能修好生成。

诊断一（teacher forcing）：用训练脚本自己的 build_prefill_embeds 前向，
  在训练样本上算 loss。若 LoRA 的 loss 明显低于 base -> adapter 学到了；
  若相近 -> adapter 没学到（训练无效）。
诊断二（merge）：bf16 base + adapter + merge_and_unload 后生成，看是否修复。
"""
import gc
import json
import os
import sys

import numpy as np
import soundfile as sf
import torch
from peft import PeftModel

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, r"D:\变声\tts_trial\Qwen3-TTS\finetuning")

# 4bit 分支 skip 名单与训练同源（bf16 对照分支不传 quantization_config）。
from qlora_quant_config import build_bnb_config

sys.stdout.reconfigure(encoding="utf-8")

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
PPT = r"D:\变声\media\ft_work\ppt12"
OUT = os.path.join(PPT, "out_ab", "diag")
os.makedirs(OUT, exist_ok=True)
SPK_ID = 3000
TEXT = "这一节我们讲三个概念，先看第一个。"


def log(m):
    print(m, flush=True)


def set_anchor(base, ref, dev="cuda"):
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
    return float(e.norm())


def teacher_forcing_loss(model, qwen3tts, samples, tag):
    """用训练脚本的 build_prefill_embeds 算 loss（teacher forcing，不生成）。"""
    from sft_12hz_qlora_aligned import build_prefill_embeds
    base = model.get_base_model() if hasattr(model, "get_base_model") else model
    dev = torch.device("cuda")
    sid = torch.tensor([[SPK_ID]], device=dev, dtype=torch.long)
    losses = []
    model.eval()
    with torch.no_grad():
        for s in samples:
            ac = torch.tensor(s["audio_codes"], dtype=torch.long, device=dev)
            if ac.shape[0] > 200:
                ac = ac[:200]
            iid = qwen3tts._tokenize_texts([qwen3tts._build_assistant_text(s["text"])])[0].to(dev)
            spk = base.talker.get_input_embeddings()(sid)
            prefill = build_prefill_embeds(base, iid.unsqueeze(0) if iid.dim() == 1 else iid, spk)
            c0 = ac[:, 0]
            emb = base.talker.get_input_embeddings()(c0.unsqueeze(0))
            input_embeds = torch.cat([prefill, emb], dim=1)
            P, N = prefill.shape[1], c0.shape[0]
            labels = torch.full((1, P + N), -100, dtype=torch.long, device=dev)
            labels[0, P - 1] = c0[0]
            if N > 1:
                labels[0, P:P + N - 1] = c0[1:]
            labels[0, P + N - 1] = base.config.talker_config.codec_eos_token_id
            attn = torch.ones((1, P + N), dtype=torch.long, device=dev)
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16):
                out = base.talker(inputs_embeds=input_embeds, attention_mask=attn, labels=labels)
            losses.append(float(out.loss))
    m = float(np.mean(losses))
    log(f"  [{tag}] teacher-forcing loss = {m:.4f}  (n={len(losses)}, "
        f"随机线 ln3072={np.log(3072):.4f})")
    return m


def gen(model, qwen3tts, tag, spk="spk"):
    base = model.get_base_model() if hasattr(model, "get_base_model") else model
    dev = next(base.parameters()).device
    tc = base.config.talker_config
    tc.spk_id = {spk: SPK_ID}
    tc.spk_is_dialect = {spk: False}
    base.supported_speakers = tc.spk_id.keys()
    # generate() 的 input_ids 签名是 Optional[list[Tensor]]，逐条按【2D [1,T]】处理：
    # 内部 enumerate 取出后直接 input_id[:, :3]，传 1D 会报维度错。
    # 故直接用 _tokenize_texts 的整条输出（list[Tensor[1,T]]），只搬到设备，不取 [0]。
    tk = qwen3tts._tokenize_texts([qwen3tts._build_assistant_text(TEXT)])
    iid_list = [(x.to(dev) if torch.is_tensor(x) else torch.tensor(x, dtype=torch.long, device=dev))
                for x in tk]
    codes, _ = model.generate(input_ids=iid_list, languages=["Auto"], speakers=[spk],
                              non_streaming_mode=True, do_sample=True, temperature=0.8,
                              top_p=0.95, repetition_penalty=1.1, max_new_tokens=400)
    c = codes[0]
    wavs, fs = base.speech_tokenizer.decode([{"audio_codes": c}])
    arr = np.asarray(wavs[0], dtype=np.float32)
    p = os.path.join(OUT, f"{tag}.wav")
    sf.write(p, arr, int(fs))
    peak = float(np.max(np.abs(arr)))
    zc = float(np.mean(np.diff(np.sign(arr)) != 0))
    log(f"  [{tag}] dur={len(arr)/fs:.1f}s frames={int(c.shape[0])} peak={peak:.2f} zcr={zc:.3f}")
    return {"tag": tag, "peak": round(peak, 3), "zcr": round(zc, 4),
            "dur": round(len(arr) / fs, 2), "path": p}


def load(use_bnb, adapter=None, merge=False):
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
    kw = dict(torch_dtype=torch.bfloat16, attn_implementation="sdpa")
    if use_bnb:
        kw["quantization_config"] = build_bnb_config()
    q = Qwen3TTSModel.from_pretrained(BASE, **kw)
    b = q.model
    b = b.to("cuda")          # 显式搬到 GPU（from_pretrained 的 device_map 未必下传）
    b.get_input_embeddings = (lambda self: self.talker.model.codec_embedding).__get__(b)
    b.set_input_embeddings = (lambda self, v: setattr(self.talker.model, "codec_embedding", v)).__get__(b)
    n = set_anchor(b, os.path.join(PPT, "ref_24k.wav"))
    log(f"  [anchor] norm={n:.2f}  device={next(b.parameters()).device}")
    if not adapter:
        return b, q, b
    m = PeftModel.from_pretrained(b, adapter)
    if merge:
        m = m.merge_and_unload()
        log("  [merge] merge_and_unload 完成")
    # 合并后锚点可能被覆盖，重设一次
    set_anchor(b if not merge else m, os.path.join(PPT, "ref_24k.wav"))
    return m, q, (b if not merge else m)


def main():
    rows = [json.loads(l) for l in open(os.path.join(PPT, "train_with_codes.jsonl"), encoding="utf-8")]
    samples = rows[:6]
    ADAPTER = "D:/变声/media/ft_work/ppt12/out_ga1/adapter-final"

    log("### 1) 纯 base（bf16）")
    m, q, tgt = load(False, None)
    teacher_forcing_loss(tgt, q, samples, "base_bf16")
    gen(m, q, "base_bf16")
    del m, q
    gc.collect(); torch.cuda.empty_cache()

    log("\n### 2) base + LoRA（bf16，不合并）")
    m, q, tgt = load(False, ADAPTER, merge=False)
    teacher_forcing_loss(tgt, q, samples, "lora_bf16")
    gen(m, q, "lora_bf16")
    del m, q
    gc.collect(); torch.cuda.empty_cache()

    log("\n### 3) base + LoRA（bf16，merge_and_unload）")
    try:
        m, q, tgt = load(False, ADAPTER, merge=True)
        teacher_forcing_loss(tgt, q, samples, "lora_merged")
        gen(m, q, "lora_merged")
        del m, q
        gc.collect(); torch.cuda.empty_cache()
    except Exception as e:
        log(f"  merge 路径失败: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
