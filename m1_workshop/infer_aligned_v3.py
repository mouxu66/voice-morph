# 用对齐版 QLoRA adapter v3 推理，验证是否干净（非电音）
import os, sys, traceback
import numpy as np
import torch
import librosa
import soundfile as sf
from peft import PeftModel
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

BASE    = "D:/变声/tts_models/qwen3-tts-1.7b-base"
ADAPTER = "D:/变声/tts_trial/Qwen3-TTS/finetuning/output_qlora_aligned_v3/adapter-final"
REF     = "D:/变声/media/voicebank/kangaroo_110637_ref_24k.wav"
SPK     = "meituan_kangaroo"
OUT     = "D:/变声/outputs/ft_aligned_v3"
os.makedirs(OUT, exist_ok=True)
LOG     = "D:/变声/output_qlora_aligned_v3_infer.log"

def log(*a):
    s=" ".join(str(x) for x in a); print(s, flush=True)
    with open(LOG,"a",encoding="utf-8") as f: f.write(s+"\n")

log("[load] base (纯 bf16，不用 4bit 量化) ...")
qwen3tts=Qwen3TTSModel.from_pretrained(BASE,torch_dtype=torch.bfloat16,attn_implementation="sdpa")
base=qwen3tts.model

use_lora = not os.environ.get("NO_LORA")
if use_lora:
    log("[load] 套对齐 adapter v3 ...")
    model=PeftModel.from_pretrained(base, ADAPTER)
else:
    log("[load] NO_LORA 纯 base")
    model=base

base.speaker_encoder=base.speaker_encoder.to(getattr(base,"dtype",torch.bfloat16))
audio,sr=librosa.load(REF,sr=None,mono=True)
tgt_sr=base.speaker_encoder_sample_rate
if int(sr)!=int(tgt_sr): audio=librosa.resample(y=audio.astype(np.float32),orig_sr=int(sr),target_sr=int(tgt_sr))
emb=base.extract_speaker_embedding(audio=audio.astype(np.float32),sr=int(tgt_sr))
emb=emb.unsqueeze(0) if emb.dim()==1 else emb
emb=emb.detach().to(torch.bfloat16)
ce=base.talker.model.codec_embedding
with torch.no_grad(): ce.weight[3000]=emb[0].to(ce.weight.dtype).to(ce.weight.device)
log(f"[anchor] codec_embedding[3000] <- 纯袋鼠视频自身声纹")

tc=base.config.talker_config
tc.spk_id={SPK:3000}; tc.spk_is_dialect={SPK:False}
base.supported_speakers=tc.spk_id.keys()

tests=["欢迎使用美团外卖，您的订单已经送达，请及时取餐。",
       "今天天气真好，我们一起去公园散步吧！",
       "您好，我是袋鼠骑士，很高兴为您服务。"]
ok=0
for i,t in enumerate(tests):
    try:
        iid=qwen3tts._tokenize_texts([qwen3tts._build_assistant_text(t)])
        codes,_=model.generate(input_ids=iid,languages=["Auto"],speakers=[SPK],non_streaming_mode=True,
                               do_sample=True,temperature=0.8,top_p=0.95,repetition_penalty=1.1,max_new_tokens=200)
        c=codes[0]
        cu=c.unique().numel() if hasattr(c,"unique") else len(set(c.tolist()))
        wavs,fs=base.speech_tokenizer.decode([{"audio_codes":c}])
        arr=wavs[0]
        if arr is None or len(arr)==0: log(f"[{i}] EMPTY"); continue
        out=f"{OUT}/aligned_{i+1}.wav"; sf.write(out,arr,fs)
        zc=float(np.mean(np.diff(np.sign(arr.astype(float)))!=0))
        log(f"[{i}] len={c.shape if hasattr(c,'shape') else len(c)} unique={cu} dur={len(arr)/fs:.1f}s zcr={zc:.3f} -> {out}")
        ok+=1
    except Exception as e:
        traceback.print_exc(); log(f"[{i}] ERROR: {e}")
try:
    iid=qwen3tts._tokenize_texts([qwen3tts._build_assistant_text(tests[0])])
    cg,_=model.generate(input_ids=iid,languages=["Auto"],speakers=[SPK],non_streaming_mode=True,
                        do_sample=False,max_new_tokens=180)
    cg=cg[0]; cu=cg.unique().numel()
    wavs_g,fs_g=base.speech_tokenizer.decode([{"audio_codes":cg}])
    ag=wavs_g[0]; outg=f"{OUT}/aligned_greedy_1.wav"; sf.write(outg,ag,fs_g)
    zc=float(np.mean(np.diff(np.sign(ag.astype(float)))!=0))
    log(f"[diag] greedy unique={cu} zcr={zc:.3f} dur={len(ag)/fs_g:.1f}s -> {outg}")
except Exception as e:
    traceback.print_exc(); log(f"[diag] ERROR: {e}")
log(f"ALIGNED_INFER_DONE ok={ok}/{len(tests)} use_lora={use_lora}")
