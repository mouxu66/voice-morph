# -*- coding: utf-8 -*-
"""B 臂（LoRA 微调）推理 —— 严格照 experiments/m1_workshop/infer_aligned_v2.py 的验路径。

为什么不能像 A 臂那样调 generate_voice_clone：
  训练脚本 build_prefill_embeds() 复刻的是 generate() 的 **non_streaming + speakers**
  分支（role + tts_pad×K + tts_bos + codec_prefill[nothink/think_bos/think_eos/speaker/
  codec_pad/codec_bos] + text_projection 偏移 + codec_bos）。LoRA 只在这个分布上学过，
  喂 generate_voice_clone 的 icl 分支 = 训练/推理分布错位 = 8 月那个"电音"坑
  （实测：cam 0.15、zcr 0.30、永不 EOS、一路跑到 2048 帧上限）。

必须满足的四条（缺一出故障）：
  1. 4-bit 加载 base（与训练时 build_bnb_config() 一致），配 llm_int8_skip_modules；
  2. get/set_input_embeddings 补丁（PEFT 需要）；
  3. 声纹锚点写进 codec_embedding[3000] + talker_config.spk_id 注册；
  4. 走 model.generate(..., speakers=[SPK], non_streaming_mode=True)。
"""
import argparse
import gc
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)  # 保证从任意 cwd 都能导入同目录的 qlora_quant_config

import librosa
import numpy as np
import soundfile as sf
import torch
from peft import PeftModel

# 量化配置单一来源：skip 名单与 BitsAndBytesConfig 必须与训练脚本逐字段一致，
# 禁止在本脚本另抄裸名清单（transformers≥5 前缀/结尾语义下会漏过命门模块，
# 已造成三次 4bit↔fp 错配事故）。详见 qlora_quant_config.py 顶部说明。
from qlora_quant_config import build_bnb_config

ROOT = os.path.abspath(os.path.join(HERE, ".."))
WORK = os.path.join(ROOT, "media", "ft_work", "ppt12")
AB = os.path.join(WORK, "out_ab")
BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
REF = os.path.join(WORK, "ref_24k.wav")
SPK = "ppt12_speaker"
SPK_ID = 3000

TEST_TEXTS = [
    "这一节我们讲三个概念，先看第一个。",
    "把步骤拆开，一步一步做就不会乱。",
    "注意这里的条件，符号写反了结论就完全不同。",
    "我们下节课继续讲第二部分的内容。",
    "这道题的关键是先判断函数的单调性。",
]

sys.stdout.reconfigure(encoding="utf-8")


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--out", default=os.path.join(AB, "B"))
    ap.add_argument("--tag", default="")
    ap.add_argument("--max-new-tokens", type=int, default=400)
    args = ap.parse_args()

    from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

    os.makedirs(args.out, exist_ok=True)

    bnb = build_bnb_config()

    log("[load] base (4-bit，与训练一致) ...")
    qwen3tts = Qwen3TTSModel.from_pretrained(
        BASE, torch_dtype=torch.bfloat16, attn_implementation="sdpa",
        quantization_config=bnb)
    base = qwen3tts.model

    def _gi(self):
        return self.talker.model.codec_embedding

    def _si(self, v):
        self.talker.model.codec_embedding = v

    base.get_input_embeddings = _gi.__get__(base)
    base.set_input_embeddings = _si.__get__(base)

    log(f"[load] 挂 adapter {args.adapter}")
    model = PeftModel.from_pretrained(base, args.adapter)
    model.eval()

    # 锚点
    base.speaker_encoder = base.speaker_encoder.to(getattr(base, "dtype", torch.bfloat16))
    audio, sr = librosa.load(REF, sr=None, mono=True)
    tgt = base.speaker_encoder_sample_rate
    if int(sr) != int(tgt):
        audio = librosa.resample(y=audio.astype(np.float32), orig_sr=int(sr), target_sr=int(tgt))
    emb = base.extract_speaker_embedding(audio=audio.astype(np.float32), sr=int(tgt))
    emb = emb.unsqueeze(0) if emb.dim() == 1 else emb
    emb = emb.detach().to(torch.bfloat16)
    ce = base.talker.model.codec_embedding
    with torch.no_grad():
        ce.weight[SPK_ID] = emb[0].to(ce.weight.dtype).to(ce.weight.device)
    log(f"[anchor] codec_embedding[{SPK_ID}] <- 目标声纹 norm={float(emb.norm()):.3f}")

    tc = base.config.talker_config
    tc.spk_id = {SPK: SPK_ID}
    tc.spk_is_dialect = {SPK: False}
    base.supported_speakers = tc.spk_id.keys()

    results = []
    for i, text in enumerate(TEST_TEXTS, 1):
        t0 = time.time()
        try:
            iid = qwen3tts._tokenize_texts([qwen3tts._build_assistant_text(text)])
            codes, _ = model.generate(
                input_ids=iid, languages=["Auto"], speakers=[SPK],
                non_streaming_mode=True, do_sample=True, temperature=0.8,
                top_p=0.95, repetition_penalty=1.1, max_new_tokens=args.max_new_tokens)
            c = codes[0]
            cu = int(c.unique().numel()) if hasattr(c, "unique") else len(set(c.tolist()))
            n = int(c.shape[0]) if hasattr(c, "shape") else len(c)
            wavs, fs = base.speech_tokenizer.decode([{"audio_codes": c}])
            arr = np.asarray(wavs[0], dtype=np.float32)
            if arr.size == 0:
                raise RuntimeError("空音频")
            p = os.path.join(args.out, f"{args.tag}{i:02d}.wav")
            sf.write(p, arr, int(fs))
            zc = float(np.mean(np.diff(np.sign(arr)) != 0))
            hit_cap = n >= args.max_new_tokens
            log(f"[B] {i}/{len(TEST_TEXTS)} dur={len(arr)/fs:.1f}s frames={n} unique={cu} "
                f"zcr={zc:.3f} {'★触顶' if hit_cap else '会EOS'} {time.time()-t0:.0f}s")
            results.append({"arm": "B", "idx": i, "text": text, "path": p,
                            "dur": round(len(arr) / fs, 2), "frames": n, "unique": cu,
                            "zcr": round(zc, 4), "hit_cap": hit_cap,
                            "gen_s": round(time.time() - t0, 1)})
        except Exception as e:
            log(f"[B] {i} 失败: {type(e).__name__}: {e}")
            results.append({"arm": "B", "idx": i, "text": text, "path": None,
                            "error": f"{type(e).__name__}: {e}"})

    gp = os.path.join(AB, "gen_result_B.jsonl")
    with open(gp, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    log(f"[B] 完成 {sum(1 for r in results if r.get('path'))}/{len(results)} -> {gp}")

    del model, base, qwen3tts
    gc.collect()
    torch.cuda.empty_cache()


if __name__ == "__main__":
    main()
