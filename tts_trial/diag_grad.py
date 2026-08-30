"""验证泄漏 = autograd 保存算子输入。在 attention 内部打印 grad 状态。"""
import functools

import torch
from transformers import DynamicCache, StaticCache

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

PRINTED = [False]
_orig_sdpa = ALL_ATTENTION_FUNCTIONS["sdpa"]


def probe_variant(module, query, key, value, attention_mask, dropout=0.0,
                  scaling=None, is_causal=None, **kwargs):
    if not PRINTED[0]:
        PRINTED[0] = True
        print(f"grad_enabled={torch.is_grad_enabled()} "
              f"inference_mode={torch.is_inference_mode_enabled()}", flush=True)
        print(f"query: req={query.requires_grad} fn={query.grad_fn}", flush=True)
        print(f"key:   req={key.requires_grad} fn={key.grad_fn}", flush=True)
        print(f"cache: req={st.layers[0].keys.requires_grad} "
              f"fn={st.layers[0].keys.grad_fn}", flush=True)
        print(f"weight req={module.q_proj.weight.requires_grad}", flush=True)
    return _orig_sdpa(module, query, key, value, attention_mask,
                      dropout=dropout, scaling=scaling, is_causal=is_causal,
                      **kwargs)


ALL_ATTENTION_FUNCTIONS["sdpa"] = probe_variant


def fwd():
    return tmodel(inputs_embeds=frame_emb, past_key_values=st, use_cache=True,
                  cache_position=cache_pos, output_attentions=False,
                  output_hidden_states=False)


fwd()
torch.cuda.synchronize()
base = torch.cuda.memory_allocated() / 2**20
for _ in range(3):
    r = fwd()
    del r
torch.cuda.synchronize()
print(f"leak = {(torch.cuda.memory_allocated() / 2**20 - base) / 3:.1f} MB/step",
      flush=True)

import gc

n = fn_n = 0
for o in gc.get_objects():
    try:
        if (isinstance(o, torch.Tensor) and o.is_cuda and o.dim() == 4
                and o.shape[1] == 16 and o.shape[2] == 2048):
            n += 1
            if o.grad_fn is not None:
                fn_n += 1
    except Exception:
        pass
print(f"leaked 4d tensors: {n}, with grad_fn: {fn_n}", flush=True)
if n:
    t = [o for o in gc.get_objects()
         if isinstance(o, torch.Tensor) and o.is_cuda and o.dim() == 4
         and o.shape[1] == 16 and o.shape[2] == 2048][0]
    print(f"sample grad_fn: {t.grad_fn}", flush=True)
