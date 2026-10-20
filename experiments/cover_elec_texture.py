"""BWE 高频的微观纹理归因：「生成出来的高频」和「真实的高频」差在哪。

第一轮（cover_elec_diagnose.py）排掉了块界凹陷、气口残留、包络失锁、调制谱、
谱平坦度 —— BWE 在高频的**宏观统计**上甚至比原唱更"散"、包络同步性更好。
那"开了就有电音"只能来自**微观结构**：8–16k 到底是不是锁在 f0 的谐波上。

真人唱歌的高频 = f0 的高次谐波（200Hz 的 f0，8–16k 是第 40–80 次谐波），
位置精确等于 k·f0，且与声门脉冲锁相。神经网络若生成的是"统计上像高频"的
东西，谐波位置就会松、或者干脆是一层固定纹理 —— 这就是类电子合成器的听感。

测项（全部逐帧、只在可信发声帧上统计）：
  T1 谐波对比度   8–16k 能量在 k·f0 处 vs 在 (k+½)·f0 处，比值的 dB 中位数。
                  真人应显著为正；纯噪声 ≈ 0；谐波位置偏移会漏到 0 甚至为负。
  T2 高频周期性   8–16k 带通后在 lag=1/f0 的自相关（逐 lag 无偏归一）。
                  真人=谐波结构决定的高周期性。
  T3 帧间谱形一致 限定 f0 窄带内的帧，8–16k 归一化谱形两两相关的均值。
                  高 = 无论唱什么都是同一层纹理（固定"沙沙"层）。
  T4 谐波对齐偏差 逐帧扫 f0 的缩放系数 α∈[0.94,1.06]，看哪个 α 让谐波对比度最高。
                  α≠1 说明高频的谐波间距与低频 f0 不一致（"另一种音高"）。
  T5 高频重心随f0  高频谱心与同帧 f0 的相关。真人正相关，固定纹理不相关。

用法：
    ./.venv/Scripts/python.exe experiments/cover_elec_texture.py
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

F0_WIN_S = 0.064      # f0 估计窗（≈13 个 200Hz 周期）
F0_HOP_S = 0.010
F0_MIN, F0_MAX = 80.0, 520.0
HI_LO, HI_HI = 8000.0, 16000.0
GUARD = 0.18          # 谐波掩码半宽（× f0）


def load(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return y.mean(axis=1), int(sr)


def est_f0(seg: np.ndarray, sr: int) -> tuple[float, float]:
    """逐 lag 无偏归一的自相关基频。返回 (f0, 周期峰)。

    ★ ac/ac[0] 会带 (1−lag/N) 偏置，高音段被系统性低估（m1p 2026-10-19 的教训），
      这里按重叠样本数归一。
    """
    x = np.asarray(seg, dtype=np.float64)
    x = x - x.mean()
    n = x.size
    var = float((x * x).mean())
    if var < 1e-8:
        return 0.0, 0.0
    lo = max(int(sr / F0_MAX), 2)
    hi = min(int(sr / F0_MIN), n // 2)
    if hi <= lo + 1:
        return 0.0, 0.0
    ac = np.correlate(x, x, "full")[n - 1:]        # ac[lag] = Σ x[i]x[i+lag]
    lags = np.arange(ac.size)
    ov = np.maximum(n - lags, 1)
    r = (ac / ov) / var                            # 无偏 + 归一到方差
    r = np.clip(r, -2.0, 2.0)
    seg_r = r[lo:hi]
    if seg_r.size == 0:
        return 0.0, 0.0
    k = int(np.argmax(seg_r)) + lo
    if r[k] <= 0.0:
        return 0.0, float(r[k])
    # 抛物线插值细化
    if lo < k < ac.size - 1:
        a, b, c = r[k - 1], r[k], r[k + 1]
        den = a - 2 * b + c
        delta = 0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0
        delta = float(np.clip(delta, -1.0, 1.0))
    else:
        delta = 0.0
    return float(sr / (k + delta)), float(r[k])


def harm_contrast(freqs: np.ndarray, pw: np.ndarray, f0: float, alpha: float = 1.0) -> float:
    """8–16k 谐波处能量 / 半谐波处能量（dB）。"""
    m = (freqs >= HI_LO) & (freqs < HI_HI)
    if not m.any() or f0 <= 0:
        return float("nan")
    f = freqs[m]
    p = pw[m]
    step = f0 * alpha
    if step < 2 * (f[1] - f[0]) * 2:
        return float("nan")
    # 距最近整数倍 / 半整数倍的相对距离
    q = f / step
    d_int = np.abs(q - np.round(q))
    harm = d_int < GUARD
    anti = np.abs(d_int - 0.5) < GUARD
    if not harm.any() or not anti.any():
        return float("nan")
    return float(10 * np.log10(max(p[harm].sum(), 1e-20) / max(p[anti].sum(), 1e-20)))


def band_period_power(y: np.ndarray, sr: int, f0: float, lo: float, hi: float) -> float:
    """带通后在 lag=1/f0 的自相关（逐 lag 无偏归一）。用 FFT 带通 + 时域自相关。"""
    from scipy.signal import butter, sosfilt

    ny = sr / 2.0
    sos = butter(4, [lo / ny, min(hi, ny * 0.98) / ny], btype="band", output="sos")
    z = sosfilt(sos, y.astype(np.float64))
    lag = int(round(sr / f0)) if f0 > 0 else 0
    n = z.size
    if lag < 2 or lag >= n - 2:
        return float("nan")
    a, b = z[: n - lag], z[lag:]
    va, vb = float((a * a).mean()), float((b * b).mean())
    if va < 1e-12 or vb < 1e-12:
        return float("nan")
    return float((a * b).mean() / np.sqrt(va * vb))


def analyze(tag: str, path: Path) -> dict:
    y, sr = load(path)
    n = int(F0_WIN_S * sr)
    h = int(F0_HOP_S * sr)
    if len(y) < n:
        raise SystemExit(f"{tag}: 太短")
    nf = 1 + (len(y) - n) // h
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    win = np.hanning(n).astype(np.float32)
    scl = float((win ** 2).sum())
    hi_m = (freqs >= HI_LO) & (freqs < HI_HI)

    rows = []
    for i in range(nf):
        s = i * h
        seg = y[s:s + n]
        f0, per = est_f0(seg, sr)
        if f0 <= 0 or per < 0.30:
            continue
        x = (seg - seg.mean()) * win
        spec = np.fft.rfft(x)
        pw = (spec.real ** 2 + spec.imag ** 2) / scl
        if pw[hi_m].sum() <= 1e-18:
            continue
        c1 = harm_contrast(freqs, pw, f0, 1.0)
        if not np.isfinite(c1):
            continue
        # T5 高频谱心
        fh, ph = freqs[hi_m], pw[hi_m]
        cen = float((fh * ph).sum() / ph.sum()) if ph.sum() > 0 else float("nan")
        rows.append({
            "t": s / sr, "f0": f0, "per": per, "c1": c1, "cen": cen,
            "seg": s,
        })

    if len(rows) < 30:
        raise SystemExit(f"{tag}: 可信帧太少 {len(rows)}")

    c1 = np.array([r["c1"] for r in rows])
    f0s = np.array([r["f0"] for r in rows])
    cen = np.array([r["cen"] for r in rows])

    # T3 帧间谱形一致性：f0 落在中位 ±8% 的帧，谱形（8–16k，减去均值、归一化）
    f0_med = float(np.median(f0s))
    shapes = []
    for r in rows:
        if not (abs(r["f0"] / f0_med - 1.0) < 0.08):
            continue
        s = r["seg"]
        seg = y[s:s + n]
        x = (seg - seg.mean()) * win
        spec = np.fft.rfft(x)
        p = (spec.real ** 2 + spec.imag ** 2)
        v = np.log10(np.maximum(p[hi_m], 1e-20))
        v = v - v.mean()
        nv = np.linalg.norm(v)
        if nv > 1e-9:
            shapes.append(v / nv)
    if len(shapes) > 400:
        rng = np.random.default_rng(7)
        pick = rng.choice(len(shapes), 400, replace=False)
        shapes = [shapes[i] for i in pick]
    shapes = np.array(shapes)
    if len(shapes) >= 10:
        gm = shapes.mean(axis=0)
        gm /= max(np.linalg.norm(gm), 1e-9)
        cons = float(np.median(shapes @ gm))
    else:
        cons = float("nan")

    # T4 α 扫描：哪个缩放让谐波对比度最高（只用前 1500 帧，够统计）
    alphas = np.arange(0.94, 1.0601, 0.01)
    best_a, best_v = 1.0, -1e9
    alpha_curve = []
    sub_rows = rows[:1500]
    for a in alphas:
        vals = []
        for r in sub_rows:
            s = r["seg"]
            seg = y[s:s + n]
            x = (seg - seg.mean()) * win
            spec = np.fft.rfft(x)
            p = (spec.real ** 2 + spec.imag ** 2) / scl
            v = harm_contrast(freqs, p, r["f0"], float(a))
            if np.isfinite(v):
                vals.append(v)
        if vals:
            mv = float(np.median(vals))
            alpha_curve.append((round(float(a), 3), round(mv, 3)))
            if mv > best_v:
                best_v, best_a = mv, float(a)

    # T2 高频周期性：抽 200 帧做带通（带通很贵，不必全跑）
    idx = np.linspace(0, len(rows) - 1, min(200, len(rows))).astype(int)
    hp = []
    for i in idx:
        r = rows[i]
        s = r["seg"]
        seg = y[s:s + n]
        v = band_period_power(seg, sr, r["f0"], HI_LO, HI_HI)
        if np.isfinite(v):
            hp.append(v)
    hi_period = float(np.median(hp)) if hp else float("nan")

    # T5 相关
    if len(f0s) > 50:
        corre = float(np.corrcoef(f0s, cen)[0, 1])
    else:
        corre = float("nan")

    return {
        "tag": tag,
        "sr": sr,
        "frames": len(rows),
        "f0_med": round(f0_med, 1),
        "T1_harm_contrast_db": round(float(np.median(c1)), 2),
        "T1_p10": round(float(np.percentile(c1, 10)), 2),
        "T1_p90": round(float(np.percentile(c1, 90)), 2),
        "T2_hi_period": round(hi_period, 3),
        "T3_shape_consistency": round(cons, 3),
        "T3_n": int(len(shapes)),
        "T4_best_alpha": round(best_a, 3),
        "T4_best_contrast_db": round(best_v, 2),
        "T4_at_1.0_db": None,
        "T5_corr_f0_centroid": round(corre, 3),
        "_alpha_curve": alpha_curve,
    }


def main() -> int:
    jobs = [
        ("A 现役链(无BWE)", FULL / "A_full_post.wav"),
        ("D1 BWE", FULL / "D1_bwe_post.wav"),
        ("D3 BWE+滚降", FULL / "D3_bwe_tilted_post.wav"),
        ("原唱干声(真值)", REF),
        ("干声 RVC", SLICE / "P0_原调_raw.wav"),
    ]
    rows = []
    for tag, path in jobs:
        if not path.exists():
            print(f"[跳过] {tag}", file=sys.stderr)
            continue
        r = analyze(tag, path)
        ac = dict(r.pop("_alpha_curve"))
        r["T4_at_1.0_db"] = ac.get(1.0)
        rows.append(r)
        print(f"[ok] {tag}  帧 {r['frames']}  T1 {r['T1_harm_contrast_db']:+.2f}dB  "
              f"T2 {r['T2_hi_period']:.3f}  T3 {r['T3_shape_consistency']:.3f}  "
              f"T4 α* {r['T4_best_alpha']}")

    print("\n=== 微观纹理指标 ===")
    keys = ["frames", "f0_med", "T1_harm_contrast_db", "T1_p10", "T1_p90",
            "T2_hi_period", "T3_shape_consistency", "T4_best_alpha",
            "T4_at_1.0_db", "T5_corr_f0_centroid"]
    print(f"{'指标':<24}" + "".join(f"{r['tag'][:13]:>15}" for r in rows))
    for k in keys:
        line = f"{k:<24}"
        for r in rows:
            v = r.get(k)
            line += f"{v:>15}" if v is None else f"{v:>15.3f}"
        print(line)

    out = FULL / "elec_texture.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
