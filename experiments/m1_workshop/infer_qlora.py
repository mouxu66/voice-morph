# coding=utf-8
# QLoRA 标准推理：4-bit 基座 + 激活态 LoRA（不 merge，避免 merge_and_unload 的 4-bit 反量化损坏）。
# 显存仅 ~4.5GB，forward 时 4-bit 层按需反量化，不经过 merge。
import os, json, sys, librosa, numpy as np, torch
from peft import PeftModel, LoraConfig
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
from transformers import BitsAndBytesConfig

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
ADAPTER = "D:/变声/output_qlora/adapter-epoch-2"
REF = "D:/变声/media/voicebank/merg_004/reference_24k.wav"
SPK = "meituan_kangaroo"
LOG = "D:/变声/output_qlora_infer.log"

def log(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f: f.write(s + "\n")

SKIP = ["speaker_encoder","codec_embedding","text_embedding","codec_head","text_projection","lm_head"]
bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
                         bnb_4bit_compute_dtype=torch.bfloat16, llm_int8_skip_modules=SKIP)

log("[infer] 加载 4-bit 基座 + LoRA 适配器 ...")
qwen3tts = Qwen3TTSModel.from_pretrained(BASE, torch_dtype=torch.bfloat16, attn_implementation="sdpa",
                                         quantization_config=bnb)
base = qwen3tts.model
def _gi(self): return self.talker.model.codec_embedding
def _si(self, v): self.talker.model.codec_embedding = v
base.get_input_embeddings = _gi.__get__(base)
base.set_input_embeddings = _si.__get__(base)

peft_model = PeftModel.from_pretrained(base, ADAPTER)
qwen3tts.model = peft_model  # 让 generate_custom_voice 走带 LoRA 的模型

# 配置为 custom_voice，注册说话人
inner = peft_model.get_base_model()
qwen3tts.model.tts_model_type = "custom_voice"
tc = inner.config.talker_config
tc = tc.copy() if isinstance(tc, dict) else dict(tc)
tc["spk_id"] = {SPK: 3000}; tc["spk_is_dialect"] = {SPK: False}
inner.config.talker_config = tc

# 注入说话人锚点（同训练逻辑：参考音频算一次）
audio, sr = librosa.load(REF, sr=None, mono=True)
tsr = inner.speaker_encoder_sample_rate
if sr != tsr:
    audio = librosa.resample(y=audio.astype(np.float32), orig_sr=int(sr), target_sr=int(tsr))
emb = inner.extract_speaker_embedding(audio=audio.astype(np.float32), sr=int(tsr))
emb = emb.unsqueeze(0) if emb.dim() == 1 else emb
emb = emb.detach().to(torch.bfloat16)
with torch.no_grad():
    w = inner.talker.model.codec_embedding.weight
    w[3000] = emb[0].to(w.device).to(w.dtype)
log(f"[infer] 说话人锚点已注入 codec_embedding[3000], emb shape={tuple(emb.shape)}")
log(f"[infer] supported speakers: {qwen3tts.get_supported_speakers()}")

import soundfile as sf
tests = [
    "欢迎使用美团外卖，您的订单已经送达，请及时取餐。",
    "今天天气真好，我们一起去公园散步吧！",
    "您好，我是袋鼠骑士，很高兴为您服务。",
]
ok = 0
for i, t in enumerate(tests):
    try:
        wavs, fs = qwen3tts.generate_custom_voice(text=t, speaker=SPK, do_sample=True, temperature=0.8, top_p=0.95)
        arr = wavs[0]
        if arr is None or len(arr) == 0:
            log(f"[{i}] EMPTY for: {t[:20]}"); continue
        out = f"D:/变声/outputs/ft_sample_{i+1}.wav"
        sf.write(out, arr, fs)
        log(f"[{i}] OK dur={len(arr)/fs:.1f}s fs={fs} -> {out}")
        ok += 1
    except Exception as e:
        log(f"[{i}] ERROR: {e}")
import traceback; traceback.print_exc()
log(f"INFER_DONE ok={ok}/{len(tests)}")
