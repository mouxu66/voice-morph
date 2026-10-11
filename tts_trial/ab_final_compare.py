# -*- coding: utf-8 -*-
"""最终裁决：纯 base vs base+LoRA，同一输入同一参数，看谁生成得像样。

关键：用 set_anchor + speakers=[SPK] 的**锚点路径**（训练对齐的那条），
但 iid 必须保持 2D（generate 内部按 input_id[:, :3] 取 role）。
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
OUT = os.path.join(PPT, "out_ab", "final")
os.makedirs(OUT, exist_ok=True)
SPK_ID = 3000
SPK = "spk"
ADAPTER = "D:/变声/media/ft_work/ppt12/out_ga1/adapter-final"


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
    q = Qwen3TTSModel.from_pretrained(BASE, torch_dtype=torch.bfloat16,
                                      attn_implementation="sdpa")
    b = q.model.to("cuda")
    b.get_input_embeddings = (lambda self: self.talker.model.codec_embedding).__get__(b)
    b.set_input_embeddings = (lambda self, v: setattr(self.talker.model, "codec_embedding", v)).__get__(b)
    set_anchor(b, REF)
    if adapter:
        m = PeftModel.from_pretrained(b, adapter)
        m.eval()
    else:
        m = b
    tc = b.config.talker_config
    tc.spk_id = {SPK: SPK_ID}
    tc.spk_is_dialect = {SPK: False}
    b.supported_speakers = tc.spk_id.keys()
    return q, b, m


TEXTS = [
    "这一节我们讲三个概念，先看第一个。",
    "把步骤拆开，一步一步做就不会乱。",
    "这道题的关键是先判断函数的单调性。",
]

from faster_whisper import WhisperModel
wm = WhisperModel("small", device="cuda", compute_type="float16")

rows = []
for tag, adapter in (("base", None), ("lora", ADAPTER)):
    log(f"\n### {tag}")
    q, b, m = build(adapter)
    for i, t in enumerate(TEXTS, 1):
        try:
            # generate() 的 input_ids 期望 **list[Tensor]**（内部 enumerate 逐条处理），
            # 传 2D tensor 会被按行迭代成 1D -> input_id[:, :3] 索引报错。
            tk = q._tokenize_texts([q._build_assistant_text(t)])
            iid_list = [x.to("cuda") for x in tk] if isinstance(tk, (list, tuple)) else [tk.to("cuda")]
            log(f"  [dbg] n={len(iid_list)} shape={tuple(iid_list[0].shape)}")
            codes, _ = m.generate(input_ids=iid_list, languages=["Auto"], speakers=[SPK],
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
            zc = float(np.mean(np.diff(np.sign(arr)) != 0))
            cu = int(c.unique().numel())
            log(f"  {i}) dur={len(arr)/fs:5.1f}s frames={int(c.shape[0]):4d} unique={cu:5d} "
                f"peak={peak:.2f} zcr={zc:.3f}")
            log(f"     期望: {t}")
            log(f"     回译: {back}")
            rows.append({"tag": tag, "i": i, "dur": round(len(arr) / fs, 2),
                         "frames": int(c.shape[0]), "unique": cu, "peak": round(peak, 3),
                         "zcr": round(zc, 4), "back": back, "text": t, "path": p})
        except Exception as e:
            log(f"  {i}) 失败 {type(e).__name__}: {e}")
    del m, b, q
    gc.collect(); torch.cuda.empty_cache()

with open(os.path.join(OUT, "final_compare.json"), "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)
log(f"\n-> {os.path.join(OUT, 'final_compare.json')}")
