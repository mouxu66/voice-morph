"""按段落统计「电音强度」：为什么用户听 c_tail 全是电音、b_mid 只有一点。

用户裁决（2026-10-20 13:09）：
  「CE 的这个全是电音，还是夹着嗓子唱的，这个 b 的有带点电音」

上一轮把全曲 24.7% 的气口按时长平均，掩盖了「电音强度随段落变化」这件事。
本脚本按**页面上那三段试听的真实窗口**（0-20 / 110-150 / 285-320）分别统计，
看"全是电音"的段落是不是气口占比更高。

同时回答第二个现象「夹着嗓子唱」—— 这一条 BWE 完全管不到 8k 以下，
所以它必须在 A 臂（干声）里就已经存在，用 H1-H2 与谱倾斜量发声紧张度。

跑法：python experiments/cover_clip_attrib.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "media/covers/midsong_20261018/chengdu_bwe_full"
DRY = ROOT / "media/covers/midsong_20261018/chengdu/P0_原调_raw.wav"
REF = ROOT / "media/covers/midsong_20261018/candidates/成都-赵雷/vocals.wav"

SR = 40000
WIN = int(0.064 * SR)
HOP = int(0.010 * SR)

#: 页面上三段试听的真实窗口（秒）
CLIPS = [(0.0, 20.0, "a_intro"), (110.0, 40.0, "b_mid"), (285.0, 35.0, "c_tail")]

#: 0–3k 帧能量分档（dB）。silent 是"气口/字缝"，soft 是过渡区。
TIERS = [("silent", -1e9, -40.0), ("soft", -40.0, -20.0),
         ("mid", -20.0, -8.0), ("loud", -8.0, 1e9)]

ARMS = [("A_dry", FULL / "A_full_post.wav"),
        ("D1_bwe", FULL / "D1_bwe_post.wav"),
        ("G2_gate", FULL / "gate/G2_stft_8k.wav"),
        ("X2_cross", FULL / "gate/X2_hi_cross.wav")]


def read(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return y.mean(axis=1).astype(np.float64), int(sr)


def frames(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 (低频能量 dB, 高频能量 dB, 8–16k 幅度谱)。"""
    n = int(0.064 * sr)
    h = int(0.010 * sr)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    win = np.hanning(n)
    scl = (win ** 2).sum()
    lo_m = (freqs >= 200) & (freqs < 3000)
    hi_m = (freqs >= 8000) & (freqs < 16000)
    nf = 1 + (len(y) - n) // h
    idx = np.arange(n)[None, :] + h * np.arange(nf)[:, None]
    seg = (y[idx] - y[idx].mean(axis=1, keepdims=True)) * win
    p = np.abs(np.fft.rfft(seg, axis=1)) ** 2 / scl
    elo = 10 * np.log10(np.maximum(p[:, lo_m].sum(axis=1), 1e-20))
    ehi = 10 * np.log10(np.maximum(p[:, hi_m].sum(axis=1), 1e-20))
    return elo, ehi, np.abs(np.fft.rfft(seg, axis=1))[:, hi_m]


def sfm_crest(mag: np.ndarray) -> tuple[float, float]:
    """谱平坦度（1=白噪 0=纯音）与谱峰锐度（dB）。"""
    p = np.maximum(mag ** 2, 1e-24)
    s = float(np.median(np.exp(np.mean(np.log(p), axis=1)) / np.maximum(np.mean(p, axis=1), 1e-24)))
    c = float(np.median(10 * np.log10(p.max(axis=1) / np.maximum(np.median(p, axis=1), 1e-24))))
    return s, c


def h1h2(y: np.ndarray, sr: int) -> float:
    """H1−H2（dB）：发声越紧（挤嗓/压喉）这个值越小。只取有声帧。"""
    from scipy.signal import find_peaks

    n = int(0.064 * sr)
    h = int(0.010 * sr)
    win = np.hanning(n)
    nf = 1 + (len(y) - n) // h
    idx = np.arange(n)[None, :] + h * np.arange(nf)[:, None]
    seg = (y[idx] - y[idx].mean(axis=1, keepdims=True)) * win
    spec = np.abs(np.fft.rfft(seg, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    lo = 10 * np.log10(np.maximum(spec[:, (freqs >= 200) & (freqs < 3000)].sum(axis=1), 1e-20))
    out = []
    for i in range(0, nf, 3):
        if lo[i] < -25:  # 只统计有声帧
            continue
        s = spec[i]
        cand, _ = find_peaks(s, height=s.max() * 0.02, distance=3)
        cand = cand[(freqs[cand] > 60) & (freqs[cand] < 500)]
        if cand.size < 2:
            continue
        f0 = freqs[cand[0]]
        b1 = np.argmin(np.abs(freqs - f0))
        b2 = np.argmin(np.abs(freqs - 2 * f0))
        if b1 == b2 or s[b1] <= 0 or s[b2] <= 0:
            continue
        out.append(10 * np.log10(s[b1] / s[b2]))
    return float(np.median(out)) if out else float("nan")


def tilt(y: np.ndarray, sr: int) -> float:
    """300–3000Hz 长时平均谱斜率（dB/oct）。挤嗓会让高端相对抬升、斜率变平。"""
    n = 4096
    win = np.hanning(n)
    nf = 1 + (len(y) - n) // (n // 2)
    acc = np.zeros(n // 2 + 1)
    for i in range(nf):
        s = i * (n // 2)
        acc += np.abs(np.fft.rfft(y[s:s + n] * win)) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    m = (freqs >= 300) & (freqs <= 3000)
    x = np.log2(freqs[m])
    yv = 10 * np.log10(np.maximum(acc[m], 1e-20))
    return float(np.polyfit(x, yv, 1)[0])


def band_stats(elo: np.ndarray, ehi: np.ndarray, mag: np.ndarray) -> dict:
    out = {}
    for name, a, b in TIERS:
        sel = (elo > a) & (elo <= b)
        if sel.sum() < 20:
            continue
        s, c = sfm_crest(mag[sel])
        out[name] = {"pct": round(100 * float(sel.mean()), 1),
                     "ehi_db": round(float(np.median(ehi[sel])), 1),
                     "sfm": round(s, 3), "crest": round(c, 1)}
    return out


def main() -> int:
    y_dry, sr_dry = read(DRY)
    y_ref, sr_ref = read(REF)
    print(f"干声 {len(y_dry)/sr_dry:.1f}s@{sr_dry}  参考 {len(y_ref)/sr_ref:.1f}s@{sr_ref}\n")

    rows = []
    for t0, dur, tag in CLIPS:
        for name, path in ARMS:
            y, sr = read(path)
            a, b = int(t0 * sr), int((t0 + dur) * sr)
            elo, ehi, mag = frames(y[a:b], sr)
            rec = {"clip": tag, "arm": name, "sr": sr,
                   "hi_all_db": round(float(np.median(ehi)), 1)}
            rec.update(band_stats(elo, ehi, mag))
            rows.append(rec)

    print("=== 各段 8–16k（分档统计，绝对 dB）===")
    print(f"{'段':<8}{'臂':<10}{'silent%':>9}{'silent 电平':>13}{'SFM':>7}{'crest':>7}"
          f"{'soft%':>7}{'soft 电平':>11}{'SFM':>7}{'全段':>8}")
    for r in rows:
        si, so = r.get("silent", {}), r.get("soft", {})
        print(f"{r['clip']:<8}{r['arm']:<10}{si.get('pct',0):>8.1f}%{si.get('ehi_db',0):>13.1f}"
              f"{si.get('sfm',0):>7.3f}{si.get('crest',0):>7.1f}"
              f"{so.get('pct',0):>6.1f}%{so.get('ehi_db',0):>11.1f}{so.get('sfm',0):>7.3f}"
              f"{r['hi_all_db']:>8.1f}")

    print("\n=== 「夹着嗓子」：干声 vs 原唱（BWE 改不到 8k 以下，所以这条只能在 A 臂里）===")
    print(f"{'段':<8}{'源':<8}{'H1−H2 dB':>12}{'谱倾斜 dB/oct':>16}")
    for t0, dur, tag in CLIPS:
        for name, y, sr in [("原唱", y_ref, sr_ref), ("RVC干声", y_dry, sr_dry)]:
            a, b = int(t0 * sr), int((t0 + dur) * sr)
            print(f"{tag:<8}{name:<8}{h1h2(y[a:b], sr):>12.2f}{tilt(y[a:b], sr):>16.2f}")

    out = ROOT / "media/covers/midsong_20261018/chengdu_bwe_full/clip_attrib.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
