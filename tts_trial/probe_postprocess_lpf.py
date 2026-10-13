# -*- coding: utf-8 -*-
"""只读核实：我们自己的后处理把 11kHz 以上砍掉了多少？

发现（承 probe_band_reality.py）：
  用户素材的高频上限本来就在 8k 附近（我们管不了），
  但 `offline_vc_infer.postprocess_audio()` 里还有一刀：

      cutoff = min(11000.0, out_sr * 0.45)
      sos = butter(4, cutoff, fs=out_sr, btype="lowpass")
      y = sosfilt(sos, y)

  即**输出恒被 4 阶低通到 11kHz**。40k 底模的 Nyquist 是 20kHz ——
  也就是说：**即使模型重建出了 11k~20k 的内容，也会被这一刀切掉。**

这一刀是我们自己加的（注释写的是"去刺耳高频"），**不是用户设备的问题**。
本脚本量化它切掉了多少，并对照：不加这一刀会多出什么。
"""
import sys

import numpy as np
from scipy.signal import butter, sosfilt

sys.path.insert(0, r"D:\变声\m2_server")
sys.stdout.reconfigure(encoding="utf-8")

SR = 40000
DUR = 2.0
CUTOFF = min(11000.0, SR * 0.45)          # 复刻 postprocess_audio 的取值


def band_energy(y, sr, lo, hi, nfft=4096):
    n = min(len(y), sr * 2)
    win = np.hanning(nfft)
    frames = [y[i:i + nfft] * win for i in range(0, n - nfft, nfft // 2)]
    if not frames:
        return 0.0
    X = np.fft.rfft(np.stack(frames), axis=1)
    P = (np.abs(X) ** 2).mean(axis=0)
    f = np.fft.rfftfreq(nfft, 1.0 / sr)
    m = (f >= lo) & (f < hi)
    return float(P[m].sum()) if m.any() else 0.0


def main():
    print("=" * 78)
    print("复刻 postprocess_audio 的低通，量它切掉了什么")
    print("=" * 78)
    print(f"输出采样率 {SR} Hz ⇒ Nyquist {SR/2:.0f} Hz")
    print(f"低通截止 cutoff = min(11000, {SR}*0.45) = **{CUTOFF:.0f} Hz**\n")

    rng = np.random.default_rng(0)
    # 造一段"模型可能重建出的"宽带信号：语音基频 + 到 20kHz 的谐波/噪声
    t = np.arange(int(SR * DUR)) / SR
    y = 0.4 * np.sin(2 * np.pi * 150 * t)              # 基频
    y += 0.10 * np.sin(2 * np.pi * 3000 * t)           # 中频
    y += 0.05 * rng.normal(0, 1, t.size)               # 宽带成分（含高频）
    y = y.astype(np.float32)

    sos = butter(4, CUTOFF, fs=SR, btype="lowpass", output="sos")
    y_lp = sosfilt(sos, y).astype(np.float32)

    bands = [(400, 800), (3000, 4000), (8000, 10000), (10000, 11000),
             (11000, 12000), (12000, 14000), (14000, 18000)]
    ref_raw = band_energy(y, SR, *bands[0]) + 1e-30
    ref_lp = band_energy(y_lp, SR, *bands[0]) + 1e-30

    print(f"{'频带 (Hz)':<16}{'低通前':>12}{'低通后':>12}{'被削掉':>12}")
    print("-" * 78)
    for lo, hi in bands:
        a = 10 * np.log10((band_energy(y, SR, lo, hi) + 1e-30) / ref_raw)
        b = 10 * np.log10((band_energy(y_lp, SR, lo, hi) + 1e-30) / ref_lp)
        cut = b - a
        mark = "   ← ★ 被砍" if cut < -6 else ""
        print(f"{lo}-{hi:<12}{a:>10.1f}dB{b:>10.1f}dB{cut:>+10.1f}dB{mark}")

    print()
    print("=" * 78)
    print("结论")
    print("=" * 78)
    print(f"""
· 11kHz 以上被削 **几十 dB**（见上表「被削掉」列）—— 这是**我们自己的后处理**，
  与用户录什么完全无关。40k 底模的可用带宽本该到 20kHz，实际只放行到 {CUTOFF/1000:.0f}kHz。

· 所以「高频空」有两个**性质完全不同**的来源，不能混为一谈：
    来源 A（用户侧，管不了）：设备/软件限带 ⇒ 素材本身 8k 以上就空
    来源 B（我们侧，**能改**）：postprocess 恒定 11kHz 低通 ⇒ 把重建出的高频又切掉

· 这一刀当初是为了"去刺耳"（RVC 输出有高频毛刺）。
  ⇒ 正确做法不是"教用户录高频"（用户质疑②成立），
     而是**把这一刀改成自适应**：只在该衰减的地方衰减，别一刀切掉整个高频段。
""", )


if __name__ == "__main__":
    main()
