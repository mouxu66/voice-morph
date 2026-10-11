# -*- coding: utf-8 -*-
"""判决性测试：修复 prefill 对齐后，LoRA 生成是否可用。

三方对照（同一参考音、同一锚点、同一参数）：
  1. 纯 base           —— 基线
  2. LoRA（修复前）     —— 已知崩坏
  3. LoRA（修复后）     —— 待判决
判据四项：peak（>0.9 崩）+ 削波 + ASR 回译可懂度 + 时长合理性
"""
import gc
import json
import os
import sys

import librosa
import numpy as np
import soundfile as sf
import torch
from peft import PeftModel

sys.stdout.reconfigure(encoding="utf-8")

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
PPT = r"D:\变声\media\ft_work\ppt12"
REF = os.path.join(PPT, "ref_24k.wav")
OUT = os.path.join(PPT, "out_ab", "verdict")
os.makedirs(OUT, exist_ok=True)
SPK_ID, SPK = 3000, "spk"

ARMS = [
    ("base", None),
    ("lora_old", "D:/变声/media/ft_work/ppt12/out_ga1/adapter-final"),
    ("lora_pfix", "D:/变声/media/ft_work/ppt12/out_fixed/adapter-final"),
    ("lora_v2fix", "D:/变声/media/ft_work/ppt12/out_v2fix/adapter-final"),
]
TEXTS = [
    "这一节我们讲三个概念，先看第一个。",
    "把步骤拆开，一步一步做就不会乱。",
    "这道题的关键是先判断函数的单调性。",
]


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


def build(adapter=None):
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
    q = Qwen3TTSModel.from_pretrained(BASE, torch_dtype=torch.bfloat16, attn_implementation="sdpa")
    b = q.model.to("cuda")
    b.get_input_embeddings = (lambda self: self.talker.model.codec_embedding).__get__(b)
    b.set_input_embeddings = (lambda self, v: setattr(self.talker.model, "codec_embedding", v)).__get__(b)
    set_anchor(b, REF)
    m = PeftModel.from_pretrained(b, adapter) if adapter else b
    m.eval()
    tc = b.config.talker_config
    tc.spk_id, tc.spk_is_dialect = {SPK: SPK_ID}, {SPK: False}
    b.supported_speakers = tc.spk_id.keys()
    return q, b, m


from faster_whisper import WhisperModel
wm = WhisperModel("small", device="cuda", compute_type="float16")


def ov(a, b):
    A, B = set(a or ""), set(b or "")
    return len(A & B) / max(len(A | B), 1) if A and B else 0.0


rows = []
for tag, adapter in ARMS:
    if adapter and not os.path.exists(adapter):
        log(f"[{tag}] adapter 缺失，跳过")
        continue
    log(f"\n### {tag}")
    q, b, m = build(adapter)
    for i, t in enumerate(TEXTS, 1):
        try:
            tk = q._tokenize_texts([q._build_assistant_text(t)])
            tk = [x.to("cuda") if torch.is_tensor(x) else torch.tensor(x, device="cuda") for x in tk]
            codes, _ = m.generate(input_ids=tk, languages=["Auto"], speakers=[SPK],
                                  non_streaming_mode=True, do_sample=True, temperature=0.8,
                                  top_p=0.95, repetition_penalty=1.1, max_new_tokens=400)
            c = codes[0]
            wavs, fs = b.speech_tokenizer.decode([{"audio_codes": c}])
            arr = np.asarray(wavs[0], dtype=np.float32)
            p = os.path.join(OUT, f"{tag}_{i}.wav")
            sf.write(p, arr, int(fs))
            segs, _ = wm.transcribe(p, language="zh", vad_filter=False)
            back = "".join(s.text for s in segs)
            peak = float(np.max(np.abs(arr)))
            clip = float(np.mean(np.abs(arr) >= 0.999))
            zc = float(np.mean(np.diff(np.sign(arr)) != 0))
            o = ov(back, t)
            verdict = "★可用" if (peak < 0.9 and o > 0.5) else ("边缘" if o > 0.3 else "✗崩坏")
            log(f"  {i}) dur={len(arr)/fs:5.1f}s peak={peak:.2f} clip={100*clip:.2f}% "
                f"zcr={zc:.3f} 可懂度={o:.2f}  {verdict}")
            log(f"     期望: {t}")
            log(f"     回译: {back}")
            rows.append({"tag": tag, "i": i, "dur": round(len(arr) / fs, 2), "peak": round(peak, 3),
                         "clip_pct": round(100 * clip, 3), "zcr": round(zc, 4),
                         "overlap": round(o, 3), "verdict": verdict, "back": back, "text": t})
        except Exception as e:
            log(f"  {i}) 失败 {type(e).__name__}: {e}")
    del m, b, q
    gc.collect(); torch.cuda.empty_cache()

log("\n" + "=" * 78)
log(f"{'臂':<12}{'平均可懂度':>12}{'平均peak':>11}{'削波%':>9}  判读")
log("-" * 78)
for tag, _ in ARMS:
    rs = [r for r in rows if r["tag"] == tag]
    if not rs:
        continue
    o = np.mean([r["overlap"] for r in rs])
    pk = np.mean([r["peak"] for r in rs])
    cl = np.mean([r["clip_pct"] for r in rs])
    v = "★可用" if (pk < 0.9 and o > 0.5) else ("边缘" if o > 0.3 else "✗崩坏")
    log(f"{tag:<12}{o:>12.3f}{pk:>11.3f}{cl:>9.3f}  {v}")
log("=" * 78)

with open(os.path.join(OUT, "verdict.json"), "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)
