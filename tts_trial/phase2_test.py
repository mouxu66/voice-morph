"""Phase 2 集成验证 + Phase 3 指标（一次 GPU 加载跑完）。

运行：
    D:/变声/tts_trial/venv312/Scripts/python.exe D:/变声/tts_trial/phase2_test.py

内容：
  A. 贪心 rp=1.0：orig vs fast —— codes 逐步对比（预期仅 bf16 噪声级分歧）、
     音频时长、声纹余弦相似度
  B. 贪心 rp=1.05（默认惩罚）：orig vs fast —— 验证重复惩罚实现
  C. 采样默认参数：fast×5 vs orig×5 计时中位数、RTF（重点指标）
  D. 多请求序列：不同长度文本 + 贪心/采样图切换（验证图复用与 L 变化）
  E. 长文本 ~30s：fast 计时 + RTF + 显存峰值（StaticCache 不越界）
  F. ICL 模式（whisper 转写 ref_text）：fast 生成 + 相似度
  G. 兜底：top_p<1 应抛 FallbackToSlow
  H. 输出 wav 供盲听 + phase2_result.json
"""
import json
import statistics
import sys
import time

import numpy as np
import soundfile as sf
import torch

sys.path.insert(0, r"D:\变声\m2_server")

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_AUDIO = r"D:/变声/tts_models/ref/meituan_rat_002.wav"
OUT_DIR = r"D:/变声/outputs"
REPORT = r"D:/变声/tts_trial/phase2_result.json"
TEXT = "巴黎罗，我要掏你炉子了，今天不给你送外卖，你的奶茶已经凉了，麻烦你下楼取一下。"
LONG_TEXT = ("欢迎来到变声工坊的语音合成测试环节。今天我们要验证的是 CUDA Graph 加速之后，"
             "长文本合成的速度和质量是否依然稳定。按照每秒大约五个字的正常语速来估算，"
             "这段文字大概需要二十五秒到三十秒的音频时长，足以覆盖大多数日常使用的场景。"
             "如果这一段能够顺利完成，说明静态缓存的长度预算设置是合理的。")

from qwen_tts import Qwen3TTSModel
from fast_tts import FastVoiceCloneEngine, FallbackToSlow

result = {"ref_audio": REF_AUDIO, "text": TEXT}
print("loading model ...", flush=True)
model = Qwen3TTSModel.from_pretrained(MODEL_DIR, device_map="cuda:0",
                                      dtype=torch.bfloat16)
talker = model.model.talker
eng = FastVoiceCloneEngine(model)
print("engine ready", flush=True)


def spk_emb(path):
    p = model.create_voice_clone_prompt(ref_audio=path, ref_text=".",
                                        x_vector_only_mode=True)
    e = p[0].ref_spk_embedding
    e = e.detach().float().cpu().numpy().flatten()
    n = np.linalg.norm(e)
    return e / n if n > 0 else e


def cos(a, b):
    return float(np.dot(a, b))


def save_wav(wav, sr, name):
    sf.write(rf"{OUT_DIR}/{name}", wav, sr)
    return rf"{OUT_DIR}/{name}"


def time_it(fn, reps=5):
    ts = []
    for _ in range(reps):
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        out = fn()
        torch.cuda.synchronize()
        ts.append(time.perf_counter() - t0)
    return statistics.median(ts), out


def capture_orig_codes(fn):
    """hook talker.generate 抓原版 codes（贪心模式可逐位对比）。"""
    cap = {}
    orig_generate = talker.generate

    def _hook(*a, **kw):
        out = orig_generate(*a, **kw)
        cap["codes"] = torch.stack(
            [h[-1] for h in out.hidden_states if h[-1] is not None], dim=1)[0].clone()
        return out

    talker.generate = _hook
    try:
        fn()
    finally:
        talker.generate = orig_generate
    return cap["codes"]


REF_EMB = spk_emb(REF_AUDIO)
prompt = model.create_voice_clone_prompt(ref_audio=REF_AUDIO, ref_text=".",
                                         x_vector_only_mode=True)

# ---------------- A. 贪心 rp=1.0 ----------------
print("[A] greedy rp=1.0: orig vs fast", flush=True)
greedy_kw = dict(do_sample=False, subtalker_dosample=False, repetition_penalty=1.0)
t0 = time.perf_counter()
orig_codes = capture_orig_codes(
    lambda: model.generate_voice_clone(text=[TEXT], language=["Chinese"],
                                       voice_clone_prompt=prompt, **greedy_kw)).cpu()
t_orig_greedy = time.perf_counter() - t0
wavs, sr = model.generate_voice_clone(text=[TEXT], language=["Chinese"],
                                      voice_clone_prompt=prompt, **greedy_kw)
p_orig = save_wav(wavs[0], sr, "phase2_orig_greedy.wav")

t_fast_greedy, (wavs_f, sr_f) = time_it(
    lambda: eng.generate(text=TEXT, language="Chinese", voice_clone_prompt=prompt,
                         **greedy_kw), reps=3)
p_fast = save_wav(wavs_f[0], sr_f, "phase2_fast_greedy.wav")
fast_codes = eng.last_codes

n = min(orig_codes.shape[0], fast_codes.shape[0])
diff = (orig_codes[:n] != fast_codes[:n]).any(dim=-1).nonzero().flatten()
first_diff = int(diff[0]) if len(diff) else -1
result["A_greedy"] = {
    "orig_s": round(t_orig_greedy, 2), "fast_s": round(t_fast_greedy, 2),
    "speedup": round(t_orig_greedy / t_fast_greedy, 2),
    "orig_frames": int(orig_codes.shape[0]), "fast_frames": int(fast_codes.shape[0]),
    "first_diff_frame": first_diff,
    "prefix_match_ratio": round(float((orig_codes[:n] == fast_codes[:n]).all(-1)
                                      .float().mean()), 4),
    "orig_audio_s": round(len(wavs[0]) / sr, 2),
    "fast_audio_s": round(len(wavs_f[0]) / sr_f, 2),
    "sim_ref_vs_orig": round(cos(REF_EMB, spk_emb(p_orig)), 4),
    "sim_ref_vs_fast": round(cos(REF_EMB, spk_emb(p_fast)), 4),
    "sim_orig_vs_fast": round(cos(spk_emb(p_orig), spk_emb(p_fast)), 4),
}
print(json.dumps(result["A_greedy"], ensure_ascii=False, indent=2), flush=True)

# ---------------- B. 贪心 rp=1.05（验证重复惩罚） ----------------
print("[B] greedy rp=1.05: orig vs fast", flush=True)
rp_kw = dict(do_sample=False, subtalker_dosample=False, repetition_penalty=1.05)
orig_codes_b = capture_orig_codes(
    lambda: model.generate_voice_clone(text=[TEXT], language=["Chinese"],
                                       voice_clone_prompt=prompt, **rp_kw)).cpu()
wavs_b, sr_b = eng.generate(text=TEXT, language="Chinese", voice_clone_prompt=prompt,
                            **rp_kw)
p_fast_b = save_wav(wavs_b[0], sr_b, "phase2_fast_greedy_rp.wav")
fb = eng.last_codes
nb = min(orig_codes_b.shape[0], fb.shape[0])
diffb = (orig_codes_b[:nb] != fb[:nb]).any(dim=-1).nonzero().flatten()
result["B_greedy_rp105"] = {
    "orig_frames": int(orig_codes_b.shape[0]), "fast_frames": int(fb.shape[0]),
    "first_diff_frame": int(diffb[0]) if len(diffb) else -1,
    "sim_ref_vs_fast": round(cos(REF_EMB, spk_emb(p_fast_b)), 4),
}
print(json.dumps(result["B_greedy_rp105"], ensure_ascii=False, indent=2), flush=True)

# ---------------- C. 采样计时（重点） ----------------
print("[C] sampling timing: fast x5 vs orig x5", flush=True)
t_fast_s, (wavs_s, sr_s) = time_it(
    lambda: eng.generate(text=TEXT, language="Chinese", voice_clone_prompt=prompt),
    reps=5)
p_samp = save_wav(wavs_s[0], sr_s, "phase2_fast_sampling.wav")
t_orig_s, _ = time_it(
    lambda: model.generate_voice_clone(text=[TEXT], language=["Chinese"],
                                       voice_clone_prompt=prompt), reps=5)
audio_s = len(wavs_s[0]) / sr_s
result["C_sampling_timing"] = {
    "audio_s": round(audio_s, 2),
    "orig_median_s": round(t_orig_s, 2), "fast_median_s": round(t_fast_s, 2),
    "speedup": round(t_orig_s / t_fast_s, 2),
    "rtf_orig": round(audio_s / t_orig_s, 3), "rtf_fast": round(audio_s / t_fast_s, 3),
    "sim_ref_vs_fast_sampling": round(cos(REF_EMB, spk_emb(p_samp)), 4),
}
print(json.dumps(result["C_sampling_timing"], ensure_ascii=False, indent=2), flush=True)

# ---------------- D. 多请求序列（图复用 + L 变化） ----------------
print("[D] multi-request sequence", flush=True)
seq = [("短句测试，你好。", dict(do_sample=False, subtalker_dosample=False,
                                repetition_penalty=1.0)),
       ("这一句稍微长一点点，用来验证不同 prefill 长度下的缓存复用情况。",
        dict(do_sample=False, subtalker_dosample=False, repetition_penalty=1.0)),
       ("第三句切回采样模式，验证两张图交替复用不会互相污染。", {})]
d_ok, d_dur = [], []
for txt, kw in seq:
    wavs_d, sr_d = eng.generate(text=txt, language="Chinese",
                                voice_clone_prompt=prompt, **kw)
    d_ok.append(True)
    d_dur.append(round(len(wavs_d[0]) / sr_d, 2))
result["D_multi_request"] = {"ok": d_ok, "audio_s": d_dur,
                             "graphs": len(eng._graphs)}
print(json.dumps(result["D_multi_request"], ensure_ascii=False), flush=True)

# ---------------- E. 长文本 ~30s ----------------
print("[E] long text (~30s)", flush=True)
torch.cuda.reset_peak_memory_stats()
t_long, (wavs_l, sr_l) = time_it(
    lambda: eng.generate(text=LONG_TEXT, language="Chinese",
                         voice_clone_prompt=prompt), reps=3)
p_long = save_wav(wavs_l[0], sr_l, "phase2_fast_long.wav")
long_audio_s = len(wavs_l[0]) / sr_l
result["E_long"] = {
    "audio_s": round(long_audio_s, 2), "gen_median_s": round(t_long, 2),
    "rtf": round(long_audio_s / t_long, 3), "frames": int(eng.last_codes.shape[0]),
    "peak_mem_mb": round(torch.cuda.max_memory_allocated() / 2**20),
    "sim_ref_vs_fast_long": round(cos(REF_EMB, spk_emb(p_long)), 4),
}
print(json.dumps(result["E_long"], ensure_ascii=False, indent=2), flush=True)

# ---------------- F. ICL 模式 ----------------
print("[F] ICL mode (whisper ref_text)", flush=True)
try:
    from faster_whisper import WhisperModel
    wm = WhisperModel("small", device="cuda", compute_type="float16")
    segs, _ = wm.transcribe(REF_AUDIO, language="zh", vad_filter=True)
    ref_text = "".join(s.text for s in segs).strip()
    prompt_icl = model.create_voice_clone_prompt(
        ref_audio=REF_AUDIO, ref_text=ref_text, x_vector_only_mode=False)
    wavs_i, sr_i = eng.generate(text=TEXT, language="Chinese",
                                voice_clone_prompt=prompt_icl)
    p_icl = save_wav(wavs_i[0], sr_i, "phase2_fast_icl.wav")
    result["F_icl"] = {"ref_text": ref_text,
                       "audio_s": round(len(wavs_i[0]) / sr_i, 2),
                       "sim_ref_vs_fast": round(cos(REF_EMB, spk_emb(p_icl)), 4)}
except Exception as exc:
    result["F_icl"] = {"error": f"{type(exc).__name__}: {exc}"}
print(json.dumps(result["F_icl"], ensure_ascii=False, indent=2), flush=True)

# ---------------- G. 兜底 ----------------
print("[G] fallback check (top_p=0.9)", flush=True)
try:
    eng.generate(text=TEXT, language="Chinese", voice_clone_prompt=prompt, top_p=0.9)
    result["G_fallback"] = {"ok": False, "note": "top_p<1 未按预期抛 FallbackToSlow"}
except FallbackToSlow as exc:
    result["G_fallback"] = {"ok": True, "reason": str(exc)}
print(json.dumps(result["G_fallback"], ensure_ascii=False), flush=True)

result["engine_stats"] = eng.stats
result["gpu_total_mem_mb"] = round(torch.cuda.memory_allocated() / 2**20)
with open(REPORT, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(f"\nsaved -> {REPORT}", flush=True)
print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
