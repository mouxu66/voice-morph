"""Phase 0 · Qwen3-TTS 推理链路分环节耗时插桩。

运行：
    D:/变声/tts_trial/venv312/Scripts/python.exe D:/变声/tts_trial/phase0_profile.py

测量（对齐方案的 Phase 0 要求）：
    1. 外层 talker.generate（HF decode 循环）总耗时
    2. prefill 耗时（变长，不进 graph）与 decode 帧数、每帧耗时
    3. talker forward 与 code predictor forward 各自耗时占比
       —— 注意：每帧实际是 1 次 talker forward + 内层 predictor.generate 的
          1 次 prefill(2 token) + 14 次 decode forward，共 15 次 forward
    4. 单步 eager 基线（sync 后取中位数）→ 理论最优估计
    5. speech_tokenizer.decode（codebook -> wav）耗时
"""
import functools
import json
import os
import statistics
import time

import soundfile as sf
import torch

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_AUDIO = r"D:/变声/tts_models/ref/meituan_rat_002.wav"
TARGET_TEXT = "巴黎罗，我要掏你炉子了，今天不给你送外卖，你的奶茶已经凉了，麻烦你下楼取一下。"
WARMUP_TEXT = "你好。"
RUNS = 3
OUT_JSON = r"D:/变声/tts_trial/phase0_result.json"

from qwen_tts import Qwen3TTSModel

print("loading model ...", flush=True)
model = Qwen3TTSModel.from_pretrained(MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)
inner = model.model            # Qwen3TTSForConditionalGeneration
talker = inner.talker
predictor = talker.code_predictor


class S:
    t_talker_gen = 0.0
    n_prefill = 0
    t_prefill = 0.0
    n_frames = 0
    t_talker_fwd = 0.0
    t_talker_model = 0.0
    n_pred_gen = 0
    t_pred_gen = 0.0
    n_pred_fwd = 0
    t_pred_fwd = 0.0
    t_pred_model = 0.0
    t_tok_decode = 0.0

    @classmethod
    def reset(cls):
        for k, v in vars(cls).items():
            if k != "reset" and isinstance(v, (int, float)):
                setattr(cls, k, 0)


CAP = {}


def snapshot_kw(kw):
    """捕获一次调用的完整 kwargs：张量 clone、Cache 保留引用。"""
    out = {}
    for k, v in kw.items():
        if torch.is_tensor(v):
            out[k] = v.clone()
        else:
            out[k] = v
    return out


def wrap_timer(obj, attr, stat, extra=None):
    orig = getattr(obj, attr)

    @functools.wraps(orig)
    def w(*a, **kw):
        t0 = time.perf_counter()
        out = orig(*a, **kw)
        setattr(S, stat, getattr(S, stat) + time.perf_counter() - t0)
        if extra is not None:
            extra(a, kw, out)
        return out

    setattr(obj, attr, w)
    return orig


# ---- 外层 talker.generate ----
def _on_talker_gen(a, kw, out):
    pass
wrap_timer(talker, "generate", "t_talker_gen", _on_talker_gen)


# ---- talker.forward：区分 prefill / decode，捕获 mid-frame ----
_orig_talker_fwd = talker.forward


@functools.wraps(_orig_talker_fwd)
def _talker_fwd_w(*a, **kw):
    t0 = time.perf_counter()
    out = _orig_talker_fwd(*a, **kw)
    dt = time.perf_counter() - t0
    ie = kw.get("inputs_embeds")
    if ie is not None and ie.dim() == 3 and ie.shape[1] > 1:
        S.n_prefill += 1
        S.t_prefill += dt
    else:
        S.n_frames += 1
        S.t_talker_fwd += dt
        if S.n_frames == 10 and "cap_talker" not in CAP:
            CAP["cap_talker"] = snapshot_kw(kw)
    return out
talker.forward = _talker_fwd_w

wrap_timer(talker.model, "forward", "t_talker_model")


# ---- 内层 predictor.generate：计时 + 捕获 prefill 输入 ----
def _on_pred_gen(a, kw, out):
    S.n_pred_gen += 1
    if S.n_pred_gen == 10 and "cap_pred" not in CAP:
        CAP["cap_pred"] = kw.get("inputs_embeds").clone()
wrap_timer(predictor, "generate", "t_pred_gen", _on_pred_gen)


def _on_pred_fwd(a, kw, out):
    S.n_pred_fwd += 1
wrap_timer(predictor, "forward", "t_pred_fwd", _on_pred_fwd)
wrap_timer(predictor.model, "forward", "t_pred_model")
wrap_timer(inner.speech_tokenizer, "decode", "t_tok_decode")


# ---------------- 运行 ----------------
print("building x-vector prompt ...", flush=True)
t0 = time.perf_counter()
prompt = model.create_voice_clone_prompt(ref_audio=REF_AUDIO, ref_text=".", x_vector_only_mode=True)
t_prompt = time.perf_counter() - t0
print(f"prompt build: {t_prompt:.2f}s", flush=True)

print("warmup ...", flush=True)
_ = model.generate_voice_clone(text=[WARMUP_TEXT], language=["Chinese"], voice_clone_prompt=prompt)
torch.cuda.synchronize()

os.makedirs(r"D:/变声/outputs", exist_ok=True)
runs = []
for i in range(RUNS):
    S.reset()
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    wavs, sr = model.generate_voice_clone(text=[TARGET_TEXT], language=["Chinese"],
                                          voice_clone_prompt=prompt)
    torch.cuda.synchronize()
    total = time.perf_counter() - t0
    audio_s = len(wavs[0]) / sr
    f = max(S.n_frames, 1)
    r = {
        "total_s": round(total, 2),
        "audio_s": round(audio_s, 2),
        "rtf": round(audio_s / total, 3),
        "frames": S.n_frames,
        "prefill_s": round(S.t_prefill, 3),
        "talker_generate_s": round(S.t_talker_gen, 2),
        "per_frame_ms": {
            "talker_fwd_total": round(S.t_talker_fwd / f * 1000, 1),
            "|_predictor_generate(15fwd)": round(S.t_pred_gen / f * 1000, 1),
            "| |_pred_fwd_15steps": round(S.t_pred_fwd / f * 1000, 1),
            "| | |_pred_model_5layer": round(S.t_pred_model / f * 1000, 1),
            "|_talker_model_28layer": round(S.t_talker_model / f * 1000, 1),
            "|_residual(emb/cat/head/pos)": round((S.t_talker_fwd - S.t_pred_gen - S.t_talker_model) / f * 1000, 1),
        },
        "outer_hf_overhead_ms_per_frame": round((S.t_talker_gen - S.t_talker_fwd - S.t_prefill) / f * 1000, 1),
        "inner_hf_overhead_ms_per_frame": round((S.t_pred_gen - S.t_pred_fwd) / f * 1000, 1),
        "pred_fwd_count(expect 15*frames)": S.n_pred_fwd,
        "tokenizer_decode_s": round(S.t_tok_decode, 2),
    }
    runs.append(r)
    print(json.dumps(r, ensure_ascii=False, indent=2), flush=True)
    sf.write(rf"D:/变声/outputs/phase0_run{i}.wav", wavs[0], sr)


# ---------------- 单步微基线（sync 取中位数） ----------------
def median_of(fn, reps=20):
    fn()
    torch.cuda.synchronize()
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        torch.cuda.synchronize()
        ts.append((time.perf_counter() - t0) * 1000)
    return round(statistics.median(ts), 2)


micro = {}

if "cap_talker" in CAP:
    try:
        cap_kw = CAP["cap_talker"]
        micro["talker_step_eager_ms(incl predictor15)"] = median_of(lambda: _orig_talker_fwd(**cap_kw))
    except Exception as e:
        micro["talker_step_err"] = repr(e)

if "cap_pred" in CAP:
    pe = CAP["cap_pred"]
    micro["predictor_inner_gen_ms(15steps)"] = median_of(
        lambda: predictor.generate(inputs_embeds=pe, max_new_tokens=15, do_sample=True,
                                   top_k=50, top_p=1.0, temperature=0.9,
                                   output_hidden_states=True, return_dict_in_generate=True))

try:
    from transformers import DynamicCache
    emb = torch.zeros(1, 1, talker.config.hidden_size, dtype=torch.bfloat16, device=talker.device)

    def t28():
        talker.model(inputs_embeds=emb, past_key_values=DynamicCache(), use_cache=True,
                     cache_position=torch.tensor([0], device=talker.device))
    micro["talker_model_28layer_step_ms"] = median_of(t28)

    pdc = predictor.device

    def p5():
        predictor.model(inputs_embeds=torch.zeros(1, 1, 1024, dtype=torch.bfloat16, device=pdc),
                        past_key_values=DynamicCache(), use_cache=True,
                        cache_position=torch.tensor([0], device=pdc))
    micro["predictor_model_5layer_step_ms"] = median_of(p5)
except Exception as e:
    micro["model_micro_err"] = repr(e)

print("MICRO:", json.dumps(micro, ensure_ascii=False, indent=2), flush=True)

if "talker_step_eager_ms(incl predictor15)" in micro:
    frames_med = statistics.median([r["frames"] for r in runs])
    prefill_med = statistics.median([r["prefill_s"] for r in runs])
    micro["theoretical_floor_s"] = round(
        prefill_med + frames_med * micro["talker_step_eager_ms(incl predictor15)"] / 1000, 2)

result = {
    "target_text": TARGET_TEXT,
    "prompt_build_s": round(t_prompt, 2),
    "runs": runs,
    "micro": micro,
    "gpu_mem_mb": round(torch.cuda.max_memory_allocated() / 1024 / 1024),
}
with open(OUT_JSON, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(f"saved -> {OUT_JSON}", flush=True)
