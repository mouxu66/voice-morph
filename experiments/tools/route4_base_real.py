# route4: 用"袋鼠真声金标" merg_004 当锚点，纯 bf16 base，两种模式对比
#  - xvec: x_vector_only=True  (只验音色，韵律/语速会退化成模型默认=快)
#  - icl : x_vector_only=False (ICL，吃参考音的韵律+音色，应保留原速)
import os, time
import torch
import soundfile as sf
from qwen_tts import Qwen3TTSModel

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_REAL = "D:/变声/_trash_20260831/merg_004/reference.wav"   # 袋鼠真声金标 20.8s
REF_TEXT = "我真的恨上些OK,一会我给你分享的视频。"            # merg_004 的 whisper 转写(ICL 对齐用)
TEXT = "现在是2026年8月28号"
OUT = "D:/变声/outputs/ab_test"
os.makedirs(OUT, exist_ok=True)

print("[1] loading base (纯 bf16, 无量化, 无 LoRA) ...", flush=True)
m = Qwen3TTSModel.from_pretrained(BASE, torch_dtype=torch.bfloat16, attn_implementation="sdpa")
print("    loaded", flush=True)

def gen_xvec():
    print("[2a] x_vector_only=True (真声锚点, 只验音色):", TEXT, flush=True)
    t0 = time.time(); torch.cuda.synchronize()
    wavs, sr = m.generate_voice_clone(
        text=[TEXT], ref_audio=REF_REAL, x_vector_only_mode=True,
        non_streaming_mode=True, do_sample=True, temperature=0.8,
        top_p=0.95, repetition_penalty=1.1, max_new_tokens=200,
    )
    torch.cuda.synchronize()
    sf.write(f"{OUT}/base_xvec_real_017.wav", wavs[0], sr)
    print(f"    gen {time.time()-t0:.2f}s sr={sr} -> base_xvec_real_017.wav", flush=True)

def gen_icl():
    print("[2b] ICL (真声锚点, 验音色+原速):", TEXT, flush=True)
    t0 = time.time(); torch.cuda.synchronize()
    wavs, sr = m.generate_voice_clone(
        text=[TEXT], ref_audio=REF_REAL, ref_text=REF_TEXT, x_vector_only_mode=False,
        non_streaming_mode=True, do_sample=True, temperature=0.8,
        top_p=0.95, repetition_penalty=1.1, max_new_tokens=200,
    )
    torch.cuda.synchronize()
    sf.write(f"{OUT}/base_icl_real_017.wav", wavs[0], sr)
    print(f"    gen {time.time()-t0:.2f}s sr={sr} -> base_icl_real_017.wav", flush=True)

gen_xvec()
gen_icl()
print("ROUTE4 DONE")
