# coding=utf-8
# 正确 QLoRA 推理：base 4-bit（与训练完全一致，主干冻结量化）
# + 运行时套 adapter（不合并，杜绝"合并成 bf16 再 4-bit 二次量化"这一脚炮）
# + 真实袋鼠声纹烤进 codec_embedding[3000]
# 绕过 generate_custom_voice 包裹层，直接走 base.generate（带 LoRA 的 PeftModel.generate）
import os, json, sys, traceback
import numpy as np
import torch
import librosa
from transformers import BitsAndBytesConfig
from peft import PeftModel
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel

BASE    = "D:/变声/tts_models/qwen3-tts-1.7b-base"
ADAPTER = "D:/变声/output_qlora/adapter-epoch-2"
REF     = "D:/变声/media/voicebank/merg_004/reference_24k.wav"
SPK     = "meituan_kangaroo"
OUT     = "D:/变声/outputs/ft_proper"
os.makedirs(OUT, exist_ok=True)
LOG     = "D:/变声/output_qlora_proper.log"

def log(*a):
    s = " ".join(str(x) for x in a); print(s, flush=True)
    with open(LOG, "a", encoding="utf-8") as f: f.write(s + "\n")

SKIP = ["speaker_encoder","codec_embedding","text_embedding","codec_head","text_projection","lm_head"]
bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                         bnb_4bit_use_double_quant=True,
                         bnb_4bit_compute_dtype=torch.bfloat16,
                         llm_int8_skip_modules=SKIP)

log("[load] base 4-bit (与训练同构，主干不合并) ...")
qwen3tts = Qwen3TTSModel.from_pretrained(
    BASE, torch_dtype=torch.bfloat16, attn_implementation="sdpa",
    quantization_config=bnb,
)
base = qwen3tts.model  # Qwen3TTSForConditionalGeneration

# get_input_embeddings 补丁（PEFT 需要）
def _gi(self): return self.talker.model.codec_embedding
def _si(self, v): self.talker.model.codec_embedding = v
base.get_input_embeddings = _gi.__get__(base)
base.set_input_embeddings = _si.__get__(base)

log("[load] 运行时套 adapter（不合并）...")
if os.environ.get("NO_LORA"):
    log("[load] NO_LORA=1 -> 纯 base（不套 adapter），用于诊断 speakers= 路径本身")
    model = base
else:
    model = PeftModel.from_pretrained(base, ADAPTER)   # model.generate 自动带 LoRA

# ---- 说话人锚点：真实袋鼠声纹 -> codec_embedding[3000] ----
# 统一 speaker_encoder 与模型实际 dtype，避免 conv 的 input/bias 类型不一致
base.speaker_encoder = base.speaker_encoder.to(getattr(base, "dtype", torch.bfloat16))
audio, sr = librosa.load(REF, sr=None, mono=True)
tgt_sr = base.speaker_encoder_sample_rate
if int(sr) != int(tgt_sr):
    audio = librosa.resample(y=audio.astype(np.float32), orig_sr=int(sr), target_sr=int(tgt_sr))
emb = base.extract_speaker_embedding(audio=audio.astype(np.float32), sr=int(tgt_sr))
emb = emb.unsqueeze(0) if emb.dim() == 1 else emb
emb = emb.detach().to(torch.bfloat16)
ce = base.talker.model.codec_embedding
with torch.no_grad():
    ce.weight[3000] = emb[0].to(ce.weight.dtype).to(ce.weight.device)
log(f"[anchor] codec_embedding[3000] <- 袋鼠声纹 shape={tuple(emb.shape)}")

# ---- 让 base.generate 走 custom_voice 的 spk_id 路径（直接调用 generate，不经包裹层检查）----
tc = base.config.talker_config
tc.spk_id = {SPK: 3000}
tc.spk_is_dialect = {SPK: False}
# 防止说话人校验读取到旧集合
try:
    base.supported_speakers = tc.spk_id.keys()
except Exception:
    pass
log(f"[cfg] spk_id={tc.spk_id}")

tests = [
    "欢迎使用美团外卖，您的订单已经送达，请及时取餐。",
    "今天天气真好，我们一起去公园散步吧！",
    "您好，我是袋鼠骑士，很高兴为您服务。",
]
ok = 0
import soundfile as sf
for i, t in enumerate(tests):
    try:
        input_ids = qwen3tts._tokenize_texts([qwen3tts._build_assistant_text(t)])
        talker_codes_list, _ = model.generate(
            input_ids=input_ids,
            languages=["Auto"],
            speakers=[SPK],
            non_streaming_mode=True,
            do_sample=True, temperature=0.8, top_p=0.95,
            repetition_penalty=1.1, max_new_tokens=200,
        )
        codes = talker_codes_list[0]
        # 诊断：token 统计
        cu = codes.unique().numel() if hasattr(codes, "unique") else len(set(codes.tolist()))
        log(f"[{i}] codes len={codes.shape if hasattr(codes,'shape') else len(codes)} unique={cu}")
        wavs, fs = base.speech_tokenizer.decode([{"audio_codes": codes}])
        arr = wavs[0]
        if arr is None or len(arr) == 0:
            log(f"[{i}] EMPTY"); continue
        out = f"{OUT}/proper_{i+1}.wav"
        sf.write(out, arr, fs)
        log(f"[{i}] OK dur={len(arr)/fs:.1f}s fs={fs} -> {out}")
        ok += 1
    except Exception as e:
        traceback.print_exc()
        log(f"[{i}] ERROR: {e}")

# ---- 诊断：同一条文本用 greedy（不采样）看是否采样把不确定模型推成噪声 ----
try:
    log("[diag] greedy 测试 ...")
    input_ids = qwen3tts._tokenize_texts([qwen3tts._build_assistant_text(tests[0])])
    codes_g, _ = model.generate(
        input_ids=input_ids, languages=["Auto"], speakers=[SPK], non_streaming_mode=True,
        do_sample=False, max_new_tokens=180,
    )
    cg = codes_g[0]
    cu = cg.unique().numel()
    wavs_g, fs_g = base.speech_tokenizer.decode([{"audio_codes": cg}])
    ag = wavs_g[0]
    outg = f"{OUT}/greedy_1.wav"
    sf.write(outg, ag, fs_g)
    zc = float(np.mean(np.diff(np.sign(ag.astype(float)))!=0))
    log(f"[diag] greedy codes len={tuple(cg.shape)} unique={cu} zcr={zc:.3f} dur={len(ag)/fs_g:.1f}s -> {outg}")
except Exception as e:
    traceback.print_exc(); log(f"[diag] ERROR: {e}")
log(f"PROPER_INFER_DONE ok={ok}/{len(tests)}")
