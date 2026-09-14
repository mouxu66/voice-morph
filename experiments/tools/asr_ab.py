# 可懂度对比：用 faster-whisper 转写 原声 / meituan 输出 / kangaroo 输出
from faster_whisper import WhisperModel

m = WhisperModel("small", device="cuda", compute_type="float16")
files = [
    ("A原声", r"D:\变声\media\clips\第三方素材_042.wav"),
    ("B1_meituan", r"D:\变声\outputs\offlinevc_1787995813849.wav"),
    ("B2_kangaroo", r"D:\变声\outputs\offlinevc_1787998535897.wav"),
]
for tag, p in files:
    try:
        segs, _ = m.transcribe(p, language="zh", vad_filter=True)
        text = "".join(s.text for s in segs).strip()
        print(f"{tag}: {text if text else '(未识别出语音)'}")
    except Exception as e:
        print(f"{tag}: EXC {e}")
