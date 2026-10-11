# -*- coding: utf-8 -*-
"""诊断 B 臂音频内容：转写全文 + 波形/频谱特征。"""
import os
import sys

import numpy as np
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")

AB = r"D:\变声\media\ft_work\ppt12\out_ab"
from faster_whisper import WhisperModel
wm = WhisperModel("small", device="cuda", compute_type="float16")

for arm in ("A", "B"):
    d = os.path.join(AB, arm)
    if not os.path.isdir(d):
        continue
    print("=" * 90)
    print(f"### {arm} 臂")
    for f in sorted(os.listdir(d)):
        if not f.endswith(".wav"):
            continue
        p = os.path.join(d, f)
        a, sr = sf.read(p, dtype="float32")
        if a.ndim > 1:
            a = a.mean(axis=1)
        segs, _ = wm.transcribe(p, language="zh", vad_filter=False)
        txt = "".join(s.text for s in segs)
        # 基频估计（自相关）
        x = a[:int(sr * 0.5)]
        x = x - x.mean()
        if x.size > 100:
            ac = np.correlate(x, x, "full")[x.size - 1:]
            ac /= (ac[0] + 1e-12)
            lo, hi = int(sr / 400), int(sr / 60)
            if hi < ac.size:
                f0 = sr / (lo + int(np.argmax(ac[lo:hi])))
            else:
                f0 = 0
        else:
            f0 = 0
        print(f"  {f}  {len(a)/sr:5.1f}s  f0≈{f0:5.0f}Hz  peak={np.max(np.abs(a)):.2f}")
        print(f"      ASR: {txt}")
