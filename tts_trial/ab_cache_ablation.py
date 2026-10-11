# -*- coding: utf-8 -*-
"""最小判别：LoRA 自由生成崩坏是否来自 transformers v5 的 KV-cache 路径。

已证：teacher-forcing（无 cache 单次前向）LoRA loss=4.08 正常；自由自回归（默认
use_cache=True）peak=1.00。本脚本对同一 bf16 base + 同一 adapter、同一句话，只切换
talker / code_predictor 的 use_cache，比对 peak / 时长 / ASR。
"""
import gc
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import librosa
import numpy as np
import soundfile as sf
import torch
from peft import PeftModel

sys.stdout.reconfigure(encoding="utf-8")
BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
PPT = r"D:\变声\media\ft_work\ppt12"
ADAPTER = os.path.join(PPT, "out_ga1", "adapter-final")
REF = os.path.join(PPT, "ref_24k.wav")
OUT = os.path.join(PPT, "out_ab", "cache_test")
os.makedirs(OUT, exist_ok=True)
SPK_ID, SPK, TEXT = 3000, "spk", "这一节我们讲三个概念，先看第一个。"


def log(m): print(m, flush=True)


def set_anchor(b):
    b.speaker_encoder = b.speaker_encoder.to(getattr(b, "dtype", torch.bfloat16))
    a, sr = librosa.load(REF, sr=None, mono=True)
    tgt = b.speaker_encoder_sample_rate
    if int(sr) != int(tgt):
        a = librosa.resample(y=a.astype(np.float32), orig_sr=int(sr), target_sr=int(tgt))
    e = b.extract_speaker_embedding(audio=a.astype(np.float32), sr=int(tgt))
    e = e.unsqueeze(0) if e.dim() == 1 else e
    ce = b.talker.model.codec_embedding
    with torch.no_grad():
        ce.weight[SPK_ID] = e.detach().to(torch.bfloat16)[0].to(ce.weight.dtype).to(ce.weight.device)


def run(tag, cache_talker, cache_sub):
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
    q = Qwen3TTSModel.from_pretrained(BASE, torch_dtype=torch.bfloat16, attn_implementation="sdpa")
    b = q.model.to("cuda")
    b.get_input_embeddings = (lambda self: self.talker.model.codec_embedding).__get__(b)
    b.set_input_embeddings = (lambda self, v: setattr(self.talker.model, "codec_embedding", v)).__get__(b)
    set_anchor(b)
    m = PeftModel.from_pretrained(b, ADAPTER)
    m.eval()
    tc = b.config.talker_config
    tc.spk_id, tc.spk_is_dialect = {SPK: SPK_ID}, {SPK: False}
    b.supported_speakers = tc.spk_id.keys()

    # 关键开关：自由生成默认 use_cache 取自 generation_config；这里强制关掉。
    for obj, on in ((b.talker, cache_talker), (b.talker.code_predictor, cache_sub)):
        if hasattr(obj, "generation_config") and obj.generation_config is not None:
            obj.generation_config.use_cache = bool(on)
        if hasattr(obj, "config") and obj.config is not None:
            obj.config.use_cache = bool(on)
    log(f"[{tag}] talker.cache={b.talker.generation_config.use_cache} "
        f"sub.cache={b.talker.code_predictor.generation_config.use_cache}")

    tk = q._tokenize_texts([q._build_assistant_text(TEXT)])
    tk = [x.to("cuda") if torch.is_tensor(x) else torch.tensor(x, device="cuda") for x in tk]
    with torch.no_grad():
        codes, _ = m.generate(input_ids=tk, languages=["Auto"], speakers=[SPK],
                              non_streaming_mode=True, do_sample=False, max_new_tokens=400)
    c = codes[0]
    wavs, fs = b.speech_tokenizer.decode([{"audio_codes": c}])
    arr = np.asarray(wavs[0], dtype=np.float32)
    p = os.path.join(OUT, f"{tag}.wav")
    sf.write(p, arr, int(fs))
    peak = float(np.max(np.abs(arr)))
    log(f"[{tag}] dur={len(arr)/fs:.1f}s frames={int(c.shape[0])} peak={peak:.3f} -> {p}")
    del m, b, q
    gc.collect(); torch.cuda.empty_cache()
    return p, peak


def main():
    cases = [
        ("cache_on", True, True),    # 复现已知崩坏
        ("talker_off", False, True),
        ("both_off", False, False),
    ]
    from faster_whisper import WhisperModel
    import zhconv
    wm = WhisperModel("small", device="cuda", compute_type="float16")
    for tag, ct, cs in cases:
        try:
            p, peak = run(tag, ct, cs)
            segs, _ = wm.transcribe(p, language="zh", vad_filter=False)
            back = zhconv.convert("".join(s.text for s in segs), "zh-cn")
            log(f"[{tag}] peak={peak:.3f} 回译: {back}")
        except Exception as e:
            import traceback; traceback.print_exc()
            log(f"[{tag}] 失败 {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
