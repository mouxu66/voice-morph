# -*- coding: utf-8 -*-
"""决定性 A/B：换一组**宽频**素材，看那一刀低通是不是真凶。

上一支脚本（真实产物）显示 12-14k 在 PPT 素材的产物里只有 -78dB（≈底噪），
说明对**这份限带素材**而言，11kHz 低通不是瓶颈（模型根本没重建出来）。

但那只证明了"对这份素材无害"，**不能证明"这一刀永远无害"**——
PPT 素材本身 12-14k 就是 -92dB（空的）。要分离变量，必须换一组
**训练集本身有高频**的素材：

    袋鼠素材（自录，另一台设备）—— memory 记录 8-10k 有 -36dB 真实内容。

若在袋鼠素材上：输出 11k+ 仍有可观能量 → 那一刀**确实切掉了真东西**。
本脚本直接量**盘上已有的**袋鼠 VC 产物（不用重跑推理）。
"""
import os
import sys

import numpy as np
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")

BANDS = [(400, 800), (2000, 3000), (4000, 6000), (6400, 8000),
         (8000, 10000), (10000, 11000), (11000, 12000), (12000, 14000),
         (14000, 16000), (16000, 18000)]

CASES = [
    ("袋鼠 参考音（自录素材）", r"D:\变声\media\voicebank\kangaroo\reference.wav"),
    ("袋鼠 RVC 输出 v2_40k", r"D:\变声\outputs\qc\kangaroo_v2_40k_vc_out.wav"),
    ("袋鼠 RVC 输出 v2", r"D:\变声\outputs\qc\kangaroo_vc_out.wav"),
]


def band_db(path, bands=BANDS, ref=(400, 800), nfft=4096, active_pct=60.0):
    a, sr = sf.read(str(path), dtype="float32", always_2d=True)
    a = a.mean(axis=1)
    if a.size < nfft * 4:
        return sr, None
    win = np.hanning(nfft).astype(np.float32)
    hop = nfft // 4
    S = np.stack([a[i:i + nfft] * win for i in range(0, a.size - nfft, hop)])
    rms = np.sqrt((S ** 2).mean(axis=1))
    keep = rms >= np.percentile(rms, active_pct)
    X = np.fft.rfft(S[keep], axis=1)
    P = (np.abs(X) ** 2).mean(axis=0)
    f = np.fft.rfftfreq(nfft, 1.0 / sr)

    def lv(lo, hi):
        m = (f >= lo) & (f < hi)
        return float(P[m].sum()) if m.any() else 1e-30

    base = lv(*ref) + 1e-30
    return sr, {f"{lo}-{hi}": 10 * np.log10((lv(lo, hi) + 1e-30) / base) for lo, hi in bands}


def main():
    keys = ["2000-3000", "4000-6000", "6400-8000", "8000-10000",
            "10000-11000", "11000-12000", "12000-14000", "14000-16000"]
    print("=" * 104)
    print("宽频素材（袋鼠）上的逐频带电平 —— 分离「那一刀」与「模型重建能力」")
    print("=" * 104)
    print(f"{'素材':<26}" + "".join(f"{k:>11}" for k in keys))
    print("-" * 104)
    res_all = {}
    for name, p in CASES:
        if not os.path.exists(p):
            print(f"{name:<26}  [缺文件] {p}")
            continue
        sr, r = band_db(p)
        if r is None:
            print(f"{name:<26}  [太短]")
            continue
        res_all[name] = r
        print(f"{name:<26}" + "".join(f"{r[k]:>10.1f} " for k in keys) + f"  (sr={sr})")

    print()
    print("=" * 104)
    print("判读")
    print("=" * 104)
    out = res_all.get("袋鼠 RVC 输出 v2_40k")
    if out:
        for k in ("8000-10000", "10000-11000", "11000-12000", "12000-14000"):
            v = out[k]
            verdict = "有真实内容" if v > -50 else "≈底噪"
            print(f"  输出 {k:>12} Hz : {v:>7.1f} dB   {verdict}")
        print()
        hi = out.get("11000-12000", -999)
        if hi > -50:
            print("  ★ 11-12k 有真实内容（> -50dB）⇒ **那一刀低通切掉了真东西**，")
            print("     与「用户录不到高频」无关，是**我们能改的**。")
        else:
            print("  ★ 11-12k 仍是底噪 ⇒ 对袋鼠素材也没砍到东西，")
            print("     说明瓶颈在 RVC 前端 16kHz（8k 以上本来就没有特征信息）。")


if __name__ == "__main__":
    main()
