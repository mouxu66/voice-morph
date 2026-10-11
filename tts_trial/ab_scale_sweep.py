# -*- coding: utf-8 -*-
"""零重训 LoRA scale 扫描（#343：默认 scale=alpha/r=2.0 过冲，~0.4 才稳）。

加载一次 base+adapter，改每个 lora 模块的 scaling[active]，在同一句上自由生成 + ASR。
scale=0 即等价纯 base（校验下沿）。
"""
import os, sys
_HERE=os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path: sys.path.insert(0,_HERE)
import librosa, numpy as np, soundfile as sf, torch
sys.stdout.reconfigure(encoding="utf-8")
BASE="D:/变声/tts_models/qwen3-tts-1.7b-base"
AD=os.environ.get("VM_ADAPTER",r"D:\变声\media\ft_work\ppt12\out_dshift_fix\adapter-final")
REF=os.environ.get("VM_REF",r"D:\变声\media\ft_work\ppt12\ref_24k.wav")
TEXT=os.environ.get("VM_TEXT","这一节我们讲三个概念，先看第一个。")
OUT=r"D:\变声\media\ft_work\ppt12\out_ab\scale_sweep"; os.makedirs(OUT,exist_ok=True)
SCALES=[2.0,1.0,0.5,0.4,0.0]
SPK_ID,SPK=3000,"spk"
def log(m): print(m,flush=True)
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
from peft import PeftModel
import transformers
log(f"[env] tf={transformers.__version__}")
q=Qwen3TTSModel.from_pretrained(BASE,torch_dtype=torch.bfloat16,attn_implementation="sdpa")
b=q.model.to("cuda")
b.get_input_embeddings=(lambda self:self.talker.model.codec_embedding).__get__(b)
b.set_input_embeddings=(lambda self,v:setattr(self.talker.model,"codec_embedding",v)).__get__(b)
b.speaker_encoder=b.speaker_encoder.to(torch.bfloat16)
a,sr=librosa.load(REF,sr=None,mono=True); tgt=b.speaker_encoder_sample_rate
if int(sr)!=int(tgt): a=librosa.resample(y=a.astype(np.float32),orig_sr=int(sr),target_sr=int(tgt))
e=b.extract_speaker_embedding(audio=a.astype(np.float32),sr=int(tgt)); e=e.unsqueeze(0) if e.dim()==1 else e
ce=b.talker.model.codec_embedding
with torch.no_grad(): ce.weight[SPK_ID]=e.detach().to(torch.bfloat16)[0].to(ce.weight.dtype).to(ce.weight.device)
m=PeftModel.from_pretrained(b,AD); m.eval()
tc=b.config.talker_config; tc.spk_id={SPK:SPK_ID}; tc.spk_is_dialect={SPK:False}; b.supported_speakers=tc.spk_id.keys()

# 找所有 lora 模块与其原始 scaling
lora_mods=[mod for mod in m.modules() if hasattr(mod,"scaling") and isinstance(getattr(mod,"scaling",None),dict)]
active=getattr(m,"active_adapter","default")
log(f"[setup] lora 模块数={len(lora_mods)} active={active} 原始 scaling 样例={lora_mods[0].scaling if lora_mods else None}")

def gen_once(ctx=None):
    with torch.no_grad():
        codes,_=m.generate(input_ids=tk,languages=["Auto"],speakers=[SPK],non_streaming_mode=True,
                           do_sample=False,max_new_tokens=200)
    return codes

from faster_whisper import WhisperModel
import zhconv
wm=WhisperModel("small",device="cuda",compute_type="float16")
tk=q._tokenize_texts([q._build_assistant_text(TEXT)])
tk=[x.to("cuda") if torch.is_tensor(x) else torch.tensor(x,device="cuda") for x in tk]

for s in SCALES:
    try:
        if s==0.0:
            with m.disable_adapter():
                codes=gen_once()
        else:
            for mod in lora_mods:
                for k in mod.scaling: mod.scaling[k]=float(s)
            codes=gen_once()
        c=codes[0]; wavs,fs=b.speech_tokenizer.decode([{"audio_codes":c}])
        arr=np.asarray(wavs[0],dtype=np.float32); p=os.path.join(OUT,f"s{s:.1f}.wav"); sf.write(p,arr,int(fs))
        segs,_=wm.transcribe(p,language="zh",vad_filter=False)
        back=zhconv.convert("".join(x.text for x in segs),"zh-cn")
        peak=float(np.max(np.abs(arr)))
        good = peak<0.9 and len(back)>=4
        log(f"[scale {s:.1f}] dur={len(arr)/fs:4.1f}s frames={int(c.shape[0]):3d} peak={peak:.3f} "
            f"{'★可懂' if good else '崩'} 回译={back}")
    except Exception as ex:
        import traceback; traceback.print_exc(); log(f"[scale {s}] 失败 {ex}")
