"""用 memory_history 记录分配栈，抓泄漏真凶。"""
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

torch.cuda.synchronize()
torch.cuda.memory._record_memory_history(max_entries=100000)
base_alloc = torch.cuda.memory_allocated()

for step in range(3):
    t = talker.model(inputs_embeds=frame_emb, past_key_values=st, use_cache=True,
                     cache_position=torch.tensor([L + step], device=DEV),
                     output_attentions=False, output_hidden_states=False)
    del t
torch.cuda.synchronize()

snap = torch.cuda.memory._snapshot()
print(f"delta = {(torch.cuda.memory_allocated()-base_alloc)/2**30:.2f} GB", flush=True)

# 聚合 active 块的分配栈
from collections import Counter

stack_counter = Counter()
for seg in snap["segments"]:
    for blk in seg["blocks"]:
        if not blk.get("state", False) and blk.get("history", None):
            frames = blk["history"][0].get("frames", [])
            key = tuple((f["filename"].split("\\")[-1], f["line"])
                        for f in frames[:6])
            stack_counter[(blk["size"], key)] += 1

for (size, key), cnt in stack_counter.most_common(12):
    print(f"\n--- block {size/2**20:.1f}MB x{cnt}", flush=True)
    for f in key:
        print(f"    {f[0]}:{f[1]}", flush=True)
