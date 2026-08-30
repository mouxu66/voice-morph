"""组合二分：层数 × mask 来源，复刻 talker.model.forward。"""
import functools

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
pos_ids3 = cache_pos.view(1, 1, -1).expand(3, 1, -1)
mk_fixed = create_causal_mask(config=tmodel.config, input_embeds=frame_emb,
                              attention_mask=None, cache_position=cache_pos,
                              past_key_values=st, position_ids=pos_ids3[0])


def my_forward(n_layers, fresh_mask, fresh_rope, no_mask=False):
    mask = None
    if not no_mask:
        mask = (create_causal_mask(config=tmodel.config, input_embeds=frame_emb,
                                   attention_mask=None, cache_position=cache_pos,
                                   past_key_values=st, position_ids=pos_ids3[0])
                if fresh_mask else mk_fixed)
    pos_emb = (tmodel.rotary_emb(frame_emb, pos_ids3) if fresh_rope else PE_FIXED)
    h = frame_emb
    for lyr in tmodel.layers[:n_layers]:
        lo = lyr(h, attention_mask=mask, position_ids=pos_ids3[0],
                 past_key_values=st, output_attentions=False, use_cache=True,
                 cache_position=cache_pos, position_embeddings=pos_emb)
        h = lo[0]
    return h


PE_FIXED = tmodel.rotary_emb(frame_emb, pos_ids3)


def report(tag, fn, reps=4):
    fn()
    torch.cuda.synchronize()
    base = torch.cuda.memory_allocated()
    for _ in range(reps):
        r = fn()
        del r
    torch.cuda.synchronize()
    d = (torch.cuda.memory_allocated() - base) / reps / 2**20
    print(f"{tag:44s} {d:8.1f} MB/step", flush=True)


report("1 layer, fixed mask, fixed rope", lambda: my_forward(1, False, False))
report("2 layers, fresh mask, fresh rope", lambda: my_forward(2, True, True))
report("7 layers, fresh mask, fresh rope", lambda: my_forward(7, True, True))
report("28 layers, fixed mask, fixed rope", lambda: my_forward(28, False, False))
report("28 layers, fresh mask, fixed rope", lambda: my_forward(28, True, False))
report("28 layers, fresh mask, fresh rope", lambda: my_forward(28, True, True))
report("28 layers, NO mask, fixed rope", lambda: my_forward(28, False, False, no_mask=True))
report("model.forward (attention_mask=None)", lambda: tmodel(
    inputs_embeds=frame_emb, past_key_values=st, use_cache=True,
    cache_position=cache_pos, output_attentions=False, output_hidden_states=False))
print("mask type:", type(mk_fixed), getattr(mk_fixed, "shape", None),
      getattr(mk_fixed, "dtype", None), flush=True)
