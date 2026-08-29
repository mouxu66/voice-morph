# 袋鼠音色稳健生成器：低温度 + 每句多次采样，按 F0 贴近锚点择优
# 用法: python m1_workshop/gen_kangaroo_robust.py ["要说的句子"...]
#   不传参=4 句默认样例; --n-try 3 每句采样次数; --target-f0 125 锚点 Hz
import os, glob, shutil, argparse
import numpy as np, torch, soundfile as sf, librosa
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

BASE="D:/变声/tts_models/qwen3-tts-1.7b-base"
REF ="D:/变声/media/clips_clean/video_260828_105338_027.wav"
REF_TEXT="希望这一分钟的录影能让模型捕捉到你声音中独特的温力与韵律。"
OUT ="D:/变声/outputs/gen_kangaroo_robust"; os.makedirs(OUT,exist_ok=True)

DEFAULT=[
 "您好，我是袋鼠骑士，很高兴为您服务。",
 "欢迎使用美团外卖，您的订单已经送达，请及时取餐。",
 "今天天气真好，我们一起去公园散步吧！",
 "袋鼠骑士提醒您，请携带手机尾号后四位取餐。",
]

ap=argparse.ArgumentParser(); ap.add_argument("text",nargs="*")
ap.add_argument("--n-try",type=int,default=3)
ap.add_argument("--target-f0",type=float,default=125.0)
ap.add_argument("--temperature",type=float,default=0.4)
args=ap.parse_args()
texts=args.text or DEFAULT
N=args.n_try; TGT=args.target_f0; TEMP=args.temperature

def f0_med(p):
    # 用 soundfile 直读绕开 librosa.load → audioread → sox 链
    y,sr=sf.read(p,dtype='float32')
    if y.ndim>1: y=y.mean(1)
    out=librosa.pyin(y, fmin=librosa.note_to_hz('C2'), fmax=librosa.note_to_hz('C6'),
                     sr=sr, frame_length=2048, hop_length=512)
    f0=out[0] if isinstance(out,tuple) else out
    f0=np.nan_to_num(f0); v=f0[f0>0]
    return float(np.median(v)) if len(v)>3 else None

print(f"加载模型... (n_try={N}, temp={TEMP}, target_f0={TGT})")
qwen3tts=Qwen3TTSModel.from_pretrained(BASE,torch_dtype=torch.bfloat16,attn_implementation="sdpa")

for i,text in enumerate(texts):
    cands=[]
    for k in range(N):
        ws,sr=qwen3tts.generate_voice_clone(
            text=[text], ref_audio=REF, ref_text=REF_TEXT,
            x_vector_only_mode=False, non_streaming_mode=True,
            do_sample=True, temperature=TEMP, top_p=0.9, repetition_penalty=1.0,
            max_new_tokens=200)
        tmp=f"{OUT}/_cand{i+1}_{k+1}.wav"; sf.write(tmp,ws[0],sr)
        med=f0_med(tmp)
        dist=abs((med or 0)-TGT)
        cands.append((dist, med, k, tmp))
        print(f"  [{texts.index(text)+1}/{len(texts)}] cand{k+1}: F0={med:.0f}Hz" if med else f"  cand{k+1}: F0=?", end="  ")
    cands.sort()
    best=cands[0]
    final=f"{OUT}/kangaroo_{i+1}.wav"
    shutil.copy(best[3], final)
    print(f"  -> 选 cand#{best[2]+1} (F0={best[1]:.0f}Hz, dist={best[0]:.1f}) -> {final}")

# 清候选
for c in glob.glob(f"{OUT}/_cand*.wav"): os.remove(c)
print("\n完成 ->", OUT)
