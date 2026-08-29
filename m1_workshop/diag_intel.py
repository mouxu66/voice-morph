# 反听验收：把我们生成的 wav 用 whisper 再转写，看是否对得上输入文本（测可懂度）
from faster_whisper import WhisperModel
import os

PROMPTS = {
    "outputs/ft_aligned_v2/aligned_1.wav": "欢迎使用美团外卖，您的订单已经送达，请及时取餐。",
    "outputs/ft_aligned_v2/aligned_2.wav": "今天天气真好，我们一起去公园散步吧！",
    "outputs/ft_aligned_v2/aligned_3.wav": "您好，我是袋鼠骑士，很高兴为您服务。",
}
m = WhisperModel("small", device="auto", compute_type="int8")
for f, prompt in PROMPTS.items():
    if not os.path.exists(f):
        print("MISSING", f); continue
    segs, _ = m.transcribe(f, language="zh", beam_size=5)
    heard = "".join(s.text for s in segs).strip()
    print(f"\n[输入] {prompt}")
    print(f"[输出被听到] {heard}")
    print(f"  -> 对得上? {'部分' if any(c in heard for c in prompt[:4]) else '基本不'}")
