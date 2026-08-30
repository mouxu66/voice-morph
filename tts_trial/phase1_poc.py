"""Phase 1a + 1b PoC：自定义 decode 循环（StaticCache）+ 整帧 CUDA Graph。

运行：
    D:/变声/tts_trial/venv312/Scripts/python.exe D:/变声/tts_trial/phase1_poc.py

内容：
  0. hook talker.generate 抓取真实 prefill 入参 + 原版 codes（贪心）
  1. Phase 1a：手写帧循环（predictor 15 步 + talker 1 步，全 StaticCache，
     attention_mask=None / position_ids=None，batch=1 无 padding 时与原版数值等价）
  2. Phase 1b：整帧捕获为单张 CUDA Graph（静态 buffer 输入输出，图内 argmax），
     replay 循环计时
  3. 正确性：贪心模式下「原版 codes == eager codes == graph codes」逐 token 对比
  4. 计时：原版(采样/贪心) vs eager(采样) vs graph(贪心)

帧结构（modeling_qwen3_tts.py:1668-1692）：
  past_hidden + tok_embed -> predictor prefill(2) + 14 步 decode -> 15 codes
  codes 16 个 embedding sum + trailing_text/pad -> talker forward(1 tok) -> logits
"""
import functools
import json
import statistics
import time

import soundfile as sf
import torch
from transformers import StaticCache

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_AUDIO = r"D:/变声/tts_models/ref/meituan_rat_002.wav"
TARGET_TEXT = "巴黎罗，我要掏你炉子了，今天不给你送外卖，你的奶茶已经凉了，麻烦你下楼取一下。"
OUT_DIR = r"D:/变声/outputs"
MAX_CACHE = 2048
MAX_FRAMES = 400
TEMP = 0.9
TOP_K = 50
REPORT = r"D:/变声/tts_trial/phase1_poc_result.json"

from qwen_tts import Qwen3TTSModel

print("loading model ...", flush=True)
model = Qwen3TTSModel.from_pretrained(MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)
inner = model.model
talker = inner.talker
predictor = talker.code_predictor
DEV = talker.device
TC = talker.config
PC = predictor.config
VOCAB = TC.vocab_size
EOS_ID = TC.codec_eos_token_id
NG = TC.num_code_groups
MIN_NEW = 2

SUPPRESS_IDX = torch.tensor(
    [i for i in range(VOCAB - 1024, VOCAB) if i != EOS_ID], device=DEV)
SUPPRESS_MASK = torch.zeros(VOCAB, dtype=torch.bool, device=DEV)
SUPPRESS_MASK[SUPPRESS_IDX] = True
EOS_ONLY_MASK = torch.zeros(VOCAB, dtype=torch.bool, device=DEV)
EOS_ONLY_MASK[EOS_ID] = True
NEG_INF = float("-inf")

CP_PRED_PREFILL = torch.arange(2, device=DEV)
CP_PRED = [torch.tensor([p], device=DEV) for p in range(2, NG + 1)]  # decode 位置 2..16

# ---------------- 抓包 ----------------
CAP = {}
_orig_talker_gen = talker.generate


@functools.wraps(_orig_talker_gen)
def _hook_gen(*a, **kw):
    CAP["prefill_embeds"] = kw["inputs_embeds"].clone()
    CAP["prefill_mask"] = kw["attention_mask"].clone()
    CAP["trailing"] = kw["trailing_text_hidden"].clone()
    CAP["pad"] = kw["tts_pad_embed"].clone()
    out = _orig_talker_gen(*a, **kw)
    CAP["codes"] = torch.stack(
        [h[-1] for h in out.hidden_states if h[-1] is not None], dim=1).clone()
    return out


talker.generate = _hook_gen

# ---------------- 采样器（张量化，可入图） ----------------

def sample_talker_logits(logits, greedy, eos_allowed):
    """logits (1,V) float；eos_allowed: 0-dim bool tensor（min_new_tokens 控制）。"""
    logits = logits.masked_fill(SUPPRESS_MASK, NEG_INF)
    logits = logits.masked_fill(EOS_ONLY_MASK & ~eos_allowed, NEG_INF)
    if greedy:
        return logits.argmax(-1, keepdim=True)
    logits = logits / TEMP
    v, idx = torch.topk(logits, TOP_K, dim=-1)
    p = torch.softmax(v, -1)
    return idx.gather(-1, torch.multinomial(p, 1))


def sample_pred_logits(logits, greedy):
    if greedy:
        return logits.argmax(-1, keepdim=True)
    logits = logits / TEMP
    v, idx = torch.topk(logits, TOP_K, dim=-1)
    p = torch.softmax(v, -1)
    return idx.gather(-1, torch.multinomial(p, 1))


# ---------------- 帧逻辑（eager / graph 共用 body） ----------------

def run_frame(tok, past_hidden, text_embed, t_cp, talker_cache, pred_cache,
              greedy, eos_allowed):
    """一帧 = predictor prefill(2)+14 步 decode + talker 1 步。

    tok (1,1) long；past_hidden (1,1,H)；text_embed (1,1,H)；t_cp (1,) long
    返回 (next_tok (1,1), new_hidden (1,1,H), codes (1,NG))
    """
    last_id_hidden = talker.model.codec_embedding(tok)          # (1,1,H)
    pin = torch.cat((past_hidden, last_id_hidden), dim=1)       # (1,2,H)
    out = predictor.forward(
        inputs_embeds=pin, past_key_values=pred_cache, use_cache=True,
        cache_position=CP_PRED_PREFILL, generation_steps=0,
        output_attentions=False, output_hidden_states=False)
    c = [tok, sample_pred_logits(out.logits[:, -1, :], greedy)]
    for i in range(1, NG - 1):                                   # 14 步 decode
        out = predictor.forward(
            input_ids=c[i], generation_steps=i, past_key_values=pred_cache,
            use_cache=True, cache_position=CP_PRED[i - 1],
            output_attentions=False, output_hidden_states=False)
        c.append(sample_pred_logits(out.logits[:, -1, :], greedy))
    codes = torch.cat(c, dim=-1)                                 # (1,NG)
    codec_hiddens = torch.cat(
        [last_id_hidden]
        + [predictor.model.codec_embedding[j](codes[:, j + 1:j + 2]) for j in range(NG - 1)],
        dim=1)                                                   # (1,NG,H)
    frame_emb = codec_hiddens.sum(dim=1, keepdim=True) + text_embed
    out = talker.model(
        inputs_embeds=frame_emb, past_key_values=talker_cache, use_cache=True,
        cache_position=t_cp, output_attentions=False, output_hidden_states=False)
    h = out.last_hidden_state                                    # (1,1,H)
    logits = talker.codec_head(h)[:, 0, :]                       # (1,V)
    next_tok = sample_talker_logits(logits.float(), greedy, eos_allowed)
    return next_tok, h, codes


def run_prefill(talker_cache):
    """用抓包的 prefill 输入跑一次 talker prefill（eager，写 StaticCache 0..L-1）。"""
    L = CAP["prefill_embeds"].shape[1]
    out = talker.forward(
        inputs_embeds=CAP["prefill_embeds"], attention_mask=CAP["prefill_mask"],
        past_key_values=talker_cache, trailing_text_hidden=CAP["trailing"],
        tts_pad_embed=CAP["pad"], generation_step=-1, use_cache=True,
        output_hidden_states=False, cache_position=torch.arange(L, device=DEV))
    return out.logits[:, -1, :].float(), out.past_hidden, L


def text_embed_at(step):
    T = CAP["trailing"].shape[1]
    if step < T:
        return CAP["trailing"][0, step].view(1, 1, -1)
    return CAP["pad"]


# ---------------- eager 自定义循环 ----------------

def run_eager_loop(greedy, collect_codes=True):
    talker_cache = StaticCache(config=TC, max_cache_len=MAX_CACHE)
    pred_cache = StaticCache(config=PC, max_cache_len=NG + 1)
    logits0, past_hidden, L = run_prefill(talker_cache)
    tok = sample_talker_logits(logits0, greedy, torch.tensor(False, device=DEV))
    codes_all, step = [], 0
    while step < MAX_FRAMES:
        # 本帧产出第 step+1 个 token；HF min_new_tokens 在 cur_len>=min 时即允许 eos
        ea = torch.tensor(step + 1 >= MIN_NEW, device=DEV)
        t_cp = torch.tensor([L + step], device=DEV)
        next_tok, past_hidden, codes = run_frame(
            tok, past_hidden, text_embed_at(step), t_cp,
            talker_cache, pred_cache, greedy, ea)
        if collect_codes:
            codes_all.append(codes)
        step += 1
        if next_tok.item() == EOS_ID:
            break
        tok = next_tok
    return (torch.cat(codes_all, dim=0) if collect_codes else None), step


# ---------------- 整帧 CUDA Graph ----------------

class GraphEngine:
    def __init__(self):
        H = TC.hidden_size
        self.tok_in = torch.zeros(1, 1, dtype=torch.long, device=DEV)
        self.past_hidden_in = torch.zeros(1, 1, H, dtype=torch.bfloat16, device=DEV)
        self.text_embed_in = torch.zeros(1, 1, H, dtype=torch.bfloat16, device=DEV)
        self.t_cp_in = torch.zeros(1, dtype=torch.long, device=DEV)
        self.eos_allowed = torch.zeros((), dtype=torch.bool, device=DEV)
        self.codes_out = torch.zeros(1, NG, dtype=torch.long, device=DEV)
        self.talker_cache = StaticCache(config=TC, max_cache_len=MAX_CACHE)
        self.pred_cache = StaticCache(config=PC, max_cache_len=NG + 1)
        self.graph = None

    def body(self):
        next_tok, h, codes = run_frame(
            self.tok_in, self.past_hidden_in, self.text_embed_in, self.t_cp_in,
            self.talker_cache, self.pred_cache, True, self.eos_allowed)
        self.codes_out.copy_(codes)
        self.tok_in.copy_(next_tok)
        self.past_hidden_in.copy_(h)

    def capture(self):
        s = torch.cuda.Stream()
        s.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(s):
            for _ in range(3):
                self.body()
        torch.cuda.current_stream().wait_stream(s)
        torch.cuda.synchronize()
        g = torch.cuda.CUDAGraph()
        with torch.cuda.graph(g):
            self.body()
        self.graph = g


def run_graph_loop(eng, greedy_collect=True):
    """每次 run 前重新 prefill（覆写静态 cache），再 replay 帧循环。"""
    logits0, past_hidden0, L = run_prefill(eng.talker_cache)
    eng.tok_in.copy_(sample_talker_logits(logits0, True, torch.tensor(False, device=DEV)))
    eng.past_hidden_in.copy_(past_hidden0)
    codes_all, step = [], 0
    while step < MAX_FRAMES:
        eng.text_embed_in.copy_(text_embed_at(step))
        eng.t_cp_in.fill_(L + step)
        eng.eos_allowed.fill_(step + 1 >= MIN_NEW)
        eng.graph.replay()
        if greedy_collect:
            codes_all.append(eng.codes_out.clone())
        step += 1
        if eng.tok_in.item() == EOS_ID:
            break
    return (torch.cat(codes_all, dim=0) if greedy_collect else None), step


# ---------------- 主流程 ----------------
# 原版 generate_voice_clone 自带 @torch.inference_mode()；自定义循环同样必须在
# inference mode 下跑。注意：必须持有 CM 对象引用，写成
# `torch.inference_mode().__enter__()` 会在语句结束时立刻析构并退出模式，
# 导致 autograd 记录 StaticCache 的 in-place index_copy_，图永久累积直接爆显存。
IM = torch.inference_mode()
IM.__enter__()

print("building x-vector prompt ...", flush=True)
prompt = model.create_voice_clone_prompt(ref_audio=REF_AUDIO, ref_text=".",
                                         x_vector_only_mode=True)

result = {"target_text": TARGET_TEXT}

# --- A. 原版贪心（抓包 + codes 基准） ---
print("[A] baseline greedy generate (captures prefill kwargs) ...", flush=True)
torch.cuda.synchronize()
t0 = time.perf_counter()
_ = model.generate_voice_clone(text=[TARGET_TEXT], language=["Chinese"],
                               voice_clone_prompt=prompt,
                               do_sample=False, subtalker_dosample=False,
                               repetition_penalty=1.0)
torch.cuda.synchronize()
result["orig_greedy_s"] = round(time.perf_counter() - t0, 2)
codes_ref = CAP["codes"][0]
result["orig_greedy_frames"] = int(codes_ref.shape[0])
print(f"    {result['orig_greedy_s']}s, frames={result['orig_greedy_frames']}", flush=True)

# --- B. Phase 1a：eager 贪心对比 ---
print("[B] custom eager greedy loop (correctness vs baseline) ...", flush=True)
codes_eager, n1 = run_eager_loop(greedy=True)
match_eager = bool(torch.equal(codes_eager, codes_ref))
result["eager_greedy_codes_match"] = match_eager
result["eager_greedy_frames"] = n1
print(f"    match={match_eager}, frames={n1}", flush=True)
if not match_eager:
    n = min(codes_eager.shape[0], codes_ref.shape[0])
    diff = (codes_eager[:n] != codes_ref[:n]).nonzero()
    print(f"    first diff at {diff[:5].tolist() if len(diff) else 'len mismatch'}", flush=True)

# --- C. Phase 1b：graph 贪心对比 ---
print("[C] CUDA Graph capture + greedy replay ...", flush=True)
eng = GraphEngine()
eng.capture()
codes_graph, n2 = run_graph_loop(eng)
match_graph = bool(torch.equal(codes_graph, codes_ref))
result["graph_greedy_codes_match"] = match_graph
result["graph_greedy_frames"] = n2
print(f"    match(orig)={match_graph}, frames={n2}", flush=True)
# 图捕获正确性判据：graph replay 应逐位复现 eager 自定义循环
if codes_graph.shape == codes_eager.shape:
    result["graph_vs_eager_match"] = bool(torch.equal(codes_graph, codes_eager))
else:
    n = min(codes_graph.shape[0], codes_eager.shape[0])
    result["graph_vs_eager_match"] = bool(torch.equal(codes_graph[:n], codes_eager[:n]))
print(f"    match(eager)={result['graph_vs_eager_match']}", flush=True)

# --- D. 计时 ---
print("[D] timing ...", flush=True)


def time_it(fn, reps=3):
    ts = []
    for _ in range(reps):
        torch.cuda.synchronize()
        t0 = time.perf_counter()
        out = fn()
        torch.cuda.synchronize()
        ts.append(time.perf_counter() - t0)
    return statistics.median(ts), out


t_eager_s, _ = time_it(lambda: run_eager_loop(greedy=False, collect_codes=False))
result["custom_eager_sampling_s"] = round(t_eager_s, 2)
t_graph_s, _ = time_it(lambda: run_graph_loop(eng, greedy_collect=False))
result["graph_greedy_s"] = round(t_graph_s, 2)

torch.cuda.synchronize()
t0 = time.perf_counter()
_ = model.generate_voice_clone(text=[TARGET_TEXT], language=["Chinese"],
                               voice_clone_prompt=prompt)
torch.cuda.synchronize()
result["orig_sampling_s"] = round(time.perf_counter() - t0, 2)

frames = result["orig_greedy_frames"]
result["per_frame_ms"] = {
    "orig_sampling": round(result["orig_sampling_s"] * 1000 / frames, 1),
    "custom_eager_sampling": round(t_eager_s * 1000 / frames, 1),
    "graph_greedy": round(t_graph_s * 1000 / frames, 1),
}
result["speedup"] = {
    "eager_vs_orig": round(result["orig_sampling_s"] / t_eager_s, 2),
    "graph_vs_orig": round(result["orig_sampling_s"] / t_graph_s, 2),
    "graph_vs_eager": round(t_eager_s / t_graph_s, 2),
}
print(json.dumps({k: result[k] for k in
                  ("orig_sampling_s", "custom_eager_sampling_s", "graph_greedy_s",
                   "per_frame_ms", "speedup")}, indent=2), flush=True)

# --- E. 输出音频（采样模式 eager + 贪心 graph） ---
codes_s, _ = run_eager_loop(greedy=True)
wavs, sr = model.model.speech_tokenizer.decode([{"audio_codes": codes_s}])
sf.write(rf"{OUT_DIR}/phase1_eager_greedy.wav", wavs[0], sr)
wavs, sr = model.model.speech_tokenizer.decode([{"audio_codes": codes_graph}])
sf.write(rf"{OUT_DIR}/phase1_graph_greedy.wav", wavs[0], sr)
codes_sm, _ = run_eager_loop(greedy=False)
wavs, sr = model.model.speech_tokenizer.decode([{"audio_codes": codes_sm}])
sf.write(rf"{OUT_DIR}/phase1_eager_sampling.wav", wavs[0], sr)
result["audio_s"] = round(len(wavs[0]) / sr, 2)
result["rtf"] = {
    "orig": round(result["audio_s"] / result["orig_sampling_s"], 3),
    "eager": round(result["audio_s"] / t_eager_s, 3),
    "graph": round(result["audio_s"] / t_graph_s, 3),
}

result["gpu_mem_mb"] = round(torch.cuda.max_memory_allocated() / 1024 / 1024)
with open(REPORT, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
print(f"saved -> {REPORT}", flush=True)
