import torch, os, soundfile as sf, traceback
import numpy as np
from qwen_tts import Qwen3TTSModel
from transformers import BitsAndBytesConfig, AutoModel
from peft import PeftModel

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
ADP  = "D:/变声/tts_models/kangaroo_knight_qlora/adapter-epoch-2"
REF  = "D:/变声/_trash_20260831/merg_004/reference.wav"
REF_TEXT = open("D:/变声/_trash_20260831/merg_004/ref_text.txt", encoding="utf-8").read().strip()
TEXT = "现在是2026年8月28号"
OUT  = "D:/变声/outputs/ab_test"
os.makedirs(OUT, exist_ok=True)

# ref_audio 用文件路径字符串传入（之前验证可用路径，避免元组触发 sox 后端）

# ---------- 路线1: base 原模型 + ICL 克隆 ----------
print("== route1: base ICL clone ==")
try:
    m1 = Qwen3TTSModel.from_pretrained(BASE, attn_implementation="sdpa")
    w1, sr1 = m1.generate_voice_clone(text=TEXT, ref_audio=REF, ref_text=REF_TEXT)
    sf.write(f"{OUT}/base_icl_017.wav", w1[0], sr1)
    print("route1 OK", w1[0].shape, sr1)
    del m1; torch.cuda.empty_cache()
except Exception as e:
    traceback.print_exc(); print("route1 FAILED:", repr(e))

# ---------- 路线2: 4bit 底座 + adapter 合并 ----------
print("== route2: 4bit base + adapter merge ==")
try:
    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.float16,
        llm_int8_skip_modules=["speaker_encoder", "codec_embedding", "text_embedding", "lm_head"],
    )
    base_m = AutoModel.from_pretrained(BASE, quantization_config=bnb, attn_implementation="sdpa")
    peft = PeftModel.from_pretrained(base_m, ADP)
    merged = peft.merge_and_unload()
    m2 = Qwen3TTSModel.from_pretrained(BASE, attn_implementation="sdpa")
    m2.model = merged
    w2, sr2 = m2.generate_voice_clone(text=TEXT, ref_audio=REF, ref_text=REF_TEXT)
    sf.write(f"{OUT}/qlora_fixed_017.wav", w2[0], sr2)
    print("route2 OK", w2[0].shape, sr2)
except Exception as e:
    traceback.print_exc(); print("route2 FAILED:", repr(e))
