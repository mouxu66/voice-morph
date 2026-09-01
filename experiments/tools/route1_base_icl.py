# route1: base 原模型 + ICL 克隆（不套 LoRA），严格对齐成功版参数
import os, time
import torch
import soundfile as sf
from qwen_tts import Qwen3TTSModel

BASE      = "D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_AUDIO = "D:/变声/_trash_20260831/merg_004/reference.wav"
REF_TEXT  = "我真的恨上些OK,一会我给你分享的视频。"
TEXT      = "现在是2026年8月28号"
OUT       = "D:/变声/outputs/ab_test/base_icl_017.wav"
os.makedirs("D:/变声/outputs/ab_test", exist_ok=True)

print("[1] loading base (bf16, cuda) ...", flush=True)
tts = Qwen3TTSModel.from_pretrained(
    BASE, device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa",
)
print(f"    tts_model_type={tts.model.tts_model_type}", flush=True)

print("[2] generate base ICL clone:", TEXT, flush=True)
torch.cuda.synchronize(); t0 = time.time()
wavs, sr = tts.generate_voice_clone(
    text=TEXT, language="Chinese", ref_audio=REF_AUDIO, ref_text=REF_TEXT,
    x_vector_only_mode=False, max_new_tokens=2048,
    do_sample=True, top_k=50, top_p=1.0, temperature=0.9, repetition_penalty=1.05,
)
torch.cuda.synchronize(); print(f"    gen {time.time()-t0:.2f}s sr={sr}", flush=True)

sf.write(OUT, wavs[0], sr)
print("    saved", OUT, flush=True)
print("ROUTE1 DONE")
