# -*- coding: utf-8 -*-
"""验证 PR#360 的 RoPE-zero / Mimi 滑窗假设（无需重训）。

步骤：加载 bf16 base -> 记录 base 干净生成 -> 套 PeftModel -> 检查包裹后 talker 每层
inv_freq 是否仍非零 -> 对【包裹后的实际模块】强制 restore_rope_buffers +
restore_mimi_full_attention -> 再自由生成。若恢复清晰，则根因是 compat 补丁在
PEFT 包裹后未作用到实际推理模块。
"""
import gc, os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path: sys.path.insert(0, _HERE)
import librosa, numpy as np, soundfile as sf, torch
from peft import PeftModel
sys.stdout.reconfigure(encoding="utf-8")
BASE="D:/变声/tts_models/qwen3-tts-1.7b-base"
PPT=r"D:\变声\media\ft_work\ppt12"
AD=os.path.join(PPT,"out_ga1","adapter-final")
REF=os.path.join(PPT,"ref_24k.wav")
OUT=os.path.join(PPT,"out_ab","rope_test"); os.makedirs(OUT,exist_ok=True)
SPK_ID,SPK,TEXT=3000,"spk","这一节我们讲三个概念，先看第一个。"
def log(m): print(m,flush=True)

def rope_sums(b,tag):
    rows=[]
    for name,mod in b.named_modules():
        if hasattr(mod,"inv_freq") and isinstance(getattr(mod,"inv_freq",None),torch.Tensor):
            s=float(mod.inv_freq.abs().sum().item())
            rows.append((name,s))
    nz=[r for r in rows if r[1]!=0.0]
    log(f"[{tag}] inv_freq 模块数={len(rows)} 非零={len(nz)} 零={len(rows)-len(nz)}; "
        f"样例: "+", ".join(f"{n.split('.')[-2]+'.'+n.split('.')[-1] if '.' in n else n}={s:.3f}" for n,s in rows[:4]))
    return rows

def anchor(b):
    b.speaker_encoder=b.speaker_encoder.to(torch.bfloat16)
    a,sr=librosa.load(REF,sr=None,mono=True); tgt=b.speaker_encoder_sample_rate
    if int(sr)!=int(tgt): a=librosa.resample(y=a.astype(np.float32),orig_sr=int(sr),target_sr=int(tgt))
    e=b.extract_speaker_embedding(audio=a.astype(np.float32),sr=int(tgt)); e=e.unsqueeze(0) if e.dim()==1 else e
    ce=b.talker.model.codec_embedding
    with torch.no_grad(): ce.weight[SPK_ID]=e.detach().to(torch.bfloat16)[0].to(ce.weight.dtype).to(ce.weight.device)

def gen(m,q,b,tag):
    tc=b.config.talker_config; tc.spk_id={SPK:SPK_ID}; tc.spk_is_dialect={SPK:False}; b.supported_speakers=tc.spk_id.keys()
    tk=q._tokenize_texts([q._build_assistant_text(TEXT)])
    tk=[x.to("cuda") if torch.is_tensor(x) else torch.tensor(x,device="cuda") for x in tk]
    with torch.no_grad():
        codes,_=m.generate(input_ids=tk,languages=["Auto"],speakers=[SPK],non_streaming_mode=True,
                           do_sample=False,max_new_tokens=300)
    c=codes[0]; wavs,fs=b.speech_tokenizer.decode([{"audio_codes":c}])
    arr=np.asarray(wavs[0],dtype=np.float32); p=os.path.join(OUT,f"{tag}.wav"); sf.write(p,arr,int(fs))
    log(f"[{tag}] dur={len(arr)/fs:.1f}s frames={int(c.shape[0])} peak={np.max(np.abs(arr)):.3f}")
    return p

def asr(p,tag):
    from faster_whisper import WhisperModel
    import zhconv
    wm=WhisperModel("small",device="cuda",compute_type="float16")
    segs,_=wm.transcribe(p,language="zh",vad_filter=False)
    log(f"[{tag}] 回译: "+zhconv.convert("".join(s.text for s in segs),"zh-cn"))

from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
from qwen_tts._transformers_compat import restore_rope_buffers, restore_mimi_full_attention
q=Qwen3TTSModel.from_pretrained(BASE,torch_dtype=torch.bfloat16,attn_implementation="sdpa")
b=q.model.to("cuda")
b.get_input_embeddings=(lambda self:self.talker.model.codec_embedding).__get__(b)
b.set_input_embeddings=(lambda self,v:setattr(self.talker.model,"codec_embedding",v)).__get__(b)
anchor(b)
rope_sums(b,"base")
pm=PeftModel.from_pretrained(b,AD); pm.eval()
rope_sums(b,"after_peft_wrap")
# 不修复，先测一次（对照）
p0=gen(pm,q,b,"lora_beforerestore"); asr(p0,"lora_beforerestore")
# 关键：对包裹后的【实际 talker / 整个 model / tokenizer】强制修复
log("[restore] restore_rope_buffers(talker) ..."); restore_rope_buffers(b.talker)
log("[restore] restore_rope_buffers(whole model) ..."); restore_rope_buffers(b)
log("[restore] restore_mimi_full_attention(whole model) ..."); restore_mimi_full_attention(b)
if hasattr(b,"speech_tokenizer") and b.speech_tokenizer is not None:
    try:
        restore_mimi_full_attention(b.speech_tokenizer); log("[restore] mimi speech_tokenizer done")
    except Exception as e: log("[restore] speech_tokenizer skip: "+repr(e))
rope_sums(b,"after_restore")
p1=gen(pm,q,b,"lora_afterrestore"); asr(p1,"lora_afterrestore")
