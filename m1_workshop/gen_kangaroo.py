# 袋鼠骑士音色克隆生成器（官方 generate_voice_clone · ICL 语气克隆 + F0 择优）
# 任意文本 → 袋鼠男声，4 句全锁贴锚点 125Hz，解决"只有第 1 句像其他漂"问题
# 用法: python m1_workshop/gen_kangaroo.py "要说的句子" ...
#   不传参数则生成 4 句默认样例
#   --mode xvec  退回"只克隆音色"(朗读腔, 不学语气)
#   --n-try N    每句采样次数(默认3, 选F0最贴锚点的)
#   --target-f0 锚点Hz(默认125, 即袋鼠参考段中值)
import os, shutil, glob, argparse
import numpy as np, torch, soundfile as sf, librosa
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

BASE="D:/变声/tts_models/qwen3-tts-1.7b-base"
# 最佳参考段: 6.4s 纯袋鼠男声(125.6Hz), 取自 video_260828_105338 去BGM人声
REF ="D:/变声/media/clips_clean/video_260828_105338_027.wav"
# ICL 模式需要参考音对应的准确文字(短句转写较准)
REF_TEXT="希望这一分钟的录影能让模型捕捉到你声音中独特的温力与韵律。"
OUT ="D:/变声/outputs/gen_kangaroo"; os.makedirs(OUT,exist_ok=True)

DEFAULT=[
 "您好，我是袋鼠骑士，很高兴为您服务。",
 "欢迎使用美团外卖，您的订单已经送达，请及时取餐。",
 "今天天气真好，我们一起去公园散步吧！",
 "袋鼠骑士提醒您，请携带手机尾号后四位取餐。",
]

ap=argparse.ArgumentParser(); ap.add_argument("text",nargs="*")
ap.add_argument("--mode",choices=["icl","xvec"],default="icl")
ap.add_argument("--n-try",type=int,default=3)
ap.add_argument("--target-f0",type=float,default=125.0)
ap.add_argument("--temperature",type=float,default=0.4)
args=ap.parse_args()
texts=args.text or DEFAULT
xvec=(args.mode=="xvec")
N=args.n_try; TGT=args.target_f0; TEMP=args.temperature

def f0_med(p):
    # soundfile 直读绕开 librosa.load → audioread → sox 链
    y,sr=sf.read(p,dtype='float32')
    if y.ndim>1: y=y.mean(1)
    out=librosa.pyin(y, fmin=librosa.note_to_hz('C2'), fmax=librosa.note_to_hz('C6'),
                     sr=sr, frame_length=2048, hop_length=512)
    f0=out[0] if isinstance(out,tuple) else out
    f0=np.nan_to_num(f0); v=f0[f0>0]
    return float(np.median(v)) if len(v)>3 else None

print(f"加载模型... (mode={args.mode}, n_try={N}, temp={TEMP}, target_f0={TGT})")
qwen3tts=Qwen3TTSModel.from_pretrained(BASE,torch_dtype=torch.bfloat16,attn_implementation="sdpa")

for i,text in enumerate(texts):
    cands=[]
    for k in range(N):
        ws,sr=qwen3tts.generate_voice_clone(
            text=[text], ref_audio=REF, ref_text=(None if xvec else REF_TEXT),
            x_vector_only_mode=xvec, non_streaming_mode=True,
            do_sample=True, temperature=TEMP, top_p=0.9, repetition_penalty=1.0,
            max_new_tokens=200)
        tmp=f"{OUT}/_cand{i+1}_{k+1}.wav"; sf.write(tmp,ws[0],sr)
        med=f0_med(tmp)
        dist=abs((med or 0)-TGT)
        cands.append((dist, med, k, tmp))
    cands.sort()
    best=cands[0]
    final=f"{OUT}/kangaroo_{i+1}.wav"
    shutil.copy(best[3], final)
    f0s=" ".join(f"{c[1]:.0f}" for c in cands if c[1])
    print(f"  [{i+1}/{len(texts)}] F0候选=[{f0s}] -> 选#{best[2]+1}({best[1]:.0f}Hz) -> {final}")

for c in glob.glob(f"{OUT}/_cand*.wav"): os.remove(c)
print("完成 ->", OUT)
