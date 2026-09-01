# route3: 历史验证方案 —— 纯 bf16 base + 袋鼠锚点，无 LoRA、无量化，x_vector_only 纯声纹
import os, time
import torch
import soundfile as sf
from qwen_tts import Qwen3TTSModel

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
REF  = "D:/变声/media/voicebank/kangaroo/reference.wav"   # 6.38s 纯袋鼠男声(105338_027)
TEXT = "现在是2026年8月28号"
OUT  = "D:/变声/outputs/ab_test/base_vc_017.wav"
os.makedirs("D:/变声/outputs/ab_test", exist_ok=True)

print("[1] loading base (纯 bf16, 无量化, 无 LoRA) ...", flush=True)
qwen3tts = Qwen3TTSModel.from_pretrained(
    BASE, torch_dtype=torch.bfloat16, attn_implementation="sdpa",
)
print("    loaded", flush=True)

print("[2] generate (x_vector_only=True, 纯声纹, 不吃 ref_text 噪声):", TEXT, flush=True)
torch.cuda.synchronize(); t0 = time.time()
wavs, sr = qwen3tts.generate_voice_clone(
    text=[TEXT], ref_audio=REF, x_vector_only_mode=True,
    non_streaming_mode=True, do_sample=True, temperature=0.8,
    top_p=0.95, repetition_penalty=1.1, max_new_tokens=200,
)
torch.cuda.synchronize(); print(f"    gen {time.time()-t0:.2f}s sr={sr}", flush=True)

sf.write(OUT, wavs[0], sr)
print("    saved", OUT, flush=True)
print("ROUTE3 DONE")
