"""追 (1,16,2048,128) 保留张量的引用链，找持有者。"""
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
before = torch.cuda.memory_allocated() / 2**20
print(f"baseline={base:.0f}MB  after 3 fwd={before:.0f}MB", flush=True)

import gc

gc.collect()
torch.cuda.synchronize()
after_gc = torch.cuda.memory_allocated() / 2**20
print(f"after gc.collect()={after_gc:.0f}MB  (freed {before - after_gc:.0f}MB)",
      flush=True)


def desc_obj(o):
    t = type(o).__name__
    mod = getattr(o.__class__, "__module__", "")
    if mod and mod != "builtins":
        t = f"{mod}.{t}"
    if isinstance(o, (list, tuple)):
        return f"{t}[{len(o)}]"
    if isinstance(o, dict):
        ks = list(o.keys())[:4]
        return f"{t}({ks})"
    return t


targets = [o for o in gc.get_objects()
           if isinstance(o, torch.Tensor) and o.is_cuda
           and o.dim() == 4 and o.shape[0] == 1 and o.shape[1] == 16
           and o.shape[2] == 2048]
print(f"\nleaked repeat_kv tensors alive: {len(targets)}", flush=True)


def walk(o, depth, seen, path):
    if depth == 0 or id(o) in seen:
        return
    seen.add(id(o))
    try:
        refs = gc.get_referrers(o)
    except Exception:
        return
    for r in refs:
        d = desc_obj(r)
        print(f"{'  ' * depth}{d}", flush=True)
        if depth <= 3 and not isinstance(r, torch.Tensor):
            walk(r, depth + 1, seen, path + [d])


if targets:
    print("\n=== referrer chain of first leaked tensor ===", flush=True)
    walk(targets[0], 1, set(), [])
    # 汇总所有泄漏张量的直接引用者类型
    from collections import Counter

    c = Counter()
    for t in targets[:50]:
        for r in gc.get_referrers(t):
            c[desc_obj(r)] += 1
    print("\n=== direct referrer type histogram (first 50 tensors) ===", flush=True)
    for k, v in c.most_common(15):
        print(f"{v:5d}  {k}", flush=True)
