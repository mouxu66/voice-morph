# coding=utf-8
# 合并 LoRA -> 单一模型（复用已训好的 adapter-epoch-2，不重跑训练）。
# 关键修正：基座用【全精度 bf16 加载，不量化】。这样 merge_and_unload 不涉及任何
# 4-bit 反量化（本机 PEFT 版本对 4-bit 层 merge 会产出 [N,1] 形状损坏权重），
# 合并结果干净。LoRA 增量与基座精度无关，照样生效。
import os, json, sys, librosa, numpy as np, torch
from peft import PeftModel, LoraConfig
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
from safetensors.torch import save_file

BASE = "D:/变声/tts_models/qwen3-tts-1.7b-base"
ADAPTER = "D:/变声/output_qlora/adapter-epoch-2"
REF = "D:/变声/media/voicebank/merg_004/reference_24k.wav"
OUT = "D:/变声/output_qlora/final_model"
SPK = "meituan_kangaroo"

print("[merge] 加载【全精度 bf16】基座（不量化） ...", flush=True)
qwen3tts = Qwen3TTSModel.from_pretrained(
    BASE, torch_dtype=torch.bfloat16, attn_implementation="sdpa",
)
base = qwen3tts.model

# get_input_embeddings 补丁（PEFT 某些路径需要）
def _gi(self): return self.talker.model.codec_embedding
def _si(self, v): self.talker.model.codec_embedding = v
base.get_input_embeddings = _gi.__get__(base)
base.set_input_embeddings = _si.__get__(base)

# 用与训练一致的 LoRA 配置，再从已训 adapter 载入权重
lora_config = LoraConfig(
    task_type=None, r=16, lora_alpha=32, lora_dropout=0.05, bias="none",
    target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
    modules_to_save=[],
)
peft_model = PeftModel.from_pretrained(base, ADAPTER)
print("[merge] 适配器载入完成", flush=True)

# 说话人锚点（同训练逻辑）
def compute_target_speaker_embedding(base_model, ref_audio_path):
    audio, sr = librosa.load(ref_audio_path, sr=None, mono=True)
    target_sr = base_model.speaker_encoder_sample_rate
    if sr != target_sr:
        audio = librosa.resample(y=audio.astype(np.float32), orig_sr=int(sr), target_sr=int(target_sr))
    emb = base_model.extract_speaker_embedding(audio=audio.astype(np.float32), sr=int(target_sr))
    emb = emb.unsqueeze(0) if emb.dim() == 1 else emb
    return emb.detach().to(torch.bfloat16)

target_speaker_embedding = compute_target_speaker_embedding(base, REF)
print(f"[merge] target_speaker_embedding shape={tuple(target_speaker_embedding.shape)}", flush=True)

print("[merge] 合并 LoRA（全精度，无 4-bit 反量化损坏） ...", flush=True)
merged = peft_model.merge_and_unload()

# 形状校验：不应出现任何 [X,1] 的损坏 Linear
bad = [(n, tuple(p.shape)) for n, p in merged.named_parameters()
       if p.ndim == 2 and p.shape[1] == 1 and p.numel() > 100000]
if bad:
    print("[merge] ERROR 仍存在损坏权重:", bad[:5]); sys.exit(1)
print("[merge] 形状校验通过", flush=True)

os.makedirs(OUT, exist_ok=True)
skip_ext = (".safetensors", ".bin", ".gguf", ".pt", ".pth", ".onnx")
for root, _, files in os.walk(BASE):
    for f in files:
        if f.lower().endswith(skip_ext): continue
        s = os.path.join(root, f); d = os.path.join(OUT, os.path.relpath(s, BASE))
        os.makedirs(os.path.dirname(d), exist_ok=True)
        with open(s, "rb") as fin, open(d, "wb") as fout: fout.write(fin.read())

config_dict = json.load(open(os.path.join(BASE, "config.json"), "r", encoding="utf-8"))
config_dict["tts_model_type"] = "custom_voice"
tc = config_dict.get("talker_config", {})
tc["spk_id"] = {SPK: 3000}; tc["spk_is_dialect"] = {SPK: False}
config_dict["talker_config"] = tc
with open(os.path.join(OUT, "config.json"), "w", encoding="utf-8") as f:
    json.dump(config_dict, f, indent=2, ensure_ascii=False)

state_dict = {k: v.detach().cpu() for k, v in merged.state_dict().items()}
for k in [k for k in state_dict if k.startswith("speaker_encoder")]:
    del state_dict[k]
weight = state_dict["talker.model.codec_embedding.weight"]
weight[3000] = target_speaker_embedding[0].detach().to(weight.device).to(weight.dtype)
state_dict["talker.model.codec_embedding.weight"] = weight
save_file(state_dict, os.path.join(OUT, "model.safetensors"))
print(f"[merge] 最终模型已写出: {OUT}", flush=True)
print("MERGE_DONE", flush=True)
