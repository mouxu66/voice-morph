# A/B 音调客观测量：voiced 段 f0 中位数对比
import numpy as np
import soundfile as sf

def f0_median(path):
    import parselmouth
    snd = parselmouth.Sound(path)
    pitch = snd.to_pitch(time_step=0.01, pitch_floor=60, pitch_ceiling=800)
    f0 = pitch.selected_array["frequency"]
    voiced = f0[f0 > 0]
    return float(np.median(voiced)), float(len(voiced) / len(f0))

try:
    a = f0_median(r"D:\变声\media\clips\神人の外卖（5）_哔哩哔_042.wav")
    b = f0_median(r"D:\变声\outputs\offlinevc_1787995813849.wav")
    print(f"A 原声: f0中位数 {a[0]:.0f}Hz, voiced占比 {a[1]:.0%}")
    print(f"B 变声: f0中位数 {b[0]:.0f}Hz, voiced占比 {b[1]:.0%}")
    print(f"音调比 B/A = {b[0] / max(a[0], 1e-9):.2f}  (>1.1 即 B 音调明显升高)")
except Exception as e:
    print(f"EXC: {e}")
