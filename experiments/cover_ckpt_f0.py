# -*- coding: utf-8 -*-
"""量 e30 / e40 产物的**中位基频**与**绝对频段能量**。

为什么要单独量一次（2026-10-14 自查）：
    `cover_metrics.py` 的 `centroid_hz` 是**谱质心**（能量的频率重心），
    不是基频 —— 它受谐波与共振峰形状影响，不能当"音域/音高"读。
    我在写结论时差点把它写成"中位基频"，所以这里用 `_median_f0`（产品同款实现）
    把真正的基频量出来；顺带把绝对频段能量也量出来，因为
    `band_db` 是**相对 0-4k 的**比值（只看"倾斜"），不能说明整体亮暗。

★ 顺带校准"亮/暗"的方向（谱质心的定义）：
    质心 = Σ f·P(f) / Σ P(f)。**越高 = 能量重心越靠高频 = 越亮**；
    越低 = 能量越挤在低频 = **越闷**。所以读这张表时"质心低"是坏消息
    （与"闷"的抱怨同向），别把它当成"音域更贴"的好消息。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

import cover_api  # noqa: E402

TARGETS = [
    ("media/covers/ckpt/epochs_12min__myvoice_ppt12_full_e10_s990.wav", "e10"),
    ("media/covers/ckpt/epochs_12min__myvoice_ppt12_full_e20_s1980.wav", "e20"),
    ("media/covers/ckpt/epochs_12min__myvoice_ppt12_full_e30_s2970.wav", "e30"),
    ("media/covers/ckpt/epochs_12min__myvoice_ppt12_full_e40_s3960.wav", "e40"),
    ("media/covers/ckpt/00_source_40k.wav", "原唱这一段"),
]


def abs_band_db(path: pathlib.Path) -> dict:
    """绝对频段能量（dBFS，未做任何归一化）—— 回答"整体是不是更亮"。"""
    d, sr = sf.read(str(path), dtype="float64", always_2d=True)
    mono = d.mean(1)
    n, hop = 1024, 512
    win = np.hanning(n)
    frames = [mono[i:i + n] for i in range(0, len(mono) - n, hop)]
    X = np.stack([np.abs(np.fft.rfft(f * win)) ** 2 for f in frames])
    rms = np.sqrt(X.sum(1) / n)
    keep = rms >= rms.max() * (10 ** (-35.0 / 20))
    P = X[keep].mean(0)
    fr = np.fft.rfftfreq(n, 1 / sr)
    # 绝对 dB（相对整段总能量）：不做 0-4k 归一化，这样"整体更亮/更闷"才看得见
    total = float(P.sum()) + 1e-15
    out = {}
    for lo, hi, name in ((0, 4000, "0-4k"), (4000, 8000, "4-8k"),
                         (8000, 11000, "8-11k"), (11000, 16000, "11-16k")):
        frac = float(P[(fr >= lo) & (fr < hi)].sum() / total)
        out[name] = round(10 * float(np.log10(frac + 1e-15)), 1)
    out["centroid_hz"] = int(round(float((fr * P).sum() / total)))
    out["_rms_dbfs"] = round(20 * float(np.log10(float(np.sqrt(np.mean(mono ** 2))) + 1e-15)), 2)
    return out


def main() -> int:
    import librosa

    rows = []
    for rel, tag in TARGETS:
        p = ROOT / rel
        if not p.exists():
            print(f"[!] 缺 {rel}")
            continue
        f0 = cover_api._median_f0(p, np, librosa)
        bands = abs_band_db(p)
        rows.append({"tag": tag, "median_f0_hz": round(float(f0), 1) if f0 else None, **bands})
        print(f"{tag:12s} 中位基频 {rows[-1]['median_f0_hz']:6.1f}Hz · "
              f"谱质心 {bands['centroid_hz']:4d}Hz · "
              f"4-8k {bands['4-8k']:+5.1f}dB · 8-11k {bands['8-11k']:+5.1f}dB · "
              f"RMS {bands['_rms_dbfs']}dBFS")

    out = ROOT / "media/covers/ckpt/f0_bands.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out}")
    print("★ 读法：谱质心**越高越亮**、越低越闷；中位基频若四档相同 = 换权重不改音高。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
