import torch
from qwen_tts.inference.qwen3_tts_model import Qwen3TTSModel
from transformers import BitsAndBytesConfig

bnb = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_quant_type="nf4",
    bnb_4bit_use_double_quant=True,
    bnb_4bit_compute_dtype=torch.bfloat16,
    llm_int8_skip_modules=["speaker_encoder", "codec_embedding", "text_embedding", "lm_head", "text_projection"],
)
m = Qwen3TTSModel.from_pretrained(
    "D:/变声/tts_models/qwen3-tts-1.7b-base",
    torch_dtype=torch.bfloat16,
    attn_implementation="sdpa",
    quantization_config=bnb,
)
base = m.model
print("TYPE(base):", type(base).__name__)
print("HAS get_input_embeddings:", hasattr(base, "get_input_embeddings"))
for path in ["talker.model.text_embedding", "talker.text_embedding",
             "model.talker.model.text_embedding", "model.text_embedding",
             "talker.model.codec_embedding", "codec_embedding"]:
    try:
        mod = base.get_submodule(path)
        print("FOUND", path, type(mod).__name__)
        break
    except Exception:
        pass
else:
    print("no embedding path found")
print("dir(base) hit:", [a for a in dir(base) if a in ("talker", "model", "codec_embedding", "text_embedding", "lm_head")])
