"""门控验证：把 BWE 的高频按「输入有多少内容」加权，看能否消掉气口的电子嗡声。

归因结论（见 elec_verify.json）
    BWE 与干声的低频互相关 0.99936、lag=0 —— 它**只改 8k 以上**。
    而「低频 <−40dB 的真静音帧」里，D1 的 8–16k 有 −39.0dB、周期性 0.746、
    气声/谐波比 −6.29dB；原唱同档是 −51.4 / 0.065 / +1.78。
    ⇒ 气口被填了一层"纯谐波、无气声、比真人响 12dB"的高频。这就是"开了才有"的那层电。

两个候选修复（都只动 8k 以上，纯后处理、不重跑模型）：
  G  门控：8k+ 乘一个随 0–8k 包络变化的增益（弱段关、强段全开）
  X  混合：弱段用 A 臂的高频（真实但偏弱），强段用 D 臂 —— 两者 lag=0 可直加

两条都输出音频 + 指标表，由耳朵裁决。

用法：
    ./.venv/Scripts/python.exe experiments/cover_elec_gate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.ndimage import uniform_filter1d
from scipy.signal import butter, sosfilt

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "media/covers/midsong_20261018/chengdu_bwe_full"
sys.path.insert(0, str(ROOT / "experiments"))

from cover_elec_verify import est_f0  # noqa: E402

SR_OUT = 40000
HI_LO = 8000.0
ENV_S = 0.050          # 门控包络平滑窗
#: (0–8k 包络 dB, 8k+ 增益 dB)。低于 −38 视为"没有内容可依"，几乎全关。
GATE_POINTS = [(-45.0, -30.0), (-38.0, -24.0), (-30.0, -14.0),
               (-22.0, -6.0), (-16.0, -1.5), (-10.0, 0.0), (10.0, 0.0)]
#: A/D 混合的权重曲线（同一自变量）
MIX_POINTS = [(-45.0, 0.0), (-38.0, 0.02), (-30.0, 0.12),
              (-22.0, 0.45), (-16.0, 0.85), (-10.0, 1.0), (10.0, 1.0)]
#: A 臂高频在弱段的补偿（实测原唱 −51.4 vs A −59.1，差 8dB）
A_BOOST_DB = 8.0


def read(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return y.mean(axis=1).astype(np.float64), int(sr)


def env_0_8k(y: np.ndarray, sr: int) -> np.ndarray:
    ny = sr / 2.0
    sos = butter(4, min(8000.0, ny * 0.98) / ny, btype="low", output="sos")
    lo = sosfilt(sos, y)
    rms = np.sqrt(np.maximum(uniform_filter1d(lo * lo, int(ENV_S * sr)), 1e-24))
    return 20 * np.log10(rms)


def curve(env_db: np.ndarray, pts: list[tuple[float, float]]) -> np.ndarray:
    xs = np.array([p[0] for p in pts], dtype=np.float64)
    ys = np.array([p[1] for p in pts], dtype=np.float64)
    return np.interp(env_db, xs, ys)


def split_hi(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    ny = sr / 2.0
    sos = butter(4, [HI_LO / ny, 0.97], btype="band", output="sos")
    hi = sosfilt(sos, y)
    return hi, y - hi


def metrics(y: np.ndarray, sr: int, tag: str) -> dict:
    """与 cover_elec_verify 同口径：按绝对 0–3k 电平分档，每档 A1/T2/ehi。"""
    n = int(0.064 * sr)
    h = int(0.010 * sr)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    win = np.hanning(n)
    scl = float((win ** 2).sum())
    hi_m = (freqs >= 8000) & (freqs < 16000)
    lo_m = (freqs >= 200) & (freqs < 3000)
    hp = sosfilt(butter(4, [8000 / (sr / 2), 0.97], btype="band", output="sos"), y)
    nf = 1 + (len(y) - n) // h
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
            rec.append((e_lo, e_hi, np.nan, np.nan))
            continue
        fh, ph = freqs[hi_m], pw[hi_m]
        q = fh / f0
        harm = np.abs(q - np.round(q)) < 0.18
        noise = np.abs(q - np.round(q)) > 0.18
        if not harm.any() or not noise.any():
            rec.append((e_lo, e_hi, np.nan, np.nan))
            continue
        a1 = float(10 * np.log10(max(ph[noise].sum(), 1e-20) / max(ph[harm].sum(), 1e-20)))
        lag = int(round(sr / f0))
        z = hp[s:s + n]
        if 2 <= lag < z.size - 2:
            a_, b_ = z[: z.size - lag], z[lag:]
            va, vb = float((a_ * a_).mean()), float((b_ * b_).mean())
            t2 = float((a_ * b_).mean() / np.sqrt(va * vb)) if va > 1e-12 and vb > 1e-12 else np.nan
        else:
            t2 = np.nan
        rec.append((e_lo, e_hi, a1, t2))
    arr = np.array(rec, dtype=np.float64)
    ok = np.isfinite(arr[:, 2])
    out = {"tag": tag, "ok": int(ok.sum())}
    for name, a, b in [("silent", -1e9, -40.0), ("quiet", -40.0, -25.0),
                       ("soft", -25.0, -15.0), ("mid", -15.0, -5.0),
                       ("loud", -5.0, 5.0), ("peak", 5.0, 1e9)]:
        sel = ok & (arr[:, 0] > a) & (arr[:, 0] <= b)
        if sel.sum() < 20:
            continue
        out[name] = {"pct": round(100.0 * sel.sum() / max(out["ok"], 1), 1),
                     "ehi": round(float(np.median(arr[sel, 1])), 1),
                     "a1": round(float(np.nanmedian(arr[sel, 2])), 2),
                     "t2": round(float(np.nanmedian(arr[sel, 3])), 3)}
    return out


def main() -> int:
    a, sa = read(FULL / "A_full_post.wav")
    d, sd = read(FULL / "D1_bwe_post.wav")
    if sa != sd:
        raise SystemExit(f"采样率不一致 {sa} vs {sd}")
    n = min(len(a), len(d))
    a, d = a[:n], d[:n]
    sr = sa

    env = env_0_8k(d, sr)
    g_db = curve(env, GATE_POINTS)
    w = curve(env, MIX_POINTS)

    hi_d, lo_d = split_hi(d, sr)
    hi_a, _ = split_hi(a, sr)
    hi_a = hi_a[: len(hi_d)]

    out_dir = FULL / "gate"
    out_dir.mkdir(parents=True, exist_ok=True)

    gated = lo_d + hi_d * (10.0 ** (g_db / 20.0))
    sf.write(str(out_dir / "G_gated_8k.wav"), gated.astype(np.float32), sr, subtype="PCM_16")

    mixed_hi = hi_a * (10.0 ** (A_BOOST_DB / 20.0)) * (1.0 - w) + hi_d * w
    mixed = lo_d + mixed_hi
    sf.write(str(out_dir / "X_mixed_8k.wav"), mixed.astype(np.float32), sr, subtype="PCM_16")

    # 只调 D 侧、不引 A 的"纯门控+补偿"版本（避免 A 臂在弱段过于闷）
    rows = [
        metrics(d, sr, "D1 BWE 原始"),
        metrics(gated, sr, "G 门控"),
        metrics(mixed, sr, "X 门控+A混合"),
        metrics(a, sr, "A 现役链(参照)"),
    ]
    ref, _ = read(ROOT / "media/covers/midsong_20261018/candidates/成都-赵雷/vocals.wav")
    # 原唱 44.1k → 简单重采样到 40k（只用于分档参考，精度够）
    ref40 = np.interp(np.arange(int(len(ref) * sr / 44100)) * 44100 / sr,
                      np.arange(len(ref)), ref)
    rows.append(metrics(ref40, sr, "原唱真值(参照)"))

    print("\n=== 各档 8–16k 电平 / 气声比 / 周期性 ===")
    for r in rows:
        print(f"\n-- {r['tag']}  (可信帧 {r['ok']})")
        print(f"   {'档':<8}{'占比%':>7}{'ehi_db':>9}{'A1_db':>8}{'T2':>8}")
        for k in ("silent", "quiet", "soft", "mid", "loud", "peak"):
            if k not in r:
                continue
            t = r[k]
            print(f"   {k:<8}{t['pct']:>7.1f}{t['ehi']:>9.1f}{t['a1']:>8.2f}{t['t2']:>8.3f}")

    (out_dir / "gate_metrics.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n音频：{out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
