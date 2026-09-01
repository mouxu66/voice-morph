# 纯基座(无LoRA) + 同一批文本生成并反听，对比 LoRA 是否破坏了文本->语音对齐
import os, numpy as np, torch, librosa, soundfile as sf
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
from faster_whisper import WhisperModel

BASE="D:/变声/tts_models/qwen3-tts-1.7b-base"
REF="D:/变声/media/voicebank/merg_004/reference_24k.wav"
SPK="meituan_kangaroo"; OUT="D:/变声/outputs/base_check"; os.makedirs(OUT,exist_ok=True)

qwen3tts=Qwen3TTSModel.from_pretrained(BASE,torch_dtype=torch.bfloat16,attn_implementation="sdpa")
base=qwen3tts.model
base.speaker_encoder=base.speaker_encoder.to(torch.bfloat16)
a,sr=librosa.load(REF,sr=None,mono=True); tgt=base.speaker_encoder_sample_rate
if int(sr)!=int(tgt): a=librosa.resample(y=a.astype(np.float32),orig_sr=int(sr),target_sr=int(tgt))
emb=base.extract_speaker_embedding(audio=a.astype(np.float32),sr=int(tgt)).detach().to(torch.bfloat16)
ce=base.talker.model.codec_embedding
with torch.no_grad(): ce.weight[3000]=emb[0].to(ce.weight.dtype).to(ce.weight.device)
tc=base.config.talker_config; tc.spk_id={SPK:3000}; tc.spk_is_dialect={SPK:False}
base.supported_speakers=tc.spk_id.keys()

tests=["欢迎使用美团外卖，您的订单已经送达，请及时取餐。",
       "今天天气真好，我们一起去公园散步吧！",
       "您好，我是袋鼠骑士，很高兴为您服务。"]
wavs_paths=[]
for i,t in enumerate(tests):
    iid=qwen3tts._tokenize_texts([qwen3tts._build_assistant_text(t)])
    codes,_=base.generate(input_ids=iid,languages=["Auto"],speakers=[SPK],non_streaming_mode=True,
                          do_sample=True,temperature=0.8,top_p=0.95,repetition_penalty=1.1,max_new_tokens=200)
    c=codes[0]
    w,_=base.speech_tokenizer.decode([{"audio_codes":c}])
    p=f"{OUT}/base_{i+1}.wav"; sf.write(p,w[0],_)
    wavs_paths.append((p,t))

print("\n=== 纯基座 反听 ===")
m=WhisperModel("small",device="auto",compute_type="int8")
for p,t in wavs_paths:
    segs,_=m.transcribe(p,language="zh",beam_size=5)
    heard="".join(s.text for s in segs).strip()
    print(f"[输入] {t}\n[听到] {heard}\n")
