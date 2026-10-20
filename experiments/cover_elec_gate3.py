"""最终修复方案：弱段用干声的高频（真实），强段用 BWE 的高频（明亮）。

为什么不是纯门控
    G2 把气口压了 14.8dB（−47.9→−62.7，原唱 −68.2），电平对了，但谱平坦度
    仍只有 0.087（原唱 0.520）—— 压完剩的还是那根窄带纯音，只是更轻。
    **增益改不了结构。**

而 A 臂（现役链，无 BWE）在气口：
    SFM 0.449、8–16k −60.9dB，原唱 0.520 / −64.6dB —— 结构几乎一模一样。
    它唯一的问题是**强段太弱**（−51 vs 原唱 −16.7，差 34dB），所以整体闷。

⇒ 两者互补：A 在弱段是对的（真实但弱），D 在强段是对的（明亮但对气口乱编）。
   按发声强度在 8k 以上做幅度交叉，两边都能取到对的那一半。
   A/D 的低频逐样本一致（互相关 0.99936、lag=0），相位不会打架；混合用 D 的相位。

用法：
    ./.venv/Scripts/python.exe experiments/cover_elec_gate3.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "media/covers/midsong_20261018/chengdu_bwe_full"
REF = ROOT / "media/covers/midsong_20261018/candidates/成都-赵雷/vocals.wav"
SR = 40000
WIN, HOP = 2048, 512
CUT = 8000.0
#: (0–8k 帧能量 dB, 用 D 的权重)。左端=气口全用 A，右端=强段全用 D。
#: ★ 气口必须**严格 0**：D 在气口是纯音（crest 24.7dB），哪怕只留 7.5% 的能量，
#:   峰值也由它主导 —— 实测 w=0.075 时 crest 仍是 23.9（全 A 是 12.9）。
MIX_POINTS = [(-60.0, 0.0), (-33.0, 0.0), (-28.0, 0.15),
              (-22.0, 0.60), (-16.0, 1.0), (20.0, 1.0)]


def read(p: Path) -> np.ndarray:
    y, _ = sf.read(str(p), dtype="float32", always_2d=True)
    return y.mean(axis=1).astype(np.float64)


def frame_lo_db(y: np.ndarray) -> np.ndarray:
    win = np.hanning(WIN)
    scl = float((win ** 2).sum())
    freqs = np.fft.rfftfreq(WIN, 1.0 / SR)
    lo_m = freqs < CUT
    nf = 1 + (len(y) - WIN) // HOP
    idx = np.arange(WIN)[None, :] + HOP * np.arange(nf)[:, None]
    seg = y[idx]
    seg = (seg - seg.mean(axis=1, keepdims=True)) * win
    p = np.abs(np.fft.rfft(seg, axis=1)) ** 2 / scl
    return 10 * np.log10(np.maximum(p[:, lo_m].sum(axis=1), 1e-20))


def main() -> int:
    a = read(FULL / "A_full_post.wav")
    d = read(FULL / "D1_bwe_post.wav")
    n = min(len(a), len(d))
    a, d = a[:n], d[:n]

    env = frame_lo_db(d)
    # ★ 不能在 dB 域做滑动平均：静音帧夹在字缝里，前后强音会把 dB 均值拉高
    #   10dB 以上，w 永远到不了 0（实测用 5 帧均值时气口 crest 仍是 23.9）。
    #   STFT 本身 4 倍重叠（hop 512 / win 2048）已经能平滑帧间跳变。
    xs = np.array([p[0] for p in MIX_POINTS])
    ys = np.array([p[1] for p in MIX_POINTS])
    w = np.interp(env, xs, ys)

    # ★ 不做任何频域切分。STFT 里把 f≥8k 的 bin 置零再重建，等价于 sinc 核，
    #   旁瓣会在 8–16k 留下残留；而 D 的低频比 A 的高频强 ~20dB，这点残留足以
    #   盖掉 A 的谱结构（实测频域拼接后 crest 恒为 23.9，A 是 12.9，等于没换）。
    #   时域加权在 w=0 / w=1 两端严格等于源信号，代价只是过渡区高频略带相消。
    t_f = np.arange(len(w)) * HOP + WIN / 2.0
    w_s = np.interp(np.arange(n), t_f, w)
    k = max(int(0.020 * SR), 1)
    w_s = np.convolve(w_s, np.ones(k) / k, mode="same")
    mixed = a * (1.0 - w_s) + d * w_s

    out_dir = FULL / "gate"
    out_dir.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_dir / "X2_hi_cross.wav"), mixed.astype(np.float32), SR, subtype="PCM_16")

    # ---- 核对 ----
    win = np.hanning(WIN)
    freqs = np.fft.rfftfreq(WIN, 1.0 / SR)
    nf = 1 + (n - WIN) // HOP
    hi_m2 = (freqs >= 8000) & (freqs < 16000)
    idx = np.arange(WIN)[None, :] + HOP * np.arange(nf)[:, None]

    def stats(sig: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        seg = (sig[idx] - sig[idx].mean(axis=1, keepdims=True)) * win
        p = np.abs(np.fft.rfft(seg, axis=1)) ** 2
        scl = float((win ** 2).sum())
        ehi = 10 * np.log10(np.maximum(p[:, hi_m2].sum(axis=1) / scl, 1e-20))
        sub = np.maximum(p[:, hi_m2], 1e-24)
        sfm = np.exp(np.mean(np.log(sub), axis=1)) / np.maximum(np.mean(sub, axis=1), 1e-24)
        crest = 10 * np.log10(np.max(sub, axis=1) / np.maximum(np.median(sub, axis=1), 1e-24))
        return ehi, sfm, crest

    rows = []
    for tag, sig in [("D1 BWE", d), ("A 现役链", a), ("X2 高频交叉", mixed)]:
        ehi, sfm, crest = stats(sig)
        r = {"tag": tag, "tiers": {}}
        for name, lo_e, hi_e in [("silent", -1e9, -45.0), ("quiet", -45.0, -35.0),
                                 ("soft", -35.0, -25.0), ("mid", -25.0, -15.0),
                                 ("loud", -15.0, -5.0), ("peak", -5.0, 1e9)]:
            sel = (env > lo_e) & (env <= hi_e)
            sel = sel[: min(len(sel), len(ehi))]
            if sel.sum() < 30:
                continue
            r["tiers"][name] = {
                "pct": round(100.0 * sel.mean(), 1),
                "ehi": round(float(np.median(ehi[sel])), 1),
                "sfm": round(float(np.median(sfm[sel])), 4),
                "crest": round(float(np.median(crest[sel])), 1),
            }
        rows.append(r)

    # 原唱单独（同帧集，重采样对齐）
    from scipy.signal import resample_poly
    refy, rsr = sf.read(str(REF), dtype="float32", always_2d=True)
    refy = refy.mean(axis=1).astype(np.float64)
    if rsr != SR:
        refy = resample_poly(refy, SR, rsr)
    refy = np.pad(refy, (0, max(0, n - len(refy))))[:n]
    ehi, sfm, crest = stats(refy)
    r = {"tag": "原唱真值", "tiers": {}}
    for name, lo_e, hi_e in [("silent", -1e9, -45.0), ("quiet", -45.0, -35.0),
                             ("soft", -35.0, -25.0), ("mid", -25.0, -15.0),
                             ("loud", -15.0, -5.0), ("peak", -5.0, 1e9)]:
        sel = (env > lo_e) & (env <= hi_e)
        sel = sel[: min(len(sel), len(ehi))]
        if sel.sum() < 30:
            continue
        r["tiers"][name] = {
            "pct": round(100.0 * sel.mean(), 1),
            "ehi": round(float(np.median(ehi[sel])), 1),
            "sfm": round(float(np.median(sfm[sel])), 4),
            "crest": round(float(np.median(crest[sel])), 1),
        }
    rows.append(r)

    for label, key, fmt in [("8–16k 电平 dB", "ehi", "{:>9.1f}"),
                            ("谱平坦度（0=纯音, 0.5=呼吸噪声）", "sfm", "{:>9.4f}"),
                            ("谱峰锐度 dB", "crest", "{:>9.1f}")]:
        print(f"\n=== {label} ===")
        print(f"{'档':<10}" + "".join(f"{r['tag'][:13]:>15}" for r in rows))
        for nm in ("silent", "quiet", "soft", "mid", "loud", "peak"):
            line = f"{nm:<10}"
            for r in rows:
                t = r["tiers"].get(nm)
                line += fmt.format(t[key]).rjust(15) if t else f"{'-':>15}"
            print(line)

    (out_dir / "gate3_metrics.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n音频 {out_dir / 'X2_hi_cross.wav'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
