# -*- coding: utf-8 -*-
"""决定性对照：同一推理代码、同一输入，分别挂 8月成功 adapter 与本次 adapter。

目的：区分「推理代码问题」还是「本次 LoRA 没训好」。
  - 若 8月 adapter 输出正常（zcr≈0.05、unique≈500）→ 推理代码对，是本次 LoRA 的问题
  - 若 8月 adapter 也输出异常 → 是推理代码/环境的问题（与训练无关）

注意 8月 adapter 是对**袋鼠**音色训的，参考音要用袋鼠的；
本次 adapter 用 PPT 演讲的参考音。两者各自配自己的 ref。
"""
import os
import sys
import time

import librosa
import numpy as np
import soundfile as sf
import torch
from peft import PeftModel

# skip 名单必须与训练同源（transformers≥5 裸名会漏过命门模块），见 qlora_quant_config.py。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from qlora_quant_config import build_bnb_config

sys.stdout.reconfigure(encoding="utf-8")

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
OUT = "D:/变声/media/ft_work/ppt12/out_ab/ctrl"
os.makedirs(OUT, exist_ok=True)
SPK_ID = 3000

CASES = [
    {"name": "aug_v2", "adapter": "D:/变声/tts_trial/Qwen3-TTS/finetuning/output_qlora_aligned_v2/adapter-final",
     "ref": "D:/变声/media/voicebank/kangaroo/reference.wav",
     "text": "您好，我是袋鼠骑士，很高兴为您服务。"},
    {"name": "aug_v3", "adapter": "D:/变声/tts_trial/Qwen3-TTS/finetuning/output_qlora_aligned_v3/adapter-final",
     "ref": "D:/变声/media/voicebank/kangaroo/reference.wav",
     "text": "您好，我是袋鼠骑士，很高兴为您服务。"},
    {"name": "ppt_ga8", "adapter": "D:/变声/media/ft_work/ppt12/out_ppt12/adapter-final",
     "ref": "D:/变声/media/ft_work/ppt12/ref_24k.wav",
     "text": "这一节我们讲三个概念，先看第一个。"},
    {"name": "ppt_ga1", "adapter": "D:/变声/media/ft_work/ppt12/out_ga1/adapter-final",
     "ref": "D:/变声/media/ft_work/ppt12/ref_24k.wav",
     "text": "这一节我们讲三个概念，先看第一个。"},
]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

    bnb = build_bnb_config()

    results = []
    for case in CASES:
        name = case["name"]
        ref = case["ref"]
        if not os.path.exists(case["adapter"]):
            log(f"[{name}] adapter 不存在，跳过")
            continue
        if not os.path.exists(ref):
            log(f"[{name}] 参考音不存在 {ref}，跳过")
            continue

        log(f"[{name}] 加载 base + adapter ...")
        qwen3tts = Qwen3TTSModel.from_pretrained(
            BASE, torch_dtype=torch.bfloat16, attn_implementation="sdpa",
            quantization_config=bnb)
        base = qwen3tts.model
        base.get_input_embeddings = (lambda self: self.talker.model.codec_embedding).__get__(base)
        base.set_input_embeddings = (lambda self, v: setattr(self.talker.model, "codec_embedding", v)).__get__(base)
        model = PeftModel.from_pretrained(base, case["adapter"])
        model.eval()

        # 锚点
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

        SPK = f"spk_{name}"
        tc = base.config.talker_config
        tc.spk_id = {SPK: SPK_ID}
        tc.spk_is_dialect = {SPK: False}
        base.supported_speakers = tc.spk_id.keys()

        try:
            iid = qwen3tts._tokenize_texts([qwen3tts._build_assistant_text(case["text"])])
            codes, _ = model.generate(
                input_ids=iid, languages=["Auto"], speakers=[SPK],
                non_streaming_mode=True, do_sample=True, temperature=0.8,
                top_p=0.95, repetition_penalty=1.1, max_new_tokens=400)
            c = codes[0]
            n = int(c.shape[0]) if hasattr(c, "shape") else len(c)
            cu = int(c.unique().numel())
            wavs, fs = base.speech_tokenizer.decode([{"audio_codes": c}])
            arr = np.asarray(wavs[0], dtype=np.float32)
            p = os.path.join(OUT, f"{name}.wav")
            sf.write(p, arr, int(fs))
            zc = float(np.mean(np.diff(np.sign(arr)) != 0))
            log(f"[{name}] dur={len(arr)/fs:.1f}s frames={n} unique={cu} zcr={zc:.3f} "
                f"{'★触顶' if n>=400 else '会EOS'} -> {p}")
            results.append({"name": name, "dur": round(len(arr) / fs, 2), "frames": n,
                            "unique": cu, "zcr": round(zc, 4), "path": p})
        except Exception as e:
            log(f"[{name}] 失败 {type(e).__name__}: {e}")
            results.append({"name": name, "error": str(e)})

        del model, base, qwen3tts
        import gc
        gc.collect()
        torch.cuda.empty_cache()

    log("\n=== 对照结果 ===")
    log(f"{'案例':<12}{'时长s':>8}{'帧':>7}{'unique':>8}{'zcr':>8}  判读")
    for r in results:
        if r.get("error"):
            log(f"{r['name']:<12}{'--':>8}  错误: {r['error'][:40]}")
            continue
        if r["zcr"] < 0.10 and r["unique"] < 800:
            v = "★正常（8月验收口径）"
        elif r["zcr"] < 0.15:
            v = "边缘"
        else:
            v = "异常（噪声样）"
        log(f"{r['name']:<12}{r['dur']:>8.1f}{r['frames']:>7}{r['unique']:>8}{r['zcr']:>8.3f}  {v}")


if __name__ == "__main__":
    main()
