# D 试听：用用户勾选聚合的 145s 音色档案 (voicebank/meituan_rat/reference.wav) 做参考
# 档案无逐句文字稿，用 x_vector_only_mode=True（只提取说话人声纹，不用 ICL）
import sys

import soundfile as sf
import torch

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
REF = r"D:/变声/media/voicebank/meituan_rat/reference.wav"
TARGET_TEXT = "巴黎罗，我要掏你炉子了，今天不给你送外卖。"

from qwen_tts import Qwen3TTSModel

model = Qwen3TTSModel.from_pretrained(MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)
prompt = model.create_voice_clone_prompt(ref_audio=REF, ref_text="占位", x_vector_only_mode=True)
wavs, sr = model.generate_voice_clone(text=[TARGET_TEXT], language=["Chinese"], voice_clone_prompt=prompt)
sf.write(r"D:/变声/outputs/ab_D_145s_xvec.wav", wavs[0], sr)
print("saved ab_D_145s_xvec.wav")
