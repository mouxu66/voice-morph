# -*- coding: utf-8 -*-
"""最小对照：同一栈/参考音/文本，base vs base+LoRA，bf16 greedy。

用法 python ab_stack_check.py base|lora:<adapter_dir>
用于在锁版 4.57.3 + 旧 qwen_tts 下干净复现，不掺量化/采样变量。
"""
import os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path: sys.path.insert(0, _HERE)
import librosa, numpy as np, soundfile as sf, torch
sys.stdout.reconfigure(encoding="utf-8")
BASE="D:/变声/tts_models/qwen3-tts-1.7b-base"
REF=os.environ.get("VM_REF","D:/变声/media/voicebank/kangaroo/reference.wav")
OUT="D:/变声/media/ft_work/ppt12/out_ab/stack_check"; os.makedirs(OUT,exist_ok=True)
SPK_ID,SPK=3000,"spk"
TEXT=os.environ.get("VM_TEXT","您好，我是袋鼠骑士，很高兴为您服务。")
arm=sys.argv[1] if len(sys.argv)>1 else "base"
adapter=None
if arm.startswith("lora"):
    adapter=arm.split(":",1)[1] if ":" in arm else r"D:\变声\tts_trial\Qwen3-TTS\finetuning\output_qlora_aligned_v3\adapter-final"
tag="lora" if adapter else "base"
def log(m): print(m,flush=True)
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
import transformers
log(f"[env] transformers={transformers.__version__} qwen_tts={__import__('qwen_tts').__file__}")
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
m=b
if adapter:
    from peft import PeftModel
    m=PeftModel.from_pretrained(b,adapter)
m.eval()
tc=b.config.talker_config; tc.spk_id={SPK:SPK_ID}; tc.spk_is_dialect={SPK:False}; b.supported_speakers=tc.spk_id.keys()
tk=q._tokenize_texts([q._build_assistant_text(TEXT)])
tk=[x.to("cuda") if torch.is_tensor(x) else torch.tensor(x,device="cuda") for x in tk]
with torch.no_grad():
    codes,_=m.generate(input_ids=tk,languages=["Auto"],speakers=[SPK],non_streaming_mode=True,
                       do_sample=False,max_new_tokens=200)
c=codes[0]; wavs,fs=b.speech_tokenizer.decode([{"audio_codes":c}])
arr=np.asarray(wavs[0],dtype=np.float32); p=os.path.join(OUT,f"{tag}.wav"); sf.write(p,arr,int(fs))
log(f"[{tag}] dur={len(arr)/fs:.1f}s frames={int(c.shape[0])} peak={np.max(np.abs(arr)):.3f} -> {p}")
os.environ.setdefault("HF_HUB_OFFLINE","1"); os.environ.setdefault("TRANSFORMERS_OFFLINE","1")
from faster_whisper import WhisperModel
import zhconv
wm=WhisperModel("small",device="cuda",compute_type="float16")
segs,_=wm.transcribe(p,language="zh",vad_filter=False)
log(f"[{tag}] 回译: "+zhconv.convert("".join(s.text for s in segs),"zh-cn"))
