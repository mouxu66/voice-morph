"""验证全局 GQA patch 是否消除 talker decode 泄漏。"""
import functools

import torch
from transformers import DynamicCache, StaticCache
from transformers.modeling_utils import ALL_ATTENTION_FUNCTIONS

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_AUDIO = r"D:/变声/tts_models/ref/meituan_rat_002.wav"


def gqa_sdpa_forward(module, query, key, value, attention_mask, dropout=0.0,
                     scaling=None, is_causal=None, **kwargs):
    sdpa_kwargs = {}
    if getattr(module, "num_key_value_groups", 1) > 1:
        sdpa_kwargs["enable_gqa"] = True
    if attention_mask is not None and attention_mask.ndim == 4:
        attention_mask = attention_mask[:, :, :, : key.shape[-2]]
    if is_causal is None:
        is_causal = (query.shape[2] > 1 and attention_mask is None
                     and getattr(module, "is_causal", True))
    attn_output = torch.nn.functional.scaled_dot_product_attention(
        query, key, value, attn_mask=attention_mask, dropout_p=dropout,
        scale=scaling, is_causal=is_causal, **sdpa_kwargs)
    return attn_output.transpose(1, 2).contiguous(), None


ALL_ATTENTION_FUNCTIONS["sdpa"] = gqa_sdpa_forward

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
# patch 后原版 generate 也走 GQA —— 顺便验证原版仍能正常出声（贪心）
wavs, sr = model.generate_voice_clone(text=["你好。"], language=["Chinese"],
                                      voice_clone_prompt=prompt, do_sample=False,
                                      subtalker_dosample=False, repetition_penalty=1.0)
import soundfile as sf
sf.write(r"D:/变声/outputs/diag_gqa_patch_orig.wav", wavs[0], sr)
print("patched orig generate OK, wav saved", flush=True)

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
mem(f"baseline (L={L})")

frame_emb = torch.zeros(1, 1, TC.hidden_size, dtype=torch.bfloat16, device=DEV)
for step in range(10):
    t = talker.model(inputs_embeds=frame_emb, past_key_values=st, use_cache=True,
                     cache_position=torch.tensor([L + step], device=DEV),
                     output_attentions=False, output_hidden_states=False)
    del t
    if step in (0, 1, 2, 9):
        mem(f"decode step {step}")
print("DONE", flush=True)
