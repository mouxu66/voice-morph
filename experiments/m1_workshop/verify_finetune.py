import torch, numpy as np, soundfile as sf, sys, traceback
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

MODEL = "D:/变声/output_qlora/final_model"
SPK = "meituan_kangaroo"

print("loading final_model ...", flush=True)
m = Qwen3TTSModel.from_pretrained(
    MODEL, torch_dtype=torch.bfloat16, attn_implementation="sdpa",
)
print("tts_model_type:", m.model.tts_model_type, flush=True)
print("supported speakers:", m.get_supported_speakers(), flush=True)

tests = [
    "欢迎使用美团外卖，您的订单已经送达，请及时取餐。",
    "今天天气真好，我们一起去公园散步吧！",
    "您好，我是袋鼠骑士，很高兴为您服务。",
]
ok = 0
for i, t in enumerate(tests):
    try:
        wavs, fs = m.generate_custom_voice(
            text=t, speaker=SPK, do_sample=True, temperature=0.8, top_p=0.95,
        )
        arr = wavs[0]
        if arr is None or len(arr) == 0:
            print(f"[{i}] EMPTY audio for: {t[:24]}", flush=True)
            continue
        out = f"D:/变声/outputs/ft_sample_{i+1}.wav"
        sf.write(out, arr, fs)
        print(f"[{i}] OK text={t[:24]} -> {out} dur={len(arr)/fs:.1f}s fs={fs}", flush=True)
        ok += 1
    except Exception as e:
        print(f"[{i}] ERROR: {e}", flush=True)
        traceback.print_exc()
print(f"VERIFY_DONE ok={ok}/{len(tests)}", flush=True)
