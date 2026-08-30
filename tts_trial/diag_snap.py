"""用 record_memory_history 抓保留张量的分配栈，定位 (N-1)×16.8MB 泄漏源。"""
import functools
from collections import defaultdict

import torch
from transformers import DynamicCache, StaticCache
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


fwd()  # warmup
torch.cuda.synchronize()
base = torch.cuda.memory_allocated() / 2**30
print(f"baseline alloc = {base:.3f} GB", flush=True)

torch.cuda.memory._record_memory_history(max_entries=100000)
for _ in range(3):
    r = fwd()
    del r
torch.cuda.synchronize()
torch.cuda.memory._record_memory_history(enabled=False)
cur = torch.cuda.memory_allocated() / 2**30
print(f"after 3 fwd    = {cur:.3f} GB  (delta {(cur - base) * 1024:.1f} MB)",
      flush=True)

snap = torch.cuda.memory._snapshot()
agg = defaultdict(lambda: [0, 0])
n_active = n_inactive = 0
for seg in snap["segments"]:
    for blk in seg["blocks"]:
        if blk["state"] == "active":
            n_active += 1
        else:
            n_inactive += 1
        if blk["state"] != "active":
            continue
        frames = blk.get("history", {}).get("frames") or []
        key = tuple((f.get("filename", "?").split("\\")[-1].split("/")[-1],
                     f.get("line")) for f in frames[:4])
        agg[key][0] += blk["size"]
        agg[key][1] += 1
print(f"snapshot blocks: active={n_active} inactive={n_inactive} "
      f"seg_sum_active={sum(v[0] for v in agg.values()) / 2**20:.0f} MB",
      flush=True)
rows = sorted(agg.items(), key=lambda kv: -kv[1][0])
print("\n=== live blocks by allocation site (top 15) ===")
for key, (size, cnt) in rows[:15]:
    print(f"{size / 2**20:9.1f} MB  x{cnt:4d}  {key}", flush=True)

# gc 普查：所有 Python 可达的 CUDA 张量，按 shape 聚合
print("\n=== gc CUDA tensor census (>= 4 MB groups) ===")
import gc

census = defaultdict(lambda: [0, 0, None])
for obj in gc.get_objects():
    try:
        if not isinstance(obj, torch.Tensor) or not obj.is_cuda:
            continue
        key = (tuple(obj.shape), str(obj.dtype))
        census[key][0] += obj.numel() * obj.element_size()
        census[key][1] += 1
    except Exception:
        pass
for key, (size, cnt, _) in sorted(census.items(), key=lambda kv: -kv[1][0]):
    if size >= 4 * 2**20:
        print(f"{size / 2**20:9.1f} MB  x{cnt:4d}  {key}", flush=True)

# 找出大张量的引用者
print("\n=== referrers of 16.8MB-class tensors ===")
seen_ref = set()
for obj in gc.get_objects():
    try:
        if (isinstance(obj, torch.Tensor) and obj.is_cuda
                and obj.numel() * obj.element_size() > 8 * 2**20):
            refs = []
            for r in gc.get_referrers(obj):
                tn = type(r).__name__
                if tn in ("list", "tuple", "dict", "set"):
                    continue
                desc = f"{tn}"
                if hasattr(r, "__class__") and r.__class__.__module__ != "builtins":
                    desc += f"<{r.__class__.__module__}.{r.__class__.__name__}>"
                if desc not in seen_ref:
                    seen_ref.add(desc)
                    refs.append(desc)
            if refs:
                print(f"{tuple(obj.shape)} <- {refs[:6]}", flush=True)
    except Exception:
        pass
