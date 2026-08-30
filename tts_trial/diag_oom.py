"""验证修复方案：prefill 走 DynamicCache + 转 StaticCache，decode 连跑 10 步看显存。"""
import functools

import torch
from transformers import DynamicCache, StaticCache

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF_AUDIO = r"D:/变声/tts_models/ref/meituan_rat_002.wav"
TARGET_TEXT = "巴黎罗，我要掏你炉子了，今天不给你送外卖，你的奶茶已经凉了，麻烦你下楼取一下。"

from qwen_tts import Qwen3TTSModel


def mem(tag):
    torch.cuda.synchronize()
    print(f"[mem] {tag:44s} alloc={torch.cuda.memory_allocated()/2**30:.2f}GB "
          f"peak={torch.cuda.max_memory_allocated()/2**30:.2f}GB", flush=True)


print("loading model ...", flush=True)
model = Qwen3TTSModel.from_pretrained(MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)
talker = model.model.talker
predictor = talker.code_predictor
DEV = talker.device
TC = talker.config
PC = predictor.config
NG = TC.num_code_groups

torch.inference_mode().__enter__()
mem("after load")

CAP = {}
_orig_gen = talker.generate


@functools.wraps(_orig_gen)
def _hook(*a, **kw):
    CAP["prefill_embeds"] = kw["inputs_embeds"].clone()
    CAP["prefill_mask"] = kw["attention_mask"].clone()
    CAP["trailing"] = kw["trailing_text_hidden"].clone()
    CAP["pad"] = kw["tts_pad_embed"].clone()
    return _orig_gen(*a, **kw)


talker.generate = _hook
prompt = model.create_voice_clone_prompt(ref_audio=REF_AUDIO, ref_text=".",
                                         x_vector_only_mode=True)
_ = model.generate_voice_clone(text=[TARGET_TEXT], language=["Chinese"],
                               voice_clone_prompt=prompt,
                               do_sample=False, subtalker_dosample=False,
                               repetition_penalty=1.0)
mem("after greedy generate (L=181 prefill captured)")
L = CAP["prefill_embeds"].shape[1]
print(f"prefill L={L}", flush=True)
torch.cuda.reset_peak_memory_stats()

# --- 修复方案：DynamicCache prefill ---
dyn = DynamicCache()
out = talker.forward(
    inputs_embeds=CAP["prefill_embeds"], attention_mask=CAP["prefill_mask"],
    past_key_values=dyn, trailing_text_hidden=CAP["trailing"],
    tts_pad_embed=CAP["pad"], generation_step=-1, use_cache=True,
    output_hidden_states=False, cache_position=torch.arange(L, device=DEV))
mem("after DynamicCache prefill")

# --- KV 拷入 StaticCache ---
st = StaticCache(config=TC, max_cache_len=2048)
for i in range(TC.num_hidden_layers):
    layer = st.layers[i]
    k, v = dyn.layers[i].keys, dyn.layers[i].values
    if not layer.is_initialized:
        layer.lazy_initialization(k)
    layer.keys[:, :, :L].copy_(k)
    layer.values[:, :, :L].copy_(v)
del dyn, out
mem("after copy to StaticCache (+dyn freed)")

# --- talker decode 连跑 10 步 ---
tok = torch.zeros(1, 1, dtype=torch.long, device=DEV)
frame_emb = torch.zeros(1, 1, TC.hidden_size, dtype=torch.bfloat16, device=DEV)
for step in range(10):
    t = talker.model(inputs_embeds=frame_emb, past_key_values=st, use_cache=True,
                     cache_position=torch.tensor([L + step], device=DEV),
                     output_attentions=False, output_hidden_states=False)
    if step in (0, 1, 2, 9):
        mem(f"talker decode step {step}")

# --- predictor 完整 15 步 × 3 帧 ---
pred_cache = StaticCache(config=PC, max_cache_len=NG + 1)
past_hidden = torch.zeros(1, 1, TC.hidden_size, dtype=torch.bfloat16, device=DEV)
pin = torch.cat((past_hidden, talker.model.codec_embedding(tok)), dim=1)
pout = predictor.forward(inputs_embeds=pin, past_key_values=pred_cache,
                         use_cache=True, cache_position=torch.arange(2, device=DEV),
                         generation_steps=0, output_attentions=False,
                         output_hidden_states=False)
mem("predictor prefill")
for i in range(1, NG - 1):
    pout = predictor.forward(
        input_ids=pout.logits[:, -1, :].argmax(-1, keepdim=True), generation_steps=i,
        past_key_values=pred_cache, use_cache=True,
        cache_position=torch.tensor([i + 1], device=DEV),
        output_attentions=False, output_hidden_states=False)
mem("predictor 15 steps done")
print("ALL OK", flush=True)
