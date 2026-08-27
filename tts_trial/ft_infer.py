# -*- coding: utf-8 -*-
"""微调模型试音：加载 checkpoint-epoch-2 合成 3 句话 -> 用基座模型声纹编码器打相似度。
结果写 ft_infer_out.txt。策略：先载入微调模型出音频并释放，再载基座模型算声纹，避免同驻超 8GB。
"""
import gc
import json
import os
import time

import numpy as np
import soundfile as sf
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_TXT = os.path.join(HERE, "ft_infer_out.txt")
CKPT = os.path.join(HERE, "ft_output", "checkpoint-epoch-2")
BASE = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_WAV = os.path.join(HERE, "ft_data", "feidudu_full.wav")
GEN_DIR = os.path.join(HERE, "ft_samples")

os.makedirs(GEN_DIR, exist_ok=True)
open(OUT_TXT, "w").close()


def log(m):
    with open(OUT_TXT, "a", encoding="utf-8") as f:
        f.write(m + "\n")


TEXTS = [
    "今天早上我出门的时候，发现楼下的猫已经等在门口了。",
    "这个问题其实很简单，只要把步骤拆开一步一步做就行。",
    "明天下午三点我们在老地方见面，记得把资料带上。",
]

# ---- 阶段1：微调模型合成 ----
log(f"[start] {time.strftime('%H:%M:%S')}")
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

ft = Qwen3TTSModel.from_pretrained(CKPT, dtype=torch.bfloat16, device_map="cuda:0")
log("[load] 微调模型就绪")
gen_paths = []
for i, text in enumerate(TEXTS):
    t0 = time.time()
    wavs, sr = ft.generate_custom_voice(text=text, speaker="feidudu")
    p = os.path.join(GEN_DIR, f"ft_s{i+1}.wav")
    sf.write(p, np.asarray(wavs[0]), int(sr))
    a = np.asarray(wavs[0])
    log(f"[gen] {os.path.basename(p)} {a.shape[0]/sr:.1f}s  {time.time()-t0:.0f}s")
    gen_paths.append(p)
del ft
gc.collect()
torch.cuda.empty_cache()
torch.cuda.reset_peak_memory_stats()
log(f"[mem] 释放后已分配 {torch.cuda.memory_allocated()/2**30:.2f} GiB")

# ---- 阶段2：基座模型声纹打分 ----
base = Qwen3TTSModel.from_pretrained(BASE, dtype=torch.bfloat16, device_map="cuda:0")


def emb_of(path):
    prompt = base.create_voice_clone_prompt(ref_audio=path, ref_text=".", x_vector_only_mode=True)
    item = prompt[0] if isinstance(prompt, list) else prompt
    e = item.ref_spk_embedding
    if torch.is_tensor(e):
        e = e.detach().float().cpu().numpy()
    e = np.asarray(e, dtype=np.float32).flatten()
    return e / (np.linalg.norm(e) + 1e-9)


e_ref = emb_of(REF_WAV)
# 自一致性：19.4s 参考切前后半
audio, sr = sf.read(REF_WAV)
half = os.path.join(GEN_DIR, "_ref_half.wav")
sf.write(half, audio[: audio.shape[0] // 2], sr)
e_self = emb_of(half)

log("")
log("[scores] 与你 19.4s 原声的余弦相似度")
for p in gen_paths:
    s = float(np.dot(emb_of(p), e_ref))
    log(f"  {os.path.basename(p)}: {s:.3f}")
s_self = float(np.dot(e_self, e_ref))
log(f"  自一致性基线(前半vs整段): {s_self:.3f}")
log("")
log(f"[out] 音频在 {GEN_DIR}")
log(f"[end] {time.strftime('%H:%M:%S')}")
