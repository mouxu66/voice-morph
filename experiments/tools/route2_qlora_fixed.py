# route2: 4-bit 底座(与训练一致) + LoRA adapter 直接推理（不 merge，分布一致）
import os, time
import torch
import soundfile as sf
from transformers import BitsAndBytesConfig
from peft import PeftModel
from qwen_tts import Qwen3TTSModel

BASE      = "D:/变声/tts_models/qwen3-tts-1.7b-base"
ADP       = "D:/变声/tts_models/kangaroo_knight_qlora/adapter-epoch-2"
REF_AUDIO = "D:/变声/_trash_20260831/merg_004/reference.wav"
REF_TEXT  = "我真的恨上些OK,一会我给你分享的视频。"
TEXT      = "现在是2026年8月28号"
OUT       = "D:/变声/outputs/ab_test/qlora_fixed_017.wav"
os.makedirs("D:/变声/outputs/ab_test", exist_ok=True)

print("[1] loading 4bit base via Qwen3TTSModel.from_pretrained (nf4, fp32 compute) ...", flush=True)
bnb = BitsAndBytesConfig(
    load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.float32,
    llm_int8_skip_modules=["speaker_encoder", "codec_embedding", "text_embedding", "lm_head"],
)
tts = Qwen3TTSModel.from_pretrained(
    BASE, quantization_config=bnb, device_map="cuda:0", attn_implementation="sdpa",
)
base_model = tts.model
print("    base loaded, tts_model_type=", getattr(base_model, "tts_model_type", "N/A"), flush=True)

# 4bit 底座 base_model.dtype 是 fp32(怪异属性)，LoRA 增量在 4bit 上也是 fp32 计算，
# 因此整条链路统一用 fp32 最稳。把被 skip 的模块(含 talker/code_predictor 内的 lm_head)递归转 fp32。
SKIP_KEYS = ["speaker_encoder", "codec_embedding", "text_embedding", "lm_head"]
for name, mod in base_model.named_modules():
    if any(k in name for k in SKIP_KEYS):
        mod.to(torch.float32)

# speaker_encoder 的输入经 extract 内部 mels.to(self.dtype) 会变成 fp32(由 base.dtype 决定)，
# 这里不需额外包装，因为权重已 fp32，输入也 fp32，一致。
print("    skip 模块已统一 fp32", flush=True)

print("[2] attaching LoRA adapter (inference-time, no merge) ...", flush=True)
peft = PeftModel.from_pretrained(base_model, ADP)
peft.tts_model_type = "base"                          # 骗过 generate_voice_clone 的架构检查
peft.speech_tokenizer = base_model.speech_tokenizer   # decode 用
tts.model = peft
print("    wrapped, ready", flush=True)

print("[3] generate 袋鼠骑士 (4bit+LoRA):", TEXT, flush=True)
torch.cuda.synchronize(); t0 = time.time()
wavs, sr = tts.generate_voice_clone(
    text=TEXT, language="Chinese", ref_audio=REF_AUDIO, ref_text=REF_TEXT,
    x_vector_only_mode=False, max_new_tokens=2048,
    do_sample=True, top_k=50, top_p=1.0, temperature=0.9, repetition_penalty=1.05,
)
torch.cuda.synchronize(); print(f"    gen {time.time()-t0:.2f}s sr={sr}", flush=True)

sf.write(OUT, wavs[0], sr)
print("    saved", OUT, flush=True)
print("ROUTE2 DONE")
