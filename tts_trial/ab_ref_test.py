# A/B 测试：参考音频长度对克隆相似度的影响
# A = 现用 3s 参考(002.wav) | B/C = 10s 候选片段(whisper 转写文字稿)
# 运行：tts_trial/venv312/Scripts/python.exe tts_trial/ab_ref_test.py
import io
import sys

import soundfile as sf
import torch

sys.path.insert(0, r"D:\变声\tts_trial\venv312\Lib\site-packages")

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
TARGET_TEXT = "巴黎罗，我要掏你炉子了，今天不给你送外卖。"

CANDIDATES = [
    ("A_3s_002", r"D:/变声/tts_models/ref/meituan_rat_002.wav",
     "怕被其他人知道这家店给你一个"),  # 现用（原文字稿）
    ("B_10s_042", r"D:/变声/media/clips/神人の外卖（5）_哔哩哔_042.wav", None),  # None=待whisper转写
    ("C_10s_041", r"D:/变声/media/clips/神人の外卖（5）_哔哩哔_041.wav", None),
]


def transcribe(path: str) -> str:
    from faster_whisper import WhisperModel
    m = WhisperModel("small", device="cuda", compute_type="float16")
    segs, _ = m.transcribe(path, language="zh", vad_filter=True)
    return "".join(s.text for s in segs).strip()


from qwen_tts import Qwen3TTSModel

model = Qwen3TTSModel.from_pretrained(MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)

for tag, ref_path, ref_text in CANDIDATES:
    if ref_text is None:
        ref_text = transcribe(ref_path)
        print(f"[{tag}] whisper 文字稿: {ref_text}")
    if not ref_text:
        print(f"[{tag}] 转写为空，跳过")
        continue
    prompt = model.create_voice_clone_prompt(ref_audio=ref_path, ref_text=ref_text)
    wavs, sr = model.generate_voice_clone(text=[TARGET_TEXT], language=["Chinese"], voice_clone_prompt=prompt)
    out = rf"D:/变声/outputs/ab_{tag}.wav"
    sf.write(out, wavs[0], sr)
    print(f"[{tag}] -> {out}")

print("DONE")
