# Qwen3-TTS-Base 袋鼠音色克隆 demo
# 用法：把模型下到 MODEL_DIR 后，python qwen3_tts_clone_demo.py
# 环境：Python 3.12 干净环境 + pip install -U qwen-tts
#       （可选）pip install -U flash-attn --no-build-isolation  -> 开启 flash_attention_2 省显存
# 注意：8GB 显存用 bfloat16 能跑；CustomVoice 版没有 generate_voice_clone，别下错

import torch
import soundfile as sf
from qwen_tts import Qwen3TTSModel

# ===== 改成你实际下载的目录（modelscope download --local_dir 指定的路径）=====
MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
# 如果没收 Tokenizer，可改成 HuggingFace/ModelScope 的 tokenizer 目录，或删掉 tokenizer 相关（from_pretrained 会自动拉）
TOKENIZER_DIR = r"D:/变声/tts_models/qwen3-tts-tokenizer-12hz"

REF_AUDIO = r"D:/变声/GPT-SoVITS/data/meituan_rat/wavs/002.wav"
REF_TEXT  = "怕被其他人知道这家店给你一个"   # 来自 GPT-SoVITS 训练列表

model = Qwen3TTSModel.from_pretrained(
    MODEL_DIR,
    device_map="cuda:0",
    dtype=torch.bfloat16,
    # attn_implementation="flash_attention_2",   # 没装 flash-attn 就注释掉这行
)

# 袋鼠音色克隆：先用参考音频构建一次可复用 prompt，后面多段文本都复用它
prompt_items = model.create_voice_clone_prompt(
    ref_audio=REF_AUDIO,
    ref_text=REF_TEXT,
    x_vector_only_mode=False,
)

texts = [
    "大家好，我是袋鼠，今天给大家做一段语音克隆的测试。",
    "其实我真的有发现，我是一个特别善于观察别人情绪的人。",
    "怕被其他人知道这家店，给你一个专属的惊喜。",
]

wavs, sr = model.generate_voice_clone(
    text=texts,
    language=["Chinese"] * len(texts),
    voice_clone_prompt=prompt_items,
)

for i, w in enumerate(wavs):
    out = f"D:/变声/tts_trial/output_kangaroo_{i}.wav"
    sf.write(out, w, sr)
    print("saved ->", out)
print("done, sample rate:", sr)
