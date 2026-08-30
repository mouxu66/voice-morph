"""验证修复方案：float bias / 无mask切片 / 手写matmul attention。
每个变体测 leak + 单步耗时。"""
import functools
import time

import torch
import torch.nn.functional as F
from transformers import DynamicCache, StaticCache
from transformers.integrations.sdpa_attention import repeat_kv
from transformers.masking_utils import create_causal_mask
from transformers.modeling_utils import ALL_ATTENTION_FUNCTIONS

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_AUDIO = r"D:/变声/tts_models/ref/meituan_rat_002.wav"

from qwen_tts import Qwen3TTSModel

model = Qwen3TTSModel.from_pretrained(MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)
talker = model.model.talker
DEV = talker.device
TC = talker.config
tmodel = talker.model
torch.inference_mode().__enter__()

CAP = {}
_orig_gen = talker.generate


@functools.wraps(_orig_gen)
def _hook(*a, **kw):
    CAP["prefill_embeds"] = kw["inputs_embeds"].clone()
    CAP["prefill_mask"] = kw["attention_mask"].clone()
    CAP["pad"] = kw["tts_pad_embed"].clone()
    CAP["trailing"] = kw["trailing_text_hidden"].clone()
    return _orig_gen(*a, **kw)


talker.generate = _hook
prompt = model.create_voice_clone_prompt(ref_audio=REF_AUDIO, ref_text=".",
                                         x_vector_only_mode=True)
_ = model.generate_voice_clone(text=["你好。"], language=["Chinese"],
                               voice_clone_prompt=prompt, do_sample=False,
                               subtalker_dosample=False, repetition_penalty=1.0)
L = CAP["prefill_embeds"].shape[1]
dyn = DynamicCache()
out = talker.forward(inputs_embeds=CAP["prefill_embeds"],
                     attention_mask=CAP["prefill_mask"], past_key_values=dyn,
                     trailing_text_hidden=CAP["trailing"], tts_pad_embed=CAP["pad"],
                     generation_step=-1, use_cache=True, output_hidden_states=False,
                     cache_position=torch.arange(L, device=DEV))
st = StaticCache(config=TC, max_cache_len=2048)
for i in range(TC.num_hidden_layers):
    layer = st.layers[i]
    k, v = dyn.layers[i].keys, dyn.layers[i].values
    if not layer.is_initialized:
        layer.lazy_initialization(k)
    layer.keys[:, :, :L].copy_(k)
    layer.values[:, :, :L].copy_(v)
del dyn, out

frame_emb = torch.zeros(1, 1, TC.hidden_size, dtype=torch.bfloat16, device=DEV)
cache_pos = torch.tensor([L], device=DEV)

# 持久 float bias：位置 >= 有效长度 处 -inf，其余 0
BIAS = torch.zeros(1, 1, 1, 2048, dtype=torch.bfloat16, device=DEV)
BIAS[..., L + 1:] = float("-inf")
BIAS[..., L] = 0.0  # 当前 token 可见自己

_orig_sdpa = ALL_ATTENTION_FUNCTIONS["sdpa"]


def bias_variant(module, query, key, value, attention_mask, dropout=0.0,
                 scaling=None, is_causal=None, **kwargs):
    g = getattr(module, "num_key_value_groups", 1)
    if g > 1:
        key = repeat_kv(key, g)
        value = repeat_kv(value, g)
    return F.scaled_dot_product_attention(
        query, key, value, attn_mask=BIAS, dropout_p=dropout, scale=scaling
    ).transpose(1, 2).contiguous(), None


def slice_variant(module, query, key, value, attention_mask, dropout=0.0,
                  scaling=None, is_causal=None, **kwargs):
    g = getattr(module, "num_key_value_groups", 1)
    if g > 1:
        key = repeat_kv(key, g)
        value = repeat_kv(value, g)
    return F.scaled_dot_product_attention(
        query, key, value, dropout_p=dropout, scale=scaling
    ).transpose(1, 2).contiguous(), None


def matmul_variant(module, query, key, value, attention_mask, dropout=0.0,
                   scaling=None, is_causal=None, **kwargs):
    g = getattr(module, "num_key_value_groups", 1)
    if g > 1:
        key = repeat_kv(key, g)
        value = repeat_kv(value, g)
    scores = torch.matmul(query, key.transpose(-1, -2)) * (scaling or 1.0)
    scores = scores + BIAS
    probs = torch.softmax(scores, dim=-1)
    o = torch.matmul(probs, value)
    return o.transpose(1, 2).contiguous(), None


def fwd():
    return tmodel(inputs_embeds=frame_emb, past_key_values=st, use_cache=True,
                  cache_position=cache_pos, output_attentions=False,
                  output_hidden_states=False)


def measure(tag, reps=3):
    fwd()
    torch.cuda.synchronize()
    base = torch.cuda.memory_allocated() / 2**20
    ts = []
    for _ in range(reps):
        t0 = time.perf_counter()
        r = fwd()
        torch.cuda.synchronize()
        ts.append(time.perf_counter() - t0)
        del r
    d = (torch.cuda.memory_allocated() / 2**20 - base) / reps
    print(f"{tag:34s} leak={d:7.1f} MB/step  t={min(ts) * 1000:6.1f} ms",
          flush=True)


# 参考数值：原版单步 forward 全程耗时
ALL_ATTENTION_FUNCTIONS["sdpa"] = matmul_variant
measure("D custom matmul + float bias")
ALL_ATTENTION_FUNCTIONS["sdpa"] = bias_variant
measure("A sdpa + float bias (repeat_kv)")
ALL_ATTENTION_FUNCTIONS["sdpa"] = slice_variant
measure("B sdpa no mask (repeat_kv, full)")
ALL_ATTENTION_FUNCTIONS["sdpa"] = _orig_sdpa
measure("C orig sdpa (bool mask, control)")

# 正确性：D vs C 在同一 cache 状态下的输出差
def fwd_hidden():
    return tmodel(inputs_embeds=frame_emb, past_key_values=st, use_cache=True,
                  cache_position=cache_pos, output_attentions=False,
                  output_hidden_states=False).last_hidden_state


ALL_ATTENTION_FUNCTIONS["sdpa"] = _orig_sdpa
ref = fwd_hidden().float()
ALL_ATTENTION_FUNCTIONS["sdpa"] = matmul_variant
got = fwd_hidden().float()
print(f"\nD vs C max abs diff = {(ref - got).abs().max().item():.6f}", flush=True)
ALL_ATTENTION_FUNCTIONS["sdpa"] = bias_variant
got2 = fwd_hidden().float()
print(f"A vs C max abs diff = {(ref - got2).abs().max().item():.6f}", flush=True)
