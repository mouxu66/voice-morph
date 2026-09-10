"""真机测速：faster-qwen3-tts 后端在 RTX5060(8GB) 上的 TTS 延迟。

测四项：
  1) 加载 + warmup（线上启动成本）
  2) 声纹提取（xvec_only）
  3) 整句 TTS（generate_voice_clone，端到端 + RTF）
  4) 流式 TTS（generate_voice_clone_streaming，TTFA 首包延迟 + 总耗时 + RTF）
     chunk_size ∈ {8, 12, 20} 对比首包早晚

音色 = 袋鼠骑士（媒体/voicebank/kangaroo/reference.wav，xvec_only 模式无需 ref_text）。
"""
import time
import numpy as np
import torch
from faster_qwen3_tts import FasterQwen3TTS

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF = r"D:/变声/media/voicebank/kangaroo/reference.wav"
TEXT = "你好，我是袋鼠骑士，正在测试实时变声的流式合成延迟表现，请仔细听这段效果。"

SR = 24000


def to_np(x):
    if torch.is_tensor(x):
        return x.detach().float().cpu().numpy()
    return np.asarray(x, dtype=np.float32)


def gb():
    return torch.cuda.memory_allocated() / 1024 / 1024 / 1024


print("=== 加载模型 ===", flush=True)
t0 = time.perf_counter()
MODEL = FasterQwen3TTS.from_pretrained(
    MODEL_DIR, device="cuda", dtype=torch.bfloat16,
    attn_implementation="sdpa", max_seq_len=4096, qwentts_use_fa=False)
t_load = time.perf_counter() - t0
print(f"加载 {t_load:.1f}s  显存 {gb():.2f}GB", flush=True)

print("=== warmup (捕获 CUDA Graph) ===", flush=True)
t0 = time.perf_counter()
MODEL.warmup()
t_warm = time.perf_counter() - t0
print(f"warmup {t_warm:.1f}s  warmed_up={getattr(MODEL, '_warmed_up', None)}  显存 {gb():.2f}GB", flush=True)

print("=== 声纹提取 (xvec_only) ===", flush=True)
t0 = time.perf_counter()
items = MODEL.model.create_voice_clone_prompt(ref_audio=REF, ref_text=".", x_vector_only_mode=True)
t_emb = time.perf_counter() - t0
print(f"声纹 {t_emb:.2f}s", flush=True)

print("=== 整句 TTS (generate_voice_clone) ===", flush=True)
def whole():
    t0 = time.perf_counter()
    wavs, sr = MODEL.generate_voice_clone(
        text=TEXT, language="Chinese", ref_audio=REF, ref_text="", xvec_only=True)
    dt = time.perf_counter() - t0
    a = to_np(wavs[0])
    return dt, sr, len(a) / sr

whole_total = whole_dur = whole_sr = 0
for i in range(2):
    dt, sr, dur = whole()
    whole_total, whole_dur, whole_sr = dt, dur, sr
    print(f"  整句[{i}] 总耗时 {dt:.2f}s  音频 {dur:.2f}s  RTF {dt/dur:.3f}", flush=True)

print("=== 流式 TTS (generate_voice_clone_streaming) ===", flush=True)
def stream(chunk_size):
    t0 = time.perf_counter()
    first = None
    n = 0
    total_audio = 0
    sr = SR
    for chunk, s, timing in MODEL.generate_voice_clone_streaming(
            text=TEXT, language="Chinese", ref_audio=REF, ref_text="",
            chunk_size=chunk_size, xvec_only=True):
        if first is None:
            first = time.perf_counter() - t0
        c = to_np(chunk)
        total_audio += len(c)
        sr = s
        n += 1
    total = time.perf_counter() - t0
    dur = total_audio / sr
    return first, total, dur, n, sr

stream_results = {}
for cs in (8, 12, 20):
    rec = None
    for i in range(2):
        first, total, dur, n, sr = stream(cs)
        rec = (first, total, dur, n)
        print(f"  流式 cs={cs}[{i}]  TTFA {first:.2f}s  总耗时 {total:.2f}s  "
              f"音频 {dur:.2f}s  RTF {total/dur:.3f}  chunks={n}", flush=True)
    stream_results[cs] = rec

print("\n=== 汇总 ===", flush=True)
print(f"加载 {t_load:.1f}s | warmup {t_warm:.1f}s | 声纹 {t_emb:.2f}s", flush=True)
print(f"整句: 总 {whole_total:.2f}s / 音频 {whole_dur:.2f}s / RTF {whole_total/whole_dur:.3f}", flush=True)
for cs, (first, total, dur, n) in stream_results.items():
    print(f"流式 cs={cs}: TTFA {first:.2f}s / 总 {total:.2f}s / RTF {total/dur:.3f} / "
          f"首包比整句早 {whole_total-first:.2f}s", flush=True)
print("=== 完成 ===", flush=True)
