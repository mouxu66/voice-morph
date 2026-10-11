# -*- coding: utf-8 -*-
"""决定性实验：确认 talker 在训练 vs 推理时是否被 4-bit 量化（分布错位根因）。

背景：memory 2026-08-28 记「Qwen3TTSModel.from_pretrained 不会把 quantization_config
下传到 talker → 实际是 bf16」。若现在（transformers 5.16.1）行为变了，训练是 bf16、
推理是 4-bit，就会出现「峰值顶到 1.0 + 满幅饱和」的崩坏（= 8 月 B_old 指纹）。

本脚本对同一 adapter 跑两种加载方式，比对：
  模式 A: 带 quantization_config（我之前的推理方式）
  模式 B: 不带（纯 bf16，与训练时 dry_run 检查到的 "q_proj 量化4bit: False" 一致）
"""
import gc
import os
import sys
import time

import librosa
import numpy as np
import soundfile as sf
import torch
from bitsandbytes.nn import Linear4bit
from peft import PeftModel
from transformers import BitsAndBytesConfig

sys.stdout.reconfigure(encoding="utf-8")

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
OUT = "D:/变声/media/ft_work/ppt12/out_ab/quant_test"
os.makedirs(OUT, exist_ok=True)
SKIP = ["speaker_encoder", "codec_embedding", "text_embedding",
        "codec_head", "text_projection", "lm_head"]
SPK_ID = 3000

CASES = [
    ("aug_v3", "D:/变声/tts_trial/Qwen3-TTS/finetuning/output_qlora_aligned_v3/adapter-final",
     "D:/变声/media/voicebank/kangaroo/reference.wav",
     "您好，我是袋鼠骑士，很高兴为您服务。"),
    ("ppt_ga1", "D:/变声/media/ft_work/ppt12/out_ga1/adapter-final",
     "D:/变声/media/ft_work/ppt12/ref_24k.wav",
     "这一节我们讲三个概念，先看第一个。"),
]


def log(m):
    print(m, flush=True)


def probe_layers(model, tag):
    """报告 talker / code_predictor 的量化状态。"""
    out = {}
    for name, mod in model.named_modules():
        if name.endswith("q_proj") and ("talker" in name or "code_predictor" in name):
            kind = type(mod).__name__
            key = "talker" if "talker" in name and "code_predictor" not in name else "code_predictor"
            out.setdefault(key, set()).add(kind)
    log(f"  [{tag}] 层类型: " + "; ".join(
        f"{k}={sorted(v)}" for k, v in out.items()))
    return out


def run(use_bnb, adapter, ref, text, tag):
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

    kwargs = dict(torch_dtype=torch.bfloat16, attn_implementation="sdpa")
    if use_bnb:
        kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16, llm_int8_skip_modules=SKIP)
    qwen3tts = Qwen3TTSModel.from_pretrained(BASE, **kwargs)
    base = qwen3tts.model
    probe_layers(base, f"{tag}/base(加载后)")

    base.get_input_embeddings = (lambda self: self.talker.model.codec_embedding).__get__(base)
    base.set_input_embeddings = (lambda self, v: setattr(self.talker.model, "codec_embedding", v)).__get__(base)
    if adapter:
        model = PeftModel.from_pretrained(base, adapter)
    else:
        model = base
    model.eval()

    base.speaker_encoder = base.speaker_encoder.to(getattr(base, "dtype", torch.bfloat16))
    audio, sr = librosa.load(ref, sr=None, mono=True)
    tgt = base.speaker_encoder_sample_rate
    if int(sr) != int(tgt):
        audio = librosa.resample(y=audio.astype(np.float32), orig_sr=int(sr), target_sr=int(tgt))
    emb = base.extract_speaker_embedding(audio=audio.astype(np.float32), sr=int(tgt))
    emb = emb.unsqueeze(0) if emb.dim() == 1 else emb
    emb = emb.detach().to(torch.bfloat16)
    ce = base.talker.model.codec_embedding
    with torch.no_grad():
        ce.weight[SPK_ID] = emb[0].to(ce.weight.dtype).to(ce.weight.device)

    SPK = "spk"
    tc = base.config.talker_config
    tc.spk_id = {SPK: SPK_ID}
    tc.spk_is_dialect = {SPK: False}
    base.supported_speakers = tc.spk_id.keys()

    iid = qwen3tts._tokenize_texts([qwen3tts._build_assistant_text(text)])
    codes, _ = model.generate(
        input_ids=iid, languages=["Auto"], speakers=[SPK],
        non_streaming_mode=True, do_sample=True, temperature=0.8,
        top_p=0.95, repetition_penalty=1.1, max_new_tokens=400)
    c = codes[0]
    n = int(c.shape[0])
    wavs, fs = base.speech_tokenizer.decode([{"audio_codes": c}])
    arr = np.asarray(wavs[0], dtype=np.float32)
    p = os.path.join(OUT, f"{tag}.wav")
    sf.write(p, arr, int(fs))
    peak = float(np.max(np.abs(arr)))
    rms = float(np.sqrt(np.mean(arr ** 2) + 1e-12))
    zc = float(np.mean(np.diff(np.sign(arr)) != 0))
    log(f"  [{tag}] dur={len(arr)/fs:.1f}s frames={n} peak={peak:.2f} "
        f"rms={20*np.log10(rms+1e-12):.1f}dB zcr={zc:.3f} -> {p}")

    del model, base, qwen3tts
    gc.collect()
    torch.cuda.empty_cache()
    return {"tag": tag, "peak": round(peak, 3), "zcr": round(zc, 4),
            "dur": round(len(arr) / fs, 2), "path": p}


def main():
    results = []
    for name, adapter, ref, text in CASES:
        for use_bnb in (True, False):
            for use_adapter in (True, False):
                mode = f"{'4bit' if use_bnb else 'bf16'}{'+LoRA' if use_adapter else '+纯base'}"
                tag = f"{name}_{'4bit' if use_bnb else 'bf16'}{'_lora' if use_adapter else '_nolo'}"
                log(f"\n[{name}] {mode}")
                try:
                    r = run(use_bnb, adapter if use_adapter else None, ref, text, tag)
                    r["mode"] = mode
                    results.append(r)
                except Exception as e:
                    log(f"  [{tag}] 失败 {type(e).__name__}: {e}")
    log("\n=== 汇总 ===")
    log(f"{'组合':<24}{'峰值':>8}{'zcr':>9}{'时长':>8}  判读")
    for r in results:
        v = "崩坏(满幅)" if r["peak"] > 0.9 else ("正常" if r["peak"] < 0.6 else "偏高")
        log(f"{r['tag']:<24}{r['peak']:>8.2f}{r['zcr']:>9.3f}{r['dur']:>8.1f}  {v}")


if __name__ == "__main__":
    main()
