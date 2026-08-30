"""隔离泄漏持有者：逐段替换 attention 路径（repeat_kv / SDPA / 两者）。"""
import functools

import torch
from transformers import DynamicCache, StaticCache
from transformers.integrations.sdpa_attention import repeat_kv
from transformers.masking_utils import create_causal_mask

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_AUDIO = r"D:/变声/tts_models/ref/meituan_rat_002.wav"

from qwen_tts import Qwen3TTSModel

model = Qwen3TTSModel.from_pretrained(MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)
talker = model.model.talker
DEV = talker.device
TC = talker.config
tmodel = talker.model
torch.inference_mode().__enter__()

from transformers.modeling_utils import ALL_ATTENTION_FUNCTIONS

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
mk = create_causal_mask(config=tmodel.config, input_embeds=frame_emb,
                        attention_mask=None, cache_position=cache_pos,
                        past_key_values=st, position_ids=cache_pos.view(1, 1, -1)[0])
print(f"mask: {type(mk)} shape={getattr(mk, 'shape', None)} dtype={getattr(mk, 'dtype', None)}",
      flush=True)


def fwd():
    return tmodel(inputs_embeds=frame_emb, past_key_values=st, use_cache=True,
                  cache_position=cache_pos, output_attentions=False,
                  output_hidden_states=False)


def census1620():
    import gc

    n = 0
    for o in gc.get_objects():
        try:
            if (isinstance(o, torch.Tensor) and o.is_cuda and o.dim() == 4
                    and o.shape[1] == 16 and o.shape[2] == 2048):
                n += 1
        except Exception:
            pass
    return n


def measure(tag, reps=3):
    fwd()
    torch.cuda.synchronize()
    base = torch.cuda.memory_allocated() / 2**20
    c0 = census1620()
    for _ in range(reps):
        r = fwd()
        del r
    torch.cuda.synchronize()
    d = (torch.cuda.memory_allocated() / 2**20 - base) / reps
    print(f"{tag:38s} {d:8.1f} MB/step   big4d={c0}->{census1620()}", flush=True)


measure("V0 control (real sdpa)")

_orig_sdpa = ALL_ATTENTION_FUNCTIONS["sdpa"]


def mk_variant(do_repeat_kv, do_sdpa):
    def variant(module, query, key, value, attention_mask, dropout=0.0,
                scaling=None, is_causal=None, **kwargs):
        if attention_mask is not None and attention_mask.ndim == 4:
            attention_mask = attention_mask[:, :, :, : key.shape[-2]]
        sdpa_kwargs = {}
        if do_repeat_kv:
            g = getattr(module, "num_key_value_groups", 1)
            if g > 1:
                key = repeat_kv(key, g)
                value = repeat_kv(value, g)
        else:
            sdpa_kwargs["enable_gqa"] = True
        if do_sdpa:
            return torch.nn.functional.scaled_dot_product_attention(
                query, key, value, attn_mask=attention_mask,
                dropout_p=dropout, scale=scaling, **sdpa_kwargs
            ).transpose(1, 2).contiguous(), None
        return torch.zeros_like(query).transpose(1, 2).contiguous(), None

    return variant


ALL_ATTENTION_FUNCTIONS["sdpa"] = mk_variant(False, False)
measure("V1 no repeat_kv, no sdpa")

ALL_ATTENTION_FUNCTIONS["sdpa"] = mk_variant(True, False)
measure("V2 repeat_kv only, no sdpa")

ALL_ATTENTION_FUNCTIONS["sdpa"] = mk_variant(False, True)
measure("V3 sdpa only (gqa, no repeat_kv)")

ALL_ATTENTION_FUNCTIONS["sdpa"] = mk_variant(True, True)
measure("V4 repeat_kv + sdpa (reconstructed)")

ALL_ATTENTION_FUNCTIONS["sdpa"] = _orig_sdpa
measure("V5 restore orig sdpa")

# --- SDPA 后端开关实验 ---
bk = torch.backends.cuda
print(f"\nbackends: flash={bk.flash_sdp_enabled()} "
      f"mem_eff={bk.mem_efficient_sdp_enabled()} "
      f"math={bk.math_sdp_enabled()} "
      f"cudnn={getattr(bk, 'cudnn_sdp_enabled', lambda: 'n/a')()}", flush=True)

bk.enable_flash_sdp(False)
bk.enable_mem_efficient_sdp(False)
bk.enable_cudnn_sdp(False)
bk.enable_math_sdp(True)
measure("V6 math only")

bk.enable_math_sdp(False)
bk.enable_cudnn_sdp(True)
measure("V7 cudnn only")

bk.enable_cudnn_sdp(False)
bk.enable_mem_efficient_sdp(True)
measure("V8 mem_efficient only")

bk.enable_mem_efficient_sdp(False)
bk.enable_flash_sdp(True)
measure("V9 flash only (expect fallback err)")

bk.enable_mem_efficient_sdp(True)
bk.enable_math_sdp(True)
