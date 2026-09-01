# coding=utf-8
# 直接加载已合并好的 final_model（config 已含 custom_voice + spk_id，说话人锚点已注入
# codec_embedding[3000]），以 4-bit 低显存方式推理，避免全精度 VRAM 溢出变慢。
import torch, soundfile as sf, os
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
from transformers import BitsAndBytesConfig

MODEL = "D:/变声/output_qlora/final_model"
SPK = "meituan_kangaroo"
LOG = "D:/变声/output_qlora_infer.log"

def log(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f: f.write(s + "\n")

SKIP = ["speaker_encoder","codec_embedding","text_embedding","codec_head","text_projection","lm_head"]
bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
                         bnb_4bit_compute_dtype=torch.bfloat16, llm_int8_skip_modules=SKIP)

log("[infer] 4-bit 加载 final_model ...")
m = Qwen3TTSModel.from_pretrained(MODEL, torch_dtype=torch.bfloat16, attn_implementation="sdpa",
                                   quantization_config=bnb)
log(f"[infer] tts_model_type={m.model.tts_model_type}")
log(f"[infer] supported speakers={m.get_supported_speakers()}")

tests = [
    "欢迎使用美团外卖，您的订单已经送达，请及时取餐。",
    "今天天气真好，我们一起去公园散步吧！",
    "您好，我是袋鼠骑士，很高兴为您服务。",
]
ok = 0
for i, t in enumerate(tests):
    try:
        wavs, fs = m.generate_custom_voice(text=t, speaker=SPK, do_sample=True, temperature=0.8, top_p=0.95, max_new_tokens=300)
        arr = wavs[0]
        if arr is None or len(arr) == 0:
            log(f"[{i}] EMPTY for: {t[:20]}"); continue
        out = f"D:/变声/outputs/ft_sample_{i+1}.wav"
        sf.write(out, arr, fs)
        log(f"[{i}] OK dur={len(arr)/fs:.1f}s fs={fs} -> {out}")
        ok += 1
    except Exception as e:
        import traceback; traceback.print_exc()
        log(f"[{i}] ERROR: {e}")
log(f"INFER_DONE ok={ok}/{len(tests)}")
