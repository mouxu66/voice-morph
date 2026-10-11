# -*- coding: utf-8 -*-
"""零训练决定性对照：当前训练 labels 是不是 double-shift(j+2)？

同一 teacher-forcing 序列，在【纯 base】上用两套 labels 各算一次 talker CE：
  A 当前脚本写法：手写 shift(labels[P-1]=c0, labels[P:P+N-1]=c1..) ，ForCausalLMLoss 内部再 shift -> j+2
  B 正确写法    ：labels 与 input 同位(labels[P:P+N]=c0..cN-1)，交给 ForCausalLMLoss 内部 shift -> j+1
判读：能正确说话的 base，正确 j+1 的 loss 应远低于随机线 ln3072=8.03；
若 A≈12(>随机) 而 B≪8，则当前训练确实在教 j+2（=Qwen3-TTS #371/#278 的 double-shift）。
"""
import json, os, sys
_HERE=os.path.dirname(os.path.abspath(__file__))
for p in (_HERE, r"D:\变声\tts_trial\Qwen3-TTS\finetuning"):
    if p not in sys.path: sys.path.insert(0,p)
import torch, librosa
sys.stdout.reconfigure(encoding="utf-8")
BASE="D:/变声/tts_models/qwen3-tts-1.7b-base"
PPT=r"D:\变声\media\ft_work\ppt12"; JSONL=os.path.join(PPT,"train_with_codes.jsonl"); REF=os.path.join(PPT,"ref_24k.wav")
SPK_ID=3000
def log(m): print(m,flush=True)
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
q=Qwen3TTSModel.from_pretrained(BASE,torch_dtype=torch.bfloat16,attn_implementation="sdpa")
b=q.model.to("cuda")
b.get_input_embeddings=(lambda self:self.talker.model.codec_embedding).__get__(b)
b.set_input_embeddings=(lambda self,v:setattr(self.talker.model,"codec_embedding",v)).__get__(b)
b.speaker_encoder=b.speaker_encoder.to(torch.bfloat16)
a,sr=librosa.load(REF,sr=None,mono=True); tgt=b.speaker_encoder_sample_rate
if int(sr)!=int(tgt): a=librosa.resample(y=a.astype(np.float32) if False else a.astype("float32"),orig_sr=int(sr),target_sr=int(tgt))
e=b.extract_speaker_embedding(audio=a.astype("float32"),sr=int(tgt)); e=e.unsqueeze(0) if e.dim()==1 else e
ce=b.talker.model.codec_embedding
with torch.no_grad(): ce.weight[SPK_ID]=e.detach().to(torch.bfloat16)[0].to(ce.weight.dtype).to(ce.weight.device)
from sft_12hz_qlora_aligned import build_prefill_embeds
import numpy as np
rows=[json.loads(l) for l in open(JSONL,encoding="utf-8")][:4]
sid=torch.tensor([[SPK_ID]],device="cuda",dtype=torch.long)
lA=[]; lB=[]
with torch.no_grad():
  for it in rows:
    ac=torch.tensor(it["audio_codes"],dtype=torch.long,device="cuda")[:160]
    iid=q._tokenize_texts([q._build_assistant_text(it["text"])])[0].to("cuda")
    if iid.dim()==1: iid=iid.unsqueeze(0)
    spk=b.talker.get_input_embeddings()(sid)
    pre=build_prefill_embeds(b,iid,spk); P=pre.shape[1]; c0=ac[:,0]; N=c0.shape[0]
    cemb=b.talker.get_input_embeddings()(c0.unsqueeze(0)); ie=torch.cat([pre,cemb],dim=1)
    am=torch.ones((1,P+N),dtype=torch.long,device="cuda")
    # A: 当前训练脚本的手写 shift
    labA=torch.full((1,P+N),-100,dtype=torch.long,device="cuda")
    labA[0,P-1]=c0[0]
    if N>1: labA[0,P:P+N-1]=c0[1:]
    labA[0,P+N-1]=b.config.talker_config.codec_eos_token_id
    # B: labels 与 input 同位，交 ForCausalLMLoss 内部 shift（正确 next-token）
    labB=torch.full((1,P+N),-100,dtype=torch.long,device="cuda")
    labB[0,P:P+N]=c0
    oA=b.talker(inputs_embeds=ie,attention_mask=am,labels=labA)
    oB=b.talker(inputs_embeds=ie,attention_mask=am,labels=labB)
    lA.append(float(oA.loss)); lB.append(float(oB.loss))
    log(f"  n={N}: A(手写shift,j+2)={float(oA.loss):.3f}   B(内部shift,j+1)={float(oB.loss):.3f}")
log(f"\n随机线 ln3072={np.log(3072):.3f}")
log(f"均值: A={np.mean(lA):.3f}  B={np.mean(lB):.3f}")
log("判读: B 远低于 8.03 且 B<<A -> 当前训练是 double-shift，LoRA 在学 j+2 的错目标")
