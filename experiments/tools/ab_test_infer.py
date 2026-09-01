# coding=utf-8
"""A/B 试听：袋鼠骑士(QLoRA 微调) vs 原音切片。
正确合并方式：全精度加载 base -> PEFT 加载 adapter-epoch-2 -> merge_and_unload 得到干净浮点模型。
（final_model/model.safetensors 本机保存成了 4bit 打包形态，不能直接用，故走 adapter 重新合并）
"""
import os, time, shutil
import torch
import soundfile as sf
from peft import PeftModel

from qwen_tts import Qwen3TTSModel

BASE      = "D:/变声/tts_models/qwen3-tts-1.7b-base"
ADAPTER   = "D:/变声/tts_models/kangaroo_knight_qlora/adapter-epoch-2"
REF_AUDIO = "D:/变声/_trash_20260831/merg_004/reference.wav"
REF_TEXT  = "我真的恨上些OK,一会我给你分享的视频。"
TEXT      = "现在是2026年8月28号"
ORIG_CLIP = "D:/变声/media/clips/video_260828_105338_017.wav"
OUT_DIR   = "D:/变声/outputs/ab_test"
os.makedirs(OUT_DIR, exist_ok=True)

print("[1/4] loading base model (full precision, no quant) ...", flush=True)
tts = Qwen3TTSModel.from_pretrained(
    BASE,
    device_map="cuda:0",
    dtype=torch.bfloat16,
    attn_implementation="sdpa",
)
print(f"    tts_model_type={tts.model.tts_model_type}", flush=True)

print("[2/4] loading LoRA adapter and merging (clean float weights) ...", flush=True)
peft_model = PeftModel.from_pretrained(tts.model, ADAPTER)
merged = peft_model.merge_and_unload()
tts.model = merged
print("    merge done; model dtype:", next(tts.model.parameters()).dtype, flush=True)

print("[3/4] generating synthetic (袋鼠骑士) for:", TEXT, flush=True)
torch.cuda.synchronize(); t0 = time.time()
wavs, sr = tts.generate_voice_clone(
    text=TEXT,
    language="Chinese",
    ref_audio=REF_AUDIO,
    ref_text=REF_TEXT,
    x_vector_only_mode=False,
    max_new_tokens=2048,
    do_sample=True, top_k=50, top_p=1.0, temperature=0.9,
    repetition_penalty=1.05,
)
torch.cuda.synchronize(); print(f"    gen time {time.time()-t0:.2f}s sr={sr}", flush=True)

syn_path = os.path.join(OUT_DIR, "kangaroo_knight_017.wav")
sf.write(syn_path, wavs[0], sr)
print("    saved", syn_path, flush=True)

print("[4/4] copying original clip for A/B ...", flush=True)
orig_path = os.path.join(OUT_DIR, "original_017.wav")
shutil.copyfile(ORIG_CLIP, orig_path)
print("    saved", orig_path, flush=True)
print("DONE")
