# -*- coding: utf-8 -*-
"""关键核实：那一刀 11kHz 低通，在实际产物上到底切掉了东西没有？

上一支脚本用**合成宽带信号**证明"低通会砍 11k+"，但那只是证明滤波器有效，
**不等于**真实 RVC 输出在 11k+ 有内容。
本脚本直接量**真实产物**（Qwen 输出 / RVC 输出 / RVC 输入），
看 11k 以上到底有多少能量 —— 若本来就接近底噪，那一刀就是无害的（不能邀功）。
"""
import os
import sys

import numpy as np
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")

PPT = r"D:\变声\media\ft_work\ppt12"
RVC = os.path.join(PPT, "myvoice_rvc_full")

BANDS = [(400, 800), (3000, 4000), (6400, 8000), (8000, 10000),
         (10000, 11000), (11000, 12000), (12000, 14000), (14000, 18000)]


def band_db(path, bands=BANDS, ref=(400, 800), nfft=4096, active_pct=60.0):
    a, sr = sf.read(str(path), dtype="float32", always_2d=True)
    a = a.mean(axis=1)
    if a.size < nfft * 4:
        return None, None
    win = np.hanning(nfft).astype(np.float32)
    hop = nfft // 4
    idx = list(range(0, a.size - nfft, hop))
    S = np.stack([a[i:i + nfft] * win for i in idx])
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
    # 取 3 组对照：RVC 的输入（=Qwen 零样本输出）、RVC 输出、真人原音过 RVC
    cases = [
        ("RVC 输入（Qwen 零样本 40k）", os.path.join(RVC, "in_tts01_40k.wav")),
        ("RVC 输出（同一条）", os.path.join(RVC, "out_tts01.wav")),
        ("真人原音过 RVC", os.path.join(RVC, "out_real.wav")),
        ("Qwen 零样本 24k 原始", os.path.join(PPT, "out_ab", "ab_effect_multi", "base_1.wav")),
    ]
    print("=" * 92)
    print("真实产物的逐频带电平（相对 400-800Hz）")
    print("=" * 92)
    hdr = f"{'素材':<28}" + "".join(f"{k:>11}" for k in
                                    ["3-4k", "6.4-8k", "8-10k", "10-11k", "11-12k", "12-14k", "14-18k"])
    print(hdr)
    print("-" * 92)
    for name, p in cases:
        if not os.path.exists(p):
            print(f"{name:<28}  [缺文件]")
            continue
        sr, res = band_db(p)
        if res is None:
            print(f"{name:<28}  [太短]")
            continue
        keys = ["3000-4000", "6400-8000", "8000-10000", "10000-11000",
                "11000-12000", "12000-14000", "14000-18000"]
        line = f"{name:<28}" + "".join(f"{res[k]:>10.1f} " for k in keys)
        print(f"{line}   (sr={sr})")

    print()
    print("=" * 92)
    print("判读")
    print("=" * 92)
    sr_in, r_in = band_db(os.path.join(RVC, "in_tts01_40k.wav"))
    sr_out, r_out = band_db(os.path.join(RVC, "out_tts01.wav"))
    if r_in and r_out:
        for k in ["8000-10000", "10000-11000", "11000-12000", "12000-14000"]:
            print(f"  {k:>12} Hz : 输入 {r_in[k]:>7.1f} dB → 输出 {r_out[k]:>7.1f} dB"
                  f"  （变化 {r_out[k]-r_in[k]:+.1f} dB）")
        print()
        print("  ★ 若 11k+ 在【输出】里本来就 < -50dB（≈底噪），那么那一刀低通**没切掉什么**，")
        print("     不能把'高频空'归到它头上 —— 真相是模型就没重建出那么多高频。")
        print("  ★ 若 11k+ 在输出里有可观能量（如 > -40dB），那一刀就是**真损失**，值得改成自适应。")


if __name__ == "__main__":
    main()
