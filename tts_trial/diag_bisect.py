"""二分定位泄漏：mask 构造 / rope / 单层 attention / 完整 model.forward。"""
import functools

import torch
from transformers import DynamicCache, StaticCache

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_AUDIO = r"D:/变声/tts_models/ref/meituan_rat_002.wav"

from qwen_tts import Qwen3TTSModel


def mem(tag):
    torch.cuda.synchronize()
    return torch.cuda.memory_allocated() / 2**30


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
cache_pos = torch.tensor([L], device=DEV)
text_pos_ids = cache_pos.view(1, 1, -1).expand(3, 1, -1)

from transformers.masking_utils import create_causal_mask

tmodel = talker.model


def report(tag, fn, reps=5):
    fn()  # warmup(初始化)
    torch.cuda.synchronize()
    base = mem(tag)
    for _ in range(reps):
        fn()
    torch.cuda.synchronize()
    print(f"{tag:34s} delta/step = {(mem(tag)-base)/reps*1024:8.1f} MB", flush=True)


# 1) mask 构造
report("create_causal_mask", lambda: create_causal_mask(
    config=tmodel.config, input_embeds=frame_emb, attention_mask=None,
    cache_position=cache_pos, past_key_values=st, position_ids=text_pos_ids[0]))

# 2) rope
report("rotary_emb", lambda: tmodel.rotary_emb(frame_emb, text_pos_ids))

# 3) 单层 attention（带 mask + StaticCache）
pos_emb = tmodel.rotary_emb(frame_emb, text_pos_ids)
attn = tmodel.layers[0].self_attn
mk = create_causal_mask(config=tmodel.config, input_embeds=frame_emb,
                        attention_mask=None, cache_position=cache_pos,
                        past_key_values=st, position_ids=text_pos_ids[0])
report("single attn (mask+cache)", lambda: attn(
    frame_emb, pos_emb, mk, st, cache_pos))

# 3b) 单层 attention 无 mask
report("single attn (mask=None)", lambda: attn(
    frame_emb, pos_emb, None, st, cache_pos))

# 4) 单层 decoder layer
layer0 = tmodel.layers[0]
report("single decoder layer", lambda: layer0(
    frame_emb, attention_mask=mk, position_ids=text_pos_ids[0],
    past_key_values=st, output_attentions=False, use_cache=True,
    cache_position=cache_pos, position_embeddings=pos_emb))

# 5) 完整 model.forward
report("full model.forward", lambda: tmodel(
    inputs_embeds=frame_emb, past_key_values=st, use_cache=True,
    cache_position=cache_pos, output_attentions=False, output_hidden_states=False))

# 6) sdpa 直接调用（隔离 kernel 本身）
q = torch.randn(1, 16, 1, 128, dtype=torch.bfloat16, device=DEV)
kk = st.layers[0].keys
vv = st.layers[0].values
m4 = torch.zeros(1, 1, 1, 2048, dtype=torch.bool, device=DEV)
report("raw sdpa (mask, no gqa)", lambda: torch.nn.functional.scaled_dot_product_attention(
    q, kk, vv, attn_mask=m4))
report("raw sdpa (mask, gqa)", lambda: torch.nn.functional.scaled_dot_product_attention(
    q, kk, vv, attn_mask=m4, enable_gqa=True))
report("raw sdpa (no mask, gqa)", lambda: torch.nn.functional.scaled_dot_product_attention(
    q, kk, vv, enable_gqa=True))
