"""「开 BWE 就有电音、不开没有」的机制归因（2026-10-20）。

用户裁决：② 不电、③ 电。上一轮只测了谱形（11–16k 上翘），解释不了"开了才有"。
本脚本把「电」拆成几类可测的物理量，逐一在整曲上量，看哪一条只属于 BWE 臂。

候选机制（每条都要能"只在 D 臂成立、A 臂不成立"）：
  H1 气口残留     静音段 8–16k 是否被凭空填了能量（模型不知道这是气口）
  H2 块界凹陷     UniverSR 每块 seed 不同 → 相邻块高频不相关 → 线性交叉淡化
                  在重叠区掉 −3dB（sqrt(0.5²+0.5²)=0.707）。周期 = hop 4.7s
  H3 包络失锁     8–16k 包络 vs 0–3k 包络的相关/延迟（生成的高频不跟发声走）
  H4 调制谱峰     高频包络频谱在 1/4.7s=0.213Hz 及其倍频是否有峰（呼吸感）
  H5 谱平坦度     8–16k 的 SFM（太干净=谐波堆叠 / 太脏=噪声底）
  H6 上翘         11–16k 相对 8–11k 的倾斜（上一轮已测，作为对照）

用法：
    python experiments/cover_elec_diagnose.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "media/covers/midsong_20261018/chengdu_bwe_full"
SLICE = ROOT / "media/covers/midsong_20261018/chengdu"
REF = ROOT / "media/covers/midsong_20261018/candidates/成都-赵雷/vocals.wav"

# UniverSR 跑手的实际分块口径（arms.json: chunks=69, chunk_s=5.0, overlap_s=0.3）
CHUNK_S = 5.0
OVERLAP_S = 0.3
HOP_S = CHUNK_S - OVERLAP_S      # 4.7

WIN_S = 0.032
FRAME_HOP_S = 0.010

BANDS = {
    "low_0_3k": (0.0, 3000.0),
    "mid_3_8k": (3000.0, 8000.0),
    "hi_8_12k": (8000.0, 12000.0),
    "hi_8_16k": (8000.0, 16000.0),
    "air_11_16k": (11000.0, 16000.0),
    "uhf_16_20k": (16000.0, 20000.0),
}


def load(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return y.mean(axis=1), int(sr)


def frame_power(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """(帧数, 频点数) 的功率谱，以及频率轴。"""
    n = int(WIN_S * sr)
    h = int(FRAME_HOP_S * sr)
    if len(y) < n:
        raise SystemExit(f"音频太短：{len(y)} < {n}")
    nf = 1 + (len(y) - n) // h
    idx = np.arange(n)[None, :] + h * np.arange(nf)[:, None]
    win = np.hanning(n).astype(np.float32)
    # 去掉每帧均值，避免 DC 泄漏到低频带
    seg = y[idx]
    seg = (seg - seg.mean(axis=1, keepdims=True)) * win
    spec = np.fft.rfft(seg, axis=1)
    scl = (win ** 2).sum()
    p = (spec.real ** 2 + spec.imag ** 2) / scl
    return p, np.fft.rfftfreq(n, 1.0 / sr)


def band_db(p: np.ndarray, f: np.ndarray, lo: float, hi: float) -> np.ndarray:
    m = (f >= lo) & (f < hi)
    if not m.any():
        return np.full(p.shape[0], -200.0)
    return 10.0 * np.log10(np.maximum(p[:, m].sum(axis=1), 1e-20))


def sfm(p: np.ndarray, f: np.ndarray, lo: float, hi: float) -> float:
    """谱平坦度中位数（几何均值/算术均值）。1=纯白噪，0=纯正弦。"""
    m = (f >= lo) & (f < hi)
    if not m.any():
        return float("nan")
    sub = np.maximum(p[:, m], 1e-20)
    g = np.exp(np.mean(np.log(sub), axis=1))
    a = np.mean(sub, axis=1)
    return float(np.median(g / np.maximum(a, 1e-20)))


def crest_hi(p: np.ndarray, f: np.ndarray) -> float:
    """8–16k 内"最强频点 / 该带中位频点"的比（dB）。高=谐波峰尖锐，低=噪声状。"""
    m = (f >= 8000) & (f < 16000)
    sub = p[:, m]
    mx = sub.max(axis=1)
    med = np.median(sub, axis=1)
    r = 10 * np.log10(np.maximum(mx, 1e-20) / np.maximum(med, 1e-20))
    return float(np.median(r))


def smooth(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return x
    ker = np.ones(k, dtype=np.float64) / k
    return np.convolve(x, ker, mode="same")


def analyze(tag: str, path: Path, ref_frames: int | None = None) -> tuple[dict, np.ndarray, np.ndarray]:
    y, sr = load(path)
    p, f = frame_power(y, sr)
    env = {k: band_db(p, f, lo, hi) for k, (lo, hi) in BANDS.items()}
    low = env["low_0_3k"]
    hi = env["hi_8_16k"]
    times = np.arange(len(low)) * FRAME_HOP_S

    # 发声/静音帧：按低频能量分位定阈（相对口径，避免不同臂响度不同）
    low_top = np.percentile(low, 85)
    low_bot = np.percentile(low, 10)
    voiced = low > (low_top - 25.0)
    quiet = low < (low_bot + 6.0)

    # H1 气口残留：静音帧的 8–16k 电平，相对该臂发声帧的高频电平
    hq = float(np.median(hi[quiet])) if quiet.sum() > 20 else float("nan")
    hv = float(np.median(hi[voiced])) if voiced.sum() > 20 else float("nan")

    # H3 包络失锁：发声段 low/hi 包络相关 + 延迟
    lv, hv_ = low[voiced], hi[voiced]
    if lv.size > 100:
        a = smooth(lv - lv.mean(), 5)
        b = smooth(hv_ - hv_.mean(), 5)
        corr = float(np.corrcoef(a, b)[0, 1])
        # 延迟扫描 ±300ms
        best_lag, best_r = 0, -2.0
        for lag in range(-30, 31):
            if lag >= 0:
                r = np.corrcoef(a[lag:], b[: b.size - lag])[0, 1] if lag else corr
            else:
                r = np.corrcoef(a[: a.size + lag], b[-lag:])[0, 1]
            if r > best_r:
                best_r, best_lag = float(r), lag * int(FRAME_HOP_S * 1000)
    else:
        corr, best_lag, best_r = float("nan"), 0, float("nan")

    # H4 折叠分析：把发声段的 hi 包络按 hop 周期折叠 → 看块界凹陷
    ph = np.mod(times, HOP_S) / HOP_S
    nb = 40
    bins = np.minimum((ph * nb).astype(int), nb - 1)
    fold = np.full(nb, np.nan)
    for i in range(nb):
        sel = (bins == i) & voiced
        if sel.sum() >= 5:
            fold[i] = float(np.median(hi[sel]))
    fold_rel = fold - np.nanmedian(fold)

    # H4b 调制谱：hi 包络去趋势后的频谱，看 1/HOP_S 有没有峰
    env_dt = smooth(hi[voiced] - smooth(hi[voiced], 201), 3)
    if env_dt.size > 512:
        win = np.hanning(env_dt.size)
        sp = np.abs(np.fft.rfft(env_dt * win))
        fr = np.fft.rfftfreq(env_dt.size, FRAME_HOP_S)
        m = (fr > 0.02) & (fr < 2.0)
        spm, frm = sp[m], fr[m]
        peak_i = int(np.argmax(spm))
        # 目标频率 1/4.7 = 0.2128Hz 附近的相对能量（±20%）
        tgt = 1.0 / HOP_S
        near = (frm > tgt * 0.8) & (frm < tgt * 1.2)
        mod_peak_hz = float(frm[peak_i])
        mod_rel = float(spm[near].max() / max(np.median(spm), 1e-12)) if near.any() else float("nan")
    else:
        mod_peak_hz, mod_rel = float("nan"), float("nan")

    out = {
        "tag": tag,
        "file": path.name,
        "sr": sr,
        "seconds": round(len(y) / sr, 2),
        "voiced_pct": round(100.0 * voiced.mean(), 1),
        "quiet_pct": round(100.0 * quiet.mean(), 1),
        # 频带（全体中位）
        **{f"band_{k}": round(float(np.median(v)), 2) for k, v in env.items()},
        # H1
        "H1_hi_voiced_db": round(hv, 2),
        "H1_hi_quiet_db": round(hq, 2),
        "H1_gap_db": round(hv - hq, 2),
        # H3
        "H3_env_corr": round(corr, 3),
        "H3_best_lag_ms": best_lag,
        "H3_best_corr": round(best_r, 3),
        # H4
        "H4_fold_depth_db": round(float(-(np.nanmin(fold_rel) - np.nanmedian(fold_rel))), 2),
        "H4_fold_range_db": round(float(np.nanmax(fold_rel) - np.nanmin(fold_rel)), 2),
        "H4_mod_peak_hz": round(mod_peak_hz, 3),
        "H4_mod_rel": round(mod_rel, 2),
        # H5/H6
        "H5_sfm_8_16k": round(sfm(p, f, 8000, 16000), 4),
        "H5_crest_8_16k_db": round(crest_hi(p, f), 2),
        "H6_tilt_11v8_db": round(
            float(np.median(env["air_11_16k"]) - np.median(env["hi_8_12k"])), 2),
    }
    return out, fold_rel, np.array([i / nb * HOP_S for i in range(nb)])


def main() -> int:
    jobs = [
        ("A 现役链(无BWE)", FULL / "A_full_post.wav"),
        ("D1 BWE", FULL / "D1_bwe_post.wav"),
        ("D3 BWE+滚降", FULL / "D3_bwe_tilted_post.wav"),
        ("D_BWE 原始48k", FULL / "D_bwe_48k.wav"),
        ("原唱干声(真值)", REF),
        ("干声 RVC", SLICE / "P0_原调_raw.wav"),
    ]
    rows, folds = [], {}
    for tag, path in jobs:
        if not path.exists():
            print(f"[跳过] {tag}: 缺 {path}", file=sys.stderr)
            continue
        r, fold, ph = analyze(tag, path)
        rows.append(r)
        folds[tag] = fold
        print(f"[ok] {tag}")

    print("\n=== 逐臂指标 ===")
    keys = ["H1_gap_db", "H3_env_corr", "H3_best_lag_ms", "H4_fold_depth_db",
            "H4_fold_range_db", "H4_mod_peak_hz", "H4_mod_rel",
            "H5_sfm_8_16k", "H5_crest_8_16k_db", "H6_tilt_11v8_db",
            "band_hi_8_16k", "band_low_0_3k"]
    hdr = f"{'指标':<20}" + "".join(f"{r['tag'][:14]:>16}" for r in rows)
    print(hdr)
    for k in keys:
        line = f"{k:<20}"
        for r in rows:
            v = r.get(k)
            line += f"{v:>16}" if isinstance(v, str) else f"{v:>16.3f}"
        print(line)

    print(f"\n=== H2 块界折叠（周期 {HOP_S}s，相对中位 dB）===")
    print(f"{'相位(s)':<10}" + "".join(f"{r['tag'][:12]:>14}" for r in rows))
    ph = np.array([i / 40 * HOP_S for i in range(40)])
    for i in range(40):
        line = f"{ph[i]:<10.3f}"
        for r in rows:
            f_ = folds.get(r["tag"])
            line += f"{f_[i]:>14.2f}" if f_ is not None else f"{'':>14}"
        print(line)

    outp = FULL / "elec_diagnose.json"
    outp.write_text(json.dumps({"rows": rows, "hop_s": HOP_S,
                                "fold_phase_s": ph.tolist(),
                                "folds": {k: v.tolist() for k, v in folds.items()}},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {outp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
