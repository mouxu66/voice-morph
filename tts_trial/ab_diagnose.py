# -*- coding: utf-8 -*-
"""诊断 B 臂输出到底是什么（是噪声？是语音？是重复？）。"""
import json
import os
import sys
import wave

import numpy as np
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")

AB = r"D:\变声\media\ft_work\ppt12\out_ab"


def info(p):
    a, sr = sf.read(p, dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    d = len(a) / sr
    rms = float(np.sqrt(np.mean(a ** 2) + 1e-12))
    peak = float(np.max(np.abs(a)))
    # 过零率（噪声高、语音低）
    zcr = float(np.mean(np.abs(np.diff(np.sign(a))) > 0) )
    # 静音占比
    fl = int(0.025 * sr)
    nf = len(a) // fl
    fr = a[:nf * fl].reshape(nf, fl)
    r = np.sqrt(np.mean(fr ** 2, axis=1) + 1e-12)
    db = 20 * np.log10(r + 1e-12)
    sil = float(np.mean(db < (np.percentile(db, 5) + 10)))
    return {"dur": round(d, 2), "rms_db": round(20 * np.log10(rms + 1e-12), 1),
            "peak": round(peak, 3), "zcr": round(zcr, 3), "sil_ratio": round(sil, 3),
            "dtype": str(a.dtype)}


print("=" * 96)
print(f"{'样本':<14}{'时长s':>9}{'RMS dB':>9}{'峰值':>8}{'过零率':>9}{'静音比':>9}  判读")
print("-" * 96)
for arm in ("A", "B"):
    d = os.path.join(AB, arm)
    if not os.path.isdir(d):
        continue
    for f in sorted(os.listdir(d)):
        if not f.endswith(".wav"):
            continue
        p = os.path.join(d, f)
        m = info(p)
        if m["zcr"] > 0.35:
            verdict = "噪声样（过零率极高）"
        elif m["dur"] > 30:
            verdict = f"超长失控（{m['dur']:.0f}s）"
        elif m["sil_ratio"] > 0.6:
            verdict = "大量静音"
        else:
            verdict = "正常语音"
        print(f"{arm + '/' + f:<14}{m['dur']:>9.1f}{m['rms_db']:>9.1f}{m['peak']:>8.2f}"
              f"{m['zcr']:>9.3f}{m['sil_ratio']:>9.3f}  {verdict}")
print("=" * 96)

# B 臂首样本频谱快照
p = os.path.join(AB, "B", "01.wav")
if os.path.exists(p):
    a, sr = sf.read(p, dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    print(f"\n[B/01 详细] {len(a)} 样本 @ {sr}Hz = {len(a)/sr:.1f}s")
    seg = a[:sr * 3]
    S = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    f = np.fft.rfftfreq(len(seg), 1 / sr)
    bands = [(0, 100), (100, 300), (300, 1000), (1000, 3000), (3000, 8000)]
    tot = S.sum() + 1e-12
    for lo, hi in bands:
        m = (f >= lo) & (f < hi)
        print(f"   {lo:>5}-{hi:<5}Hz 能量占比 {100*S[m].sum()/tot:5.1f}%")
    # 前后半段是否相似（复读检测）
    h = len(a) // 2
    e1 = np.sqrt(np.mean(a[:h] ** 2))
    e2 = np.sqrt(np.mean(a[h:] ** 2))
    print(f"   前/后半段 RMS: {20*np.log10(e1+1e-12):.1f} / {20*np.log10(e2+1e-12):.1f} dB")
