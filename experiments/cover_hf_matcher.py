"""给 e 臂补高频电平：内容已经在，只差量。

e（你的参照先 BWE 再喂 Seed-VC）拿到了"跟随嗓音"的高频，CAM++ 0.380 说明音色还是你的，
但 8–12k 比源低 15.6dB、6–8k 低 12.9dB。这条要验的就一件事：

**把电平补上去之后，那层"跟随嗓音"的高频会不会退化成噪声底？**

为什么这个问题不能想当然：A 臂（无 BWE，产品链 `treble+6dB@7k`）就是"抬高频电平"的
现成反面教材 —— 它抬的是噪声底，slope 只有 0.11。所以"补电平"本身不解决问题，
**补在有余量的内容上才行**。判据还是 m1v 那三条：slope / 气口掉 / r_env。

做法：按频带差做匹配 EQ（不是拍脑袋给个 shelf 值）。目标取源人声的 5k 以上谱形，
逐带算差，在对数频率上插值成平滑增益曲线，整段零相位 FFT 施加。
上限夹在 +18dB —— 超过这个就不是"补"了，是把底噪搬上来。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import cover_hf_tracking as H  # noqa: E402
import cover_seedvc_probe as P  # noqa: E402

OUT = ROOT / "media" / "covers" / "midsong_20261018" / "seedvc_probe"
#: 参与匹配的频带（5k 以下不动：那是音色本体，改了就不像你了）
BANDS = [(5000, 6000), (6000, 8000), (8000, 12000), (12000, 16000)]
MAX_GAIN = 18.0
#: 增益曲线在 4k 以下必须回落到 0dB，否则会连中频一起抬
F_LOW, F_HIGH = 4000.0, 16000.0


def band_levels(y: np.ndarray, sr: int, bands=BANDS) -> dict[str, float]:
    n, hop = int(0.064 * sr), int(0.010 * sr)
    win = np.hanning(n)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    lo = (freqs >= 200) & (freqs < 3000)
    masks = [(freqs >= a) & (freqs < b) for a, b in bands]
    elo, pows = [], []
    for s in range(0, len(y) - n, hop):
        seg = np.asarray(y[s : s + n], dtype=np.float64)
        p = np.abs(np.fft.rfft((seg - seg.mean()) * win)) ** 2
        elo.append(10 * np.log10(max(float(p[lo].sum()), 1e-20)))
        pows.append(p)
    elo, pows = np.array(elo), np.array(pows)
    sel = elo >= np.percentile(elo, 50)
    # 响度对齐到 200–3k = −20dB，跨文件才可比
    gain = -20.0 - 10 * np.log10(np.median(pows[sel][:, lo].sum(axis=1)))
    return {
        f"{a}-{b}": round(
            float(np.median(10 * np.log10(np.maximum(pows[sel][:, m].sum(axis=1), 1e-20))) + gain),
            1,
        )
        for (a, b), m in zip(bands, masks, strict=True)
    }


def eq_curve(target: dict, current: dict, bands=BANDS) -> tuple[np.ndarray, np.ndarray]:
    """逐带差 → 对数频率上的平滑增益锚点。"""
    fs = [np.sqrt(a * b) for a, b in bands]
    gs = [float(np.clip(target[k] - current[k], 0.0, MAX_GAIN)) for k in target]
    f = np.concatenate([[500.0, F_LOW], fs])
    g = np.concatenate([[0.0, 0.0], gs])
    return f, g


def apply_eq(y: np.ndarray, sr: int, anchors_f: np.ndarray, anchors_g: np.ndarray) -> np.ndarray:
    """整段零相位 EQ。不用 STFT 逐帧：分帧会给每个 bin 单独相位，等于给高频加噪。"""
    x = np.asarray(y, dtype=np.float64)
    n = x.size
    f = np.fft.rfftfreq(n, 1.0 / sr)
    g = np.interp(
        np.log2(np.maximum(f, 1.0)),
        np.log2(np.maximum(anchors_f, 1.0)),
        anchors_g,
        left=0.0,
        right=float(anchors_g[-1]),
    )
    out = np.fft.irfft(np.fft.rfft(x) * (10.0 ** (g / 20.0)), n)
    peak = np.abs(out).max()
    return (out * min(1.0, 0.99 / peak) if peak > 0.99 else out).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="e_你的参照BWE")
    ap.add_argument("--out", default="g_e_补高频")
    args = ap.parse_args()

    src = OUT / "c_tail_source.wav"
    arm = OUT / f"{args.arm}.wav"
    if not src.exists() or not arm.exists():
        raise SystemExit("缺产物：先跑 cover_seedvc_refbwe.py")

    ys, srs = P.M.read(src)
    ya, sra = P.M.read(arm)
    target = band_levels(ys, srs)
    cur = band_levels(ya, sra)
    af, ag = eq_curve(target, cur)
    boosted = apply_eq(ya, sra, af, ag)

    dst = OUT / f"{args.out}.wav"
    sf.write(str(dst), boosted, sra, subtype="FLOAT")

    print(f"\n=== 匹配 EQ：{args.arm} → {args.out} ===")
    print(f"{'频带':<12}{'目标(源)':>10}{'补前':>9}{'差':>7}{'补后':>9}")
    after = band_levels(boosted, sra)
    for k in target:
        print(f"{k:<12}{target[k]:>10.1f}{cur[k]:>9.1f}{target[k] - cur[k]:>+7.1f}{after[k]:>9.1f}")
    print("增益曲线：" + "  ".join(f"{f:.0f}Hz:{g:+.1f}dB" for f, g in zip(af[2:], ag[2:], strict=True)))

    rows = []
    for name, p in (("补前", arm), ("补后", dst), ("源", src)):
        y, sr = P.M.read(p)
        t = H.tracking(y, sr)
        t["name"] = name
        t["bands"] = band_levels(y, sr)
        rows.append(t)
        print(
            f"{name:<6}{t['hf_active_db']:>8.1f}{t['r_env']:>8.2f}{t['slope']:>7.2f}"
            f"{t['gap_drop_db']:>8.1f}{t['hf_spread_db']:>8.1f}  {H.verdict(t, -52.5)}"
        )

    with open(OUT / "matcher.json", "w", encoding="utf-8") as f:
        json.dump(
            {"curve": {"f": af.tolist(), "g": np.round(ag, 2).tolist()}},
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(
        "\n判据：补后 slope 必须明显高于 A 臂的 0.11，气口掉必须比 −8 更负。"
        "两条都过，说明抬起来的是内容不是底噪。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
