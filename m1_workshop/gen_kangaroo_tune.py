# 袋鼠音色调优对比生成器
# 用法:
#   python m1_workshop/gen_kangaroo_tune.py --mode xvec --ref <参考音> --out <目录> ["文本"...]
#   python m1_workshop/gen_kangaroo_tune.py --mode icl  --ref <参考音> --ref_text "参考音对应文字" --out <目录> ["文本"...]
import os, argparse
import numpy as np, torch, soundfile as sf
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

BASE="D:/变声/tts_models/qwen3-tts-1.7b-base"
DEFAULT=[
 "您好，我是袋鼠骑士，很高兴为您服务。",
 "欢迎使用美团外卖，您的订单已经送达，请及时取餐。",
 "今天天气真好，我们一起去公园散步吧！",
 "袋鼠骑士提醒您，请携带手机尾号后四位取餐。",
]
ap=argparse.ArgumentParser()
ap.add_argument("--ref",required=True)
ap.add_argument("--ref_text",default=None)
ap.add_argument("--mode",choices=["xvec","icl"],default="xvec")
ap.add_argument("--out",required=True)
ap.add_argument("text",nargs="*")
a=ap.parse_args()
texts=a.text or DEFAULT
xvec=(a.mode=="xvec")
assert xvec or a.ref_text, "ICL 模式必须给 --ref_text"

qwen3tts=Qwen3TTSModel.from_pretrained(BASE,torch_dtype=torch.bfloat16,attn_implementation="sdpa")
wavs,sr=qwen3tts.generate_voice_clone(
    text=texts, ref_audio=a.ref, ref_text=a.ref_text,
    x_vector_only_mode=xvec, non_streaming_mode=True,
    do_sample=True, temperature=0.7, top_p=0.95, repetition_penalty=1.05, max_new_tokens=200)
os.makedirs(a.out,exist_ok=True)
for i,w in enumerate(wavs):
    p=os.path.join(a.out,f"{a.mode}_{i+1}.wav"); sf.write(p,w,sr); print(f"[{a.mode}] {p}")
print("完成 ->", a.out)
