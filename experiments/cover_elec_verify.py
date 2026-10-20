"""验证「BWE 在弱发音段生成纯谐波」——把上一步的采样偏差修掉并分档。

上一步（cover_elec_texture.py）的三个信号：
  T2 高频周期性  D1 0.142 vs 原唱 0.022（6.5×）
  T5 谱心~f0 相关 D1 −0.558 vs 原唱 +0.048
  T4 α=1 谐波对比度 D1 前 1500 帧 14.8dB vs 全曲 1.19dB  ← 疑似采样偏差
      （前 1500 帧 ≈ 前 15s，而《成都》前 10s 是 −81dB 静音）

本脚本做两件更正 + 一件直接测量：
  ① T4 改**随机抽样**，不再取时间序列前段
  ② 按帧的中低频能量分 5 档（p0–20 / 20–40 / ... / 80–100），逐档算指标
  ③ A1 气声/谐波比：8–16k 里 谐波掩码外 / 掩码内 的能量比（dB）
     这是"缺非周期成分"最直接的度量 —— 真人高频是谐波+气声，纯生成是纯谐波
  ④ A2 高频能量随发声强度的斜率（dB/dB）：BWE 是否把高频动态范围压扁了

用法：
    ./.venv/Scripts/python.exe experiments/cover_elec_verify.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "media/covers/midsong_20261018/chengdu_bwe_full"
SLICE = ROOT / "media/covers/midsong_20261018/chengdu"
REF = ROOT / "media/covers/midsong_20261018/candidates/成都-赵雷/vocals.wav"

WIN_S = 0.064
HOP_S = 0.010
F0_MIN, F0_MAX = 80.0, 520.0
HI_LO, HI_HI = 8000.0, 16000.0
LO_LO, LO_HI = 200.0, 3000.0
GUARD = 0.18
TIERS = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100)]
#: 绝对能量档（0–3k 的帧电平，dB）。跨文件必须用绝对值才可比 ——
#: 按各文件自己的分位切，D1 的 p0–20 是 −47.8dB（真静音），原唱同档是 −15.8dB
#: （弱发音），拿它们对比等于拿气口比弱唱。
ABS_TIERS = [
    ("silent  <-40", -1e9, -40.0),
    ("quiet   -40~-25", -40.0, -25.0),
    ("soft    -25~-15", -25.0, -15.0),
    ("mid     -15~-5", -15.0, -5.0),
    ("loud    -5~+5", -5.0, 5.0),
    ("peak    >+5", 5.0, 1e9),
]


def load(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return y.mean(axis=1), int(sr)


def est_f0(x: np.ndarray, sr: int) -> tuple[float, float]:
    x = np.asarray(x, dtype=np.float64)
    x = x - x.mean()
    n = x.size
    var = float((x * x).mean())
    if var < 1e-7:
        return 0.0, 0.0
    lo = max(int(sr / F0_MAX), 2)
    hi = min(int(sr / F0_MIN), n // 2)
    if hi <= lo + 1:
        return 0.0, 0.0
    ac = np.correlate(x, x, "full")[n - 1:]
    lags = np.arange(ac.size)
    r = (ac / np.maximum(n - lags, 1)) / var
    r = np.clip(r, -2.0, 2.0)
    k = int(np.argmax(r[lo:hi])) + lo
    if r[k] <= 0:
        return 0.0, float(r[k])
    a, b, c = r[k - 1], r[k], r[k + 1]
    den = a - 2 * b + c
    d = 0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0
    return float(sr / (k + float(np.clip(d, -1, 1)))), float(r[k])


def main() -> int:
    jobs = [
        ("A 现役链(无BWE)", FULL / "A_full_post.wav"),
        ("D1 BWE", FULL / "D1_bwe_post.wav"),
        ("D3 BWE+滚降", FULL / "D3_bwe_tilted_post.wav"),
        ("D_BWE原始48k", FULL / "D_bwe_48k.wav"),
        ("原唱干声(真值)", REF),
        ("干声 RVC", SLICE / "P0_原调_raw.wav"),
    ]
    rng = np.random.default_rng(11)
    allrows = []

    for tag, path in jobs:
        if not path.exists():
            continue
        y, sr = load(path)
        ny = sr / 2.0
        # 一次性带通，后面逐帧切片（比每帧设计滤波器快十倍）
        hp = sosfilt(butter(4, [HI_LO / ny, min(HI_HI, ny * 0.98) / ny],
                            btype="band", output="sos"), y.astype(np.float64)).astype(np.float32)

        n = int(WIN_S * sr)
        h = int(HOP_S * sr)
        nf = 1 + (len(y) - n) // h
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        win = np.hanning(n).astype(np.float32)
        scl = float((win ** 2).sum())
        hi_m = (freqs >= HI_LO) & (freqs < HI_HI)
        lo_m = (freqs >= LO_LO) & (freqs < LO_HI)
        if not hi_m.any() or not lo_m.any():
            print(f"[跳过] {tag}: 频带越界 sr={sr}")
            continue

        rec = []
        for i in range(nf):
            s = i * h
            seg = y[s:s + n]
            f0, per = est_f0(seg, sr)
            x = (seg - seg.mean()) * win
            spec = np.fft.rfft(x)
            pw = (spec.real ** 2 + spec.imag ** 2) / scl
            e_lo = float(10 * np.log10(max(pw[lo_m].sum(), 1e-20)))
            e_hi = float(10 * np.log10(max(pw[hi_m].sum(), 1e-20)))
            if f0 <= 0 or per < 0.30 or e_hi < e_lo - 70:
                rec.append((e_lo, e_hi, np.nan, np.nan, np.nan))
                continue
            fh = freqs[hi_m]
            ph = pw[hi_m]
            q = fh / f0
            d_int = np.abs(q - np.round(q))
            harm = d_int < GUARD
            noise = q - np.round(q)
            noise = np.abs(noise) > GUARD
            if not harm.any() or not noise.any():
                rec.append((e_lo, e_hi, np.nan, np.nan, np.nan))
                continue
            eh = max(ph[harm].sum(), 1e-20)
            en = max(ph[noise].sum(), 1e-20)
            a1 = float(10 * np.log10(en / eh))            # 气声/谐波（dB，越大越"气"）
            # 谐波对比度：谐波 vs 半谐波（与上一步同口径，用于核对）
            anti = np.abs(np.abs(q - np.floor(q)) - 0.5) < GUARD
            c1 = float(10 * np.log10(eh / max(ph[anti].sum(), 1e-20))) if anti.any() else np.nan
            # 高频周期性（带通信号在 lag=1/f0 的自相关）
            lag = int(round(sr / f0))
            z = hp[s:s + n]
            if 2 <= lag < z.size - 2:
                a_, b_ = z[: z.size - lag], z[lag:]
                va, vb = float((a_ * a_).mean()), float((b_ * b_).mean())
                t2 = float((a_ * b_).mean() / np.sqrt(va * vb)) if va > 1e-12 and vb > 1e-12 else np.nan
            else:
                t2 = np.nan
            rec.append((e_lo, e_hi, a1, c1, t2))

        arr = np.array(rec, dtype=np.float64)
        ok = np.isfinite(arr[:, 2])
        elo, ehi = arr[:, 0], arr[:, 1]
        row = {"tag": tag, "sr": sr, "frames": int(len(arr)), "ok": int(ok.sum())}

        # ③ 全曲 A1 / T2（随机抽样核对上一步）
        idx = np.where(ok)[0]
        pick = rng.choice(idx, min(3000, idx.size), replace=False) if idx.size else idx
        row["A1_gas_harm_db_all"] = round(float(np.nanmedian(arr[pick, 2])), 2) if idx.size else None
        row["T2_hi_period_all"] = round(float(np.nanmedian(arr[pick, 4])), 3) if idx.size else None
        row["C1_contrast_db_all"] = round(float(np.nanmedian(arr[pick, 3])), 2) if idx.size else None

        # ② 按**绝对**中低频电平分档（跨文件可比）
        tiers = {}
        for name, lo_e, hi_e in ABS_TIERS:
            sel = ok & (elo > lo_e) & (elo <= hi_e)
            if sel.sum() < 20:
                continue
            tiers[name] = {
                "n": int(sel.sum()),
                "pct": round(100.0 * float(sel.sum()) / max(int(ok.sum()), 1), 1),
                "elo_db": round(float(np.median(elo[sel])), 1),
                "ehi_db": round(float(np.median(ehi[sel])), 1),
                "A1_db": round(float(np.nanmedian(arr[sel, 2])), 2),
                "C1_db": round(float(np.nanmedian(arr[sel, 3])), 2),
                "T2": round(float(np.nanmedian(arr[sel, 4])), 3),
            }
        row["tiers"] = tiers

        # ④ 高频能量随发声强度的斜率
        if ok.sum() > 200:
            x = elo[ok]
            yy = ehi[ok]
            if x.std() > 0.1:
                slope = float(np.polyfit(x, yy, 1)[0])
            else:
                slope = float("nan")
            row["A2_slope_db_per_db"] = round(slope, 3)
        else:
            row["A2_slope_db_per_db"] = None

        allrows.append(row)
        print(f"[ok] {tag}")

    print("\n=== 全曲（随机 3000 帧）===")
    keys = ["A1_gas_harm_db_all", "C1_contrast_db_all", "T2_hi_period_all", "A2_slope_db_per_db"]
    print(f"{'指标':<24}" + "".join(f"{r['tag'][:13]:>15}" for r in allrows))
    for k in keys:
        line = f"{k:<24}"
        for r in allrows:
            v = r.get(k)
            line += f"{v:>15}" if v is None else f"{v:>15.3f}"
        print(line)

    print("\n=== 按**绝对**发声电平分档：A1 气声/谐波（dB，越大越有气声，真人≈2.3）===")
    names = [n for n, _, _ in ABS_TIERS]
    print(f"{'档':<18}" + "".join(f"{r['tag'][:13]:>15}" for r in allrows))
    for key in names:
        line = f"{key:<18}"
        for r in allrows:
            t = r["tiers"].get(key)
            line += f"{t['A1_db']:>15.2f}" if t else f"{'-':>15}"
        print(line)

    print("\n=== 同档：T2 高频周期性（0.71=纯正弦级，真人≈0.02–0.05）===")
    print(f"{'档':<18}" + "".join(f"{r['tag'][:13]:>15}" for r in allrows))
    for key in names:
        line = f"{key:<18}"
        for r in allrows:
            t = r["tiers"].get(key)
            line += f"{t['T2']:>15.3f}" if t else f"{'-':>15}"
        print(line)

    print("\n=== 同档：8–16k 绝对电平（dB）===")
    print(f"{'档':<18}" + "".join(f"{r['tag'][:13]:>15}" for r in allrows))
    for key in names:
        line = f"{key:<18}"
        for r in allrows:
            t = r["tiers"].get(key)
            line += f"{t['ehi_db']:>15.1f}" if t else f"{'-':>15}"
        print(line)

    print("\n=== 同档：占可信帧的比例（%）===")
    print(f"{'档':<18}" + "".join(f"{r['tag'][:13]:>15}" for r in allrows))
    for key in names:
        line = f"{key:<18}"
        for r in allrows:
            t = r["tiers"].get(key)
            line += f"{t['pct']:>15.1f}" if t else f"{'-':>15}"
        print(line)

    out = FULL / "elec_verify.json"
    out.write_text(json.dumps(allrows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
