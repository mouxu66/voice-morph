# 反听 v3 生成样本，测可懂度（是否说对输入文本）
import os, numpy as np, soundfile as sf
from faster_whisper import WhisperModel

refs = [
    ("outputs/ft_aligned_v3/aligned_1.wav", "欢迎使用美团外卖，您的订单已经送达，请及时取餐。"),
    ("outputs/ft_aligned_v3/aligned_2.wav", "今天天气真好，我们一起去公园散步吧！"),
    ("outputs/ft_aligned_v3/aligned_3.wav", "您好，我是袋鼠骑士，很高兴为您服务。"),
    ("outputs/ft_aligned_v3/aligned_greedy_1.wav", "欢迎使用美团外卖，您的订单已经送达，请及时取餐。"),
]
model = WhisperModel("small", device="cpu", compute_type="int8")
print("=== v3 生成样本 whisper 反听 ===")
for f, gt in refs:
    if not os.path.exists(f):
        print(f"  MISSING {f}"); continue
    a, sr = sf.read(f)
    if a.ndim > 1: a = a[:, 0]
    a = (a.astype(np.float32) * 32767).astype(np.int16)
    segs, _ = model.transcribe(a, language="zh", beam_size=5)
    txt = "".join(s.text for s in segs).strip()
    print(f"\n[文件] {os.path.basename(f)}")
    print(f"  输入 : {gt}")
    print(f"  反听 : {txt}")
