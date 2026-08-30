"""定位 talker decode 每步 +430MB 泄漏的真凶。"""
import functools
import gc

import torch
from transformers import DynamicCache, StaticCache

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_AUDIO = r"D:/变声/tts_models/ref/meituan_rat_002.wav"
TARGET_TEXT = "你好。"

from qwen_tts import Qwen3TTSModel


def mem(tag):
    torch.cuda.synchronize()
    print(f"[mem] {tag:40s} alloc={torch.cuda.memory_allocated()/2**30:.3f}GB", flush=True)


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
_ = model.generate_voice_clone(text=[TARGET_TEXT], language=["Chinese"],
                               voice_clone_prompt=prompt, do_sample=False,
                               subtalker_dosample=False, repetition_penalty=1.0)
L = CAP["prefill_embeds"].shape[1]
print(f"L={L}", flush=True)

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
gc.collect()
mem("baseline (static cache ready)")

frame_emb = torch.zeros(1, 1, TC.hidden_size, dtype=torch.bfloat16, device=DEV)

# --- 实验1：原样 3 步 + gc ---
for step in range(3):
    t = talker.model(inputs_embeds=frame_emb, past_key_values=st, use_cache=True,
                     cache_position=torch.tensor([L + step], device=DEV),
                     output_attentions=False, output_hidden_states=False)
    del t
    gc.collect()
    mem(f"step{step} (gc'd)")

# --- 实验2：不看 mask，直接测 repeat_kv 语义的泄漏 ---
k = torch.randn(1, 8, 2048, 128, dtype=torch.bfloat16, device=DEV)
for i in range(3):
    x = k[:, :, None, :, :].expand(1, 8, 2, 2048, 128).reshape(1, 16, 2048, 128)
    del x
    gc.collect()
    mem(f"repeat_kv sim {i}")

# --- 实验3：单层 attention 直接调 ---
attn = talker.model.layers[0].self_attn
pos_emb = (torch.randn(1, 1, 128, dtype=torch.bfloat16, device=DEV),
           torch.randn(1, 1, 128, dtype=torch.bfloat16, device=DEV))
mask = torch.zeros(1, 1, 1, 2048, dtype=torch.bool, device=DEV)
for i in range(3):
    o = attn(frame_emb, pos_emb, mask, st, torch.tensor([L + i], device=DEV))
    del o
    gc.collect()
    mem(f"single-attn {i}")

stats = torch.cuda.memory_stats()
print("active_bytes:", stats["allocated_bytes.all.current"] / 2**30, "GB", flush=True)
print("segment_cnt:", stats["segment.all.current"], flush=True)
print("num_alloc_retries:", stats.get("num_alloc_retries", 0), flush=True)
