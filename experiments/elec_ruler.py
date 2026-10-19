# -*- coding: utf-8 -*-
"""「电音/毛刺」的量具 —— 先给尺子定标，再用它量产物。

为什么不用上一版的「谐波峰/谐间能量比」（2026-10-15 实测，尺子没过）
    那版量出来对已知 SNR **不单调**（-10dB→-1.5、0dB→+1.1、10dB→+0.1、
    20dB→-0.4、30dB→-0.2、45dB→-0.2），却在产物上"读出"了漂亮的分箱趋势 ——
    **尺子不过关，后面所有数字都不算数**（同族：`docs/犯错指南.md` #178
    「"没差异"的结论必须先给尺子定分辨率」、#171「下结论前先验尺子」）。

本模块的口径
    电音/沙哑在**逐帧自相关**上表现为**周期性被破坏**：把帧做归一化自相关，
    取 F0 处的峰高（`peak_at_f0`）—— 纯周期信号接近 1，噪声/毛刺趋近 0。
    比谱方法稳，且不依赖 F0 估计之外的任何参数。

可测性：`peak_at_f0` 是**纯函数**（只要 numpy），标定与守卫都直接喂合成信号。
"""
from __future__ import annotations

import numpy as np

#: 自相关窗长（样本）。40kHz 下 50ms —— 够容纳 65Hz 的 3 个周期。
FRAME = 2048
HOP = 512
#: 逐帧 RMS 门限：低于它的帧不参与统计（静音帧的"自相关峰"没有意义）
SILENCE_RMS = 3e-4


def peak_at_f0(frame: np.ndarray, sr: int, f0: float) -> float | None:
    """帧在 F0 处的归一化自相关峰（0..1）。1 = 完美周期，越小越噪/越毛刺。

    做法：去均值 → FFT 求自相关 → 在 lag = sr/f0 附近取最大值。

    ★★ 2026-10-19 修：旧版用 `ac / ac[0]` 归一，而自相关在 lag=τ 处的"重叠样本数"
    只有 n−τ 个 —— 于是**纯正弦也会被读成 1−τ/n**：
        108Hz（τ≈370）上限 0.819、325Hz（τ≈123）上限 0.940。
    这正是上一轮"周期峰随音高单调上升"的来源（实测 0.670→0.869，看起来像
    "高音更周期"，其实是**尺子随音高变松**）。现在改成逐 lag 归一：
        r(τ) = Σx[i]x[i+τ] / sqrt(Σx[i]² · Σx[i+τ]²)
    纯正弦在任意 lag 都读 1.00，音高不再改变上限。

    ⚠️ 只在 `[0.85, 1.15] × lag` 内找峰：不设窗的话，lag→0 处恒为 1，
    会把任何信号都读成"完美周期"。
    """
    x = np.asarray(frame, dtype=np.float64)
    x = x - x.mean()
    n = len(x)
    if f0 <= 0 or n < 8 or float((x * x).sum()) <= 1e-18:
        return None
    lag = sr / f0
    lo, hi = int(max(1, lag * 0.85)), int(min(n - 2, lag * 1.15))
    if hi <= lo:
        return None
    # 补零到 2n 做线性（非循环）自相关，避免回绕
    X = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(X * np.conj(X), 2 * n)[:n]
    # 逐 lag 的能量前缀：分母只算**实际重叠**的那一段
    csum = np.concatenate(([0.0], np.cumsum(x * x)))
    best = 0.0
    for t in range(lo, hi + 1):
        e0 = csum[n - t]          # x[0 .. n-t-1]
        e1 = csum[n] - csum[t]    # x[t .. n-1]
        den = float(np.sqrt(max(e0, 0.0) * max(e1, 0.0)))
        if den <= 1e-18:
            continue
        best = max(best, float(ac[t] / den))
    return best


def frame_stats(path, sr_target: int = 40000, *, fmin: float = 65.0,
                fmax: float = 1400.0):
    """整文件的逐帧 (f0, 周期峰) 列表 —— 只在浊音且有声的帧上取。"""
    import librosa
    import soundfile as sf

    d, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = d.mean(1)
    if sr != sr_target:
        mono = librosa.resample(mono, orig_sr=sr, target_sr=sr_target)
        sr = sr_target
    f0, _vf, _vp = librosa.pyin(mono, fmin=fmin, fmax=fmax, sr=sr,
                                hop_length=HOP, frame_length=FRAME)
    f0 = np.nan_to_num(np.asarray(f0, dtype=np.float64), nan=0.0)
    rows = []
    for i in range(len(f0)):
        s = i * HOP
        if s + FRAME > len(mono):
            break
        f = f0[i]
        if f <= 0:
            continue
        fr = mono[s:s + FRAME]
        if float(np.sqrt((fr ** 2).mean())) < SILENCE_RMS:
            continue
        v = peak_at_f0(fr, sr, f)
        if v is not None:
            rows.append((float(f), float(v)))
    return rows


def summarise(rows) -> dict:
    """把逐帧结果收成一行统计（含按输出音高分箱）。"""
    if not rows:
        return {"frames": 0}
    a = np.array(rows)
    f, v = a[:, 0], a[:, 1]
    out = {
        "frames": int(len(v)),
        "f0_median": round(float(np.median(f)), 1),
        "periodicity_p10": round(float(np.percentile(v, 10)), 4),
        "periodicity_p50": round(float(np.median(v)), 4),
        "periodicity_p90": round(float(np.percentile(v, 90)), 4),
    }
    edges = [0, 130, 170, 220, 280, 350, 450, 600, 1500]
    bands = {}
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (f >= lo) & (f < hi)
        if m.sum() < 15:
            continue
        bands[f"{lo}-{hi}"] = {"n": int(m.sum()),
                               "p50": round(float(np.percentile(v[m], 50)), 4)}
    out["by_f0"] = bands
    return out
