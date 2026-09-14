# 用官方 generate_voice_clone 做声音克隆（稳定，不依赖 codec_embedding hack）
# 用法: python m1_workshop/gen_vc.py
#   --ref kangaroo | me      选参考音（默认两个都出）
#   --text "句子"            可多条
import os, sys, argparse, numpy as np, torch, librosa, soundfile as sf
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

BASE="D:/变声/tts_models/qwen3-tts-1.7b-base"
REFS={
 "kangaroo":"D:/变声/media/voicebank/kangaroo_110637_ref_24k.wav",  # 第三方素材（男）
 "me":      "D:/变声/media/voicebank/my_voice_1/reference.wav",     # 你自己的声音(男)
}
DEFAULT_TEXTS=[
 "您好，我是袋鼠骑士，很高兴为您服务。",
 "欢迎使用美团外卖，您的订单已经送达，请及时取餐。",
 "今天天气真好，我们一起去公园散步吧！",
 "袋鼠骑士提醒您，请携带手机尾号后四位取餐。",
]

ap=argparse.ArgumentParser()
ap.add_argument("--ref",nargs="*",default=list(REFS.keys()))
ap.add_argument("--text",nargs="*",default=DEFAULT_TEXTS)
args=ap.parse_args()

qwen3tts=Qwen3TTSModel.from_pretrained(BASE,torch_dtype=torch.bfloat16,attn_implementation="sdpa")

for key in args.ref:
    if key not in REFS:
        print("未知 ref:",key); continue
    ref=REFS[key]
    out=f"D:/变声/outputs/gen_{key}"; os.makedirs(out,exist_ok=True)
    wavs,sr=qwen3tts.generate_voice_clone(
        text=args.text, ref_audio=ref, x_vector_only_mode=True,
        non_streaming_mode=True, do_sample=True, temperature=0.8,
        top_p=0.95, repetition_penalty=1.1, max_new_tokens=200)
    for i,w in enumerate(wavs):
        p=f"{out}/{key}_{i+1}.wav"; sf.write(p,w,sr)
        print(f"[gen][{key}] {p}  ({len(args.text[i])}字)")
    print()
print("完成。输出目录:", {k:f"D:/变声/outputs/gen_{k}" for k in args.ref})
