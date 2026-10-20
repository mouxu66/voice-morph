"""X3：修 loud 段的「谱平坦度反转」。

X2 只修了气口（silent+soft 占 33%），loud 段 61% 原样保留 D1 的高频。
全曲实测（8–16k 谱平坦度 SFM，1=白噪 0=纯音）：

    档       原唱    D1_BWE   偏差
    silent   0.483   0.096   −0.387   ← 太纯，听感「嗡」
    soft     0.305   0.147   −0.158
    mid      0.189   0.186    0.000
    loud     0.183   0.280   +0.098   ← 太散，听感「嘶」

真人的规律是「越响越谐波化」（0.483→0.183 单调下降），D1 是**反的**（0.096→0.280
单调上升）。两个方向都错，而 X2 只修了低能量那一头 —— 用户听感「还是全是电音」
就来自 loud 段这 61%。

X3 = X2 的能量门控 + 对 D 的高频做谐波梳状整形（用 A 估的 f0 加谱对比度）。
只动 8k 以上，8k 以下逐样本等于 D1。

跑法：python experiments/cover_elec_x3.py [dry-run]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "media/covers/midsong_20261018/chengdu_bwe_full"
A_SRC = FULL / "A_full_post.wav"
D_SRC = FULL / "D1_bwe_post.wav"
OUT = FULL / "gate" / "X3_sharp.wav"

SR = 40000
WIN = 2048
HOP = 512
CUT = 8000.0

#: 能量 → 用 D 的权重（与 X2 同一套点，气口严格 0）
MIX_POINTS = [(-55.0, 0.0), (-40.0, 0.0), (-30.0, 0.0),
              (-25.0, 0.40), (-19.0, 0.80), (-14.0, 1.0), (20.0, 1.0)]

#: 谐波梳的深度：0=不动，1=把半谐波处压到 0
SHARPEN = 0.55
#: 弱化整形：f0 置信度低的帧（清音/摩擦）不整形，否则 s/sh 会被做成嗡嗡声
CONF_GATE = 0.30


def read(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return y.mean(axis=1).astype(np.float64), int(sr)


def frame_lo_db(y: np.ndarray) -> np.ndarray:
    """0–3k 帧能量（dB），与 X2 同口径。"""
    win = np.hanning(WIN)
    freqs = np.fft.rfftfreq(WIN, 1.0 / SR)
    m = (freqs >= 200) & (freqs < 3000)
    nf = 1 + (len(y) - WIN) // HOP
    idx = np.arange(WIN)[None, :] + HOP * np.arange(nf)[:, None]
    seg = (y[idx] - y[idx].mean(axis=1, keepdims=True)) * win
    p = np.abs(np.fft.rfft(seg, axis=1)) ** 2
    return 10 * np.log10(np.maximum(p[:, m].sum(axis=1), 1e-20))


def track_f0(y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """逐帧自相关基频与置信度（0–1）。用 8k 以下，A 与 D 在这一段完全一致。"""
    lo, hi = 70.0, 450.0
    n = 1024
    hop = HOP
    nf = 1 + (len(y) - WIN) // hop
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    f0 = np.zeros(nf)
    conf = np.zeros(nf)
    fmin = int(SR / hi)
    fmax = int(SR / lo)
    win = np.hanning(n)
    for i in range(nf):
        s = i * hop
        seg = y[s:s + n]
        if seg.size < n:
            break
        seg = (seg - seg.mean()) * win
        if np.sqrt((seg ** 2).mean()) < 1e-4:
            continue
        p = np.abs(np.fft.rfft(seg, nfft)) ** 2
        ac = np.fft.irfft(p, nfft)[:n]
        if ac[0] <= 0:
            continue
        ac = ac / ac[0]
        a, b = fmin, min(fmax, n - 1)
        if b <= a:
            continue
        k = a + int(np.argmax(ac[a:b]))
        f0[i] = SR / k
        conf[i] = float(ac[k])
    return f0, conf


def harmonic_mask(freqs: np.ndarray, f0: float) -> np.ndarray:
    """梳状掩模：谐波峰处 1，半谐波处 1−SHARPEN，平滑过渡。"""
    q = freqs / max(f0, 1e-6)
    d = np.abs(q - np.round(q))  # 0（峰）… 0.5（谷）
    return 1.0 - SHARPEN * np.clip(d / 0.5, 0.0, 1.0) ** 2


def main() -> int:
    a, sr_a = read(A_SRC)
    d, sr_d = read(D_SRC)
    assert sr_a == sr_d == SR, f"采样率不一致 {sr_a}/{sr_d}"
    n = min(len(a), len(d))
    a, d = a[:n], d[:n]
    print(f"输入 {n/SR:.2f}s @ {SR}")

    env = frame_lo_db(d)
    xs = np.array([p[0] for p in MIX_POINTS])
    ys = np.array([p[1] for p in MIX_POINTS])
    w = np.interp(env, xs, ys)
    print("w 分位:", [round(float(np.percentile(w, p)), 3) for p in (5, 25, 50, 75, 95)])

    f0, conf = track_f0(a)
    voiced = conf >= CONF_GATE
    print(f"f0 可信帧 {100*voiced.mean():.0f}%，f0 中位 {np.median(f0[voiced]):.0f}Hz")

    win = np.hanning(WIN)
    freqs = np.fft.rfftfreq(WIN, 1.0 / SR)
    hi_m = freqs >= CUT
    nf = 1 + (n - WIN) // HOP
    pad = np.zeros(nf * HOP + WIN - n + HOP)
    ap, dp = np.concatenate([a, pad]), np.concatenate([d, pad])
    out = np.zeros_like(dp)
    wsum = np.zeros_like(dp)
    shaped_n = 0

    for i in range(nf):
        s = i * HOP
        SA = np.fft.rfft(ap[s:s + WIN] * win)
        SD = np.fft.rfft(dp[s:s + WIN] * win)
        wt = w[min(i, len(w) - 1)]
        SDh = SD[hi_m]
        # ★ 只对可信浊音帧整形：清音（s/sh/t）本来就是宽带，做成梳状会变「嗡嗡」
        if voiced[min(i, len(voiced) - 1)]:
            SDh = SDh * harmonic_mask(freqs[hi_m], f0[i])
            shaped_n += 1
        S = SD.copy()
        S[hi_m] = SA[hi_m] * (1.0 - wt) + SDh * wt
        out[s:s + WIN] += np.fft.irfft(S, WIN) * win
        wsum[s:s + WIN] += win ** 2

    mixed = (out / np.maximum(wsum, 1e-8))[:n]
    print(f"整形帧占比 {100*shaped_n/max(nf,1):.1f}%")

    # ---- 核对：8k 以下必须与 D1 逐样本相等 ----
    from scipy.signal import butter, sosfilt
    ny = SR / 2
    sos = butter(4, CUT / ny, btype="low", output="sos")
    lo_mix, lo_d = sosfilt(sos, mixed), sosfilt(sos, d)
    print(f"8k 以下 vs D1：max|Δ| {np.abs(lo_mix-lo_d).max():.2e}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(OUT), mixed.astype(np.float32), SR, subtype="PCM_16")
    meta = {"sfm_sharpen": SHARPEN, "conf_gate": CONF_GATE,
            "voiced_pct": round(100 * float(voiced.mean()), 1),
            "f0_median": round(float(np.median(f0[voiced])), 1)}
    (OUT.parent / "x3_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    print(f"→ {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
