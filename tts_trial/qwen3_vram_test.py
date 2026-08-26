# Qwen3-TTS 显存压力测试（bf16 + 原生 SDPA，不依赖 flash-attn）
import torch
import subprocess
from qwen_tts import Qwen3TTSModel

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
TOKENIZER_DIR = r"D:/变声/tts_models/qwen3-tts-tokenizer-12hz"
REF_AUDIO = r"D:/变声/GPT-SoVITS/data/meituan_rat/wavs/002.wav"
REF_TEXT = "怕被其他人知道这家店给你一个"


def nvidia():
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free,memory.total",
             "--format=csv,noheader,nounits"], text=True)
        used, free, total = [int(x.strip()) for x in out.strip().split(",")]
        return used, free, total
    except Exception as e:
        return None, None, None


total = round(torch.cuda.get_device_properties(0).total_memory / 1024 ** 2, 1)
print(f"[info] GPU 总显存: {total} MiB")
torch.cuda.empty_cache()
u, f, _ = nvidia()
print(f"[before load] nvidia used={u} free={f} MiB")

model = Qwen3TTSModel.from_pretrained(
    MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)
torch.cuda.synchronize()
u, f, _ = nvidia()
print(f"[after load ] nvidia used={u} free={f} MiB | allocated={torch.cuda.max_memory_allocated()//1024**2} MiB")

prompt_items = model.create_voice_clone_prompt(
    ref_audio=REF_AUDIO, ref_text=REF_TEXT, x_vector_only_mode=False)
texts = ["大家好，我是袋鼠，今天给大家做一段语音克隆的测试。"]
wavs, sr = model.generate_voice_clone(
    text=texts, language=["Chinese"], voice_clone_prompt=prompt_items)
torch.cuda.synchronize()
u, f, _ = nvidia()
print(f"[after gen  ] nvidia used={u} free={f} MiB")
print(f"[peak       ] allocated={torch.cuda.max_memory_allocated()//1024**2} MiB | reserved={torch.cuda.max_memory_reserved()//1024**2} MiB")
print("[done] sample_rate =", sr)
