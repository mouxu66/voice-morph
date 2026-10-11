# -*- coding: utf-8 -*-
"""核对控制实验的四个输出：转写内容 + 峰值 + 与 8 月验收口径对照。

8 月修复记录的原话：旧 B_old 峰值顶到 int16 上限（32256→1.0），原音峰值 16228（≈0.5）。
所以 peak≈1.0 本身就是"崩坏"的指纹。
"""
import os
import sys

import numpy as np
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")

CTRL = r"D:\变声\media\ft_work\ppt12\out_ab\ctrl"

from faster_whisper import WhisperModel
wm = WhisperModel("small", device="cuda", compute_type="float16")

print("=" * 100)
print(f"{'案例':<12}{'时长s':>8}{'峰值':>8}{'RMS dB':>9}{'削波%':>8}  转写内容")
print("-" * 100)
for f in sorted(os.listdir(CTRL)):
    if not f.endswith(".wav"):
        continue
    p = os.path.join(CTRL, f)
    a, sr = sf.read(p, dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    peak = float(np.max(np.abs(a)))
    rms = float(np.sqrt(np.mean(a ** 2) + 1e-12))
    clip = float(np.mean(np.abs(a) >= 0.999))
    segs, _ = wm.transcribe(p, language="zh", vad_filter=False)
    txt = "".join(s.text for s in segs)
    print(f"{f[:-4]:<12}{len(a)/sr:>8.1f}{peak:>8.2f}{20*np.log10(rms+1e-12):>9.1f}"
          f"{100*clip:>8.2f}  {txt}")

print("=" * 100)
print("\n判据（8月修复记录口径）:")
print("  peak ≈ 1.00 / 削波 >0  -> 崩坏（旧 B_old 指纹）")
print("  peak ≈ 0.2~0.5 且转写可懂 -> 正常")
