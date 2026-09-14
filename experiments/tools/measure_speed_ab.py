# A/B 语速客观测量：同一段语音的原声与变声输出，各算音节包络峰值数与有效语音时长
import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt, find_peaks

def envelope(path):
    d, sr = sf.read(path)
    if d.ndim > 1:
        d = d.mean(-1)
    # 包络：全波整流 + 8Hz 低通平滑
    sos = butter(2, 8, fs=sr, btype="lowpass", output="sos")
    env = sosfilt(sos, np.abs(d))
    env = env / (env.max() + 1e-9)
    # 有效语音：包络 > 10% 峰值
    active = float((env > 0.1).sum() / sr)
    # 音节峰：包络上间隔 >120ms 的峰
    peaks, _ = find_peaks(env, height=0.15, distance=int(sr * 0.12))
    return active, len(peaks), sr, len(d) / sr

a = envelope(r"D:\变声\media\clips\第三方素材_042.wav")
b = envelope(r"D:\变声\outputs\offlinevc_1787995813849.wav")
c = envelope(r"D:\变声\outputs\offlinevc_1787998535897.wav")
print(f"A 原声: 有效语音 {a[0]:.2f}s / 文件 {a[3]:.2f}s, 音节峰 {a[1]}")
print(f"B1_meituan: 有效语音 {b[0]:.2f}s / 文件 {b[3]:.2f}s, 音节峰 {b[1]}")
print(f"B2_kangaroo: 有效语音 {c[0]:.2f}s / 文件 {c[3]:.2f}s, 音节峰 {c[1]}")
print(f"音节峰比值 B2/A = {c[1] / max(a[1], 1):.2f}  (>1.15 即 B2 语速更快)")
