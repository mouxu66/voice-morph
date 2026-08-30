"""单进程单配置：argv 选 SDPA 后端配置，测 leak。"""
import functools
import sys

import torch
from transformers import DynamicCache, StaticCache
from transformers.masking_utils import create_causal_mask

MODE = sys.argv[1] if len(sys.argv) > 1 else "default"
bk = torch.backends.cuda
if MODE != "default":
    bk.enable_flash_sdp(False)
    bk.enable_mem_efficient_sdp(False)
    bk.enable_math_sdp(False)
    bk.enable_cudnn_sdp(False)
    {"math": bk.enable_math_sdp,
     "mem": bk.enable_mem_efficient_sdp,
     "cudnn": bk.enable_cudnn_sdp,
     "flash": bk.enable_flash_sdp}[MODE](True)
elif MODE == "default":
    pass

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
d = (torch.cuda.memory_allocated() / 2**20 - base) / 3
print(f"[{MODE:8s}] leak = {d:8.1f} MB/step", flush=True)
