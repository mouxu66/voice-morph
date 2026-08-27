# -*- coding: utf-8 -*-
"""最小检查点加载验证：generate_custom_voice 一句话 + 声纹相似度。结果写 ft_loadcheck_out.txt"""
import gc
import os
import time

import numpy as np
import soundfile as sf
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "ft_loadcheck_out.txt")
CKPT = os.path.join(HERE, "ft_output", "checkpoint-epoch-0")
BASE = r"D:/变声/tts_models/qwen3-tts-1.7b-base"

open(OUT, "w").close()


def log(m):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(str(m) + "\n")


log(f"[start] {time.strftime('%H:%M:%S')}")
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

ft = Qwen3TTSModel.from_pretrained(CKPT, dtype=torch.bfloat16, device_map="cuda:0")
log("[load] 最小检查点加载成功")
spks = ft.model.get_supported_speakers() if hasattr(ft.model, "get_supported_speakers") else "?"
log(f"[speakers] {spks}")
wavs, sr = ft.generate_custom_voice(text="这是一段用来验证微调检查点是否可以正常合成的测试语音。",
                                    speaker="feidudu")
p = os.path.join(HERE, "ft_samples", "minckpt_test.wav")
sf.write(p, np.asarray(wavs[0]), int(sr))
a = np.asarray(wavs[0])
log(f"[gen] {os.path.basename(p)} 时长 {a.shape[0]/sr:.1f}s")

del ft
gc.collect()
torch.cuda.empty_cache()

base = Qwen3TTSModel.from_pretrained(BASE, dtype=torch.bfloat16, device_map="cuda:0")


def emb_of(path):
    pr = base.create_voice_clone_prompt(ref_audio=path, ref_text=".", x_vector_only_mode=True)
    it = pr[0] if isinstance(pr, list) else pr
    e = it.ref_spk_embedding.detach().float().cpu().numpy().flatten()
    return e / (np.linalg.norm(e) + 1e-9)


ref = os.path.join(HERE, "ft_data", "feidudu_full.wav")
s = float(np.dot(emb_of(p), emb_of(ref)))
log(f"[sim] 与参考音频余弦相似度: {s:.3f}")
log("[ok] 全部通过" if s > 0.9 else "[warn] 相似度偏低")
