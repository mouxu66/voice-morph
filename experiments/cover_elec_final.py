"""最终归因：同一帧集、不筛 f0，看 BWE 在气口到底生成了什么。

两个必须先纠正的口径错误（本脚本存在的理由）
  ① 上一版按各文件自己的 `ok`（要求 est_f0 成功）分档 → A 臂的真气口因为
     "估不出 f0"被整批剔掉，silent 只剩 0.5%；D 臂却因高频有纯谐波能估出 f0，
     silent 占 16.5%。两边根本不是同一批帧。
     实测 A/D 的 0–3k 逐帧电平：相关 0.9623、差值中位 0.0dB、silent 100% 重合
     → **低频完全没被改**，真实静音帧占全曲 24.7%。
  ② A 臂在气口估不出 f0、D 臂估得出 —— 这不是 bug，是**现象本身**：
     BWE 让气口有了音高。

所以本脚本不筛 f0，只用"0–3k 帧电平"这一与 BWE 无关的客观量分档，
对全部帧统计 8–16k 的电平 / 谱平坦度 / 谱峰锐度 / 峰值频率。

用法：
    ./.venv/Scripts/python.exe experiments/cover_elec_final.py
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

WIN_S = 0.064
HOP_S = 0.010
LO_M = (200.0, 3000.0)
HI_M = (8000.0, 16000.0)
TIERS = [("silent", -1e9, -40.0), ("quiet", -40.0, -30.0), ("soft", -30.0, -20.0),
         ("mid", -20.0, -10.0), ("loud", -10.0, 0.0), ("peak", 0.0, 1e9)]


def read(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return y.mean(axis=1).astype(np.float64), int(sr)


def analyze(path: Path, tag: str, tier_ref: np.ndarray | None = None) -> dict:
    y, sr = read(path)
    n = int(WIN_S * sr)
    h = int(HOP_S * sr)
    nf = 1 + (len(y) - n) // h
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    win = np.hanning(n)
    scl = float((win ** 2).sum())
    lo_m = (freqs >= LO_M[0]) & (freqs < LO_M[1])
    hi_m = (freqs >= HI_M[0]) & (freqs < HI_M[1])
    fh = freqs[hi_m]

    elo = np.empty(nf)
    ehi = np.empty(nf)
    sfm = np.empty(nf)
    crest = np.empty(nf)
    peak_hz = np.empty(nf)
    peak_share = np.empty(nf)   # 最强 5 个 bin 占该带能量的比（纯音会接近 1）

    for i in range(nf):
        s = i * h
        seg = y[s:s + n]
        p = np.abs(np.fft.rfft((seg - seg.mean()) * win)) ** 2 / scl
        elo[i] = 10 * np.log10(max(p[lo_m].sum(), 1e-20))
        ph = p[hi_m]
        tot = max(ph.sum(), 1e-20)
        ehi[i] = 10 * np.log10(tot)
        sub = np.maximum(ph, 1e-24)
        sfm[i] = float(np.exp(np.mean(np.log(sub))) / np.mean(sub))
        crest[i] = float(10 * np.log10(max(ph.max(), 1e-20) / max(np.median(sub), 1e-20)))
        k = int(np.argmax(ph))
        peak_hz[i] = fh[k] if tot > 1e-18 else np.nan
        top = np.sort(ph)[-5:]
        peak_share[i] = float(top.sum() / tot)

    # 分档基准：优先用外部传入的参照（A 臂），保证跨版本同一批帧
    if tier_ref is not None:
        m = min(len(tier_ref), nf)
        base = tier_ref[:m]
    else:
        base = elo
        m = nf

    tiers = {}
    for name, a, b in TIERS:
        sel = (base > a) & (base <= b)
        if sel.sum() < 30:
            continue
        idx = np.where(sel)[0]
        idx = idx[idx < nf]
        t = {
            "n": int(idx.size),
            "pct": round(100.0 * idx.size / m, 1),
            "elo": round(float(np.median(elo[idx])), 1),
            "ehi": round(float(np.median(ehi[idx])), 1),
            "sfm": round(float(np.median(sfm[idx])), 4),
            "crest": round(float(np.median(crest[idx])), 1),
            "peak_share": round(float(np.median(peak_share[idx])), 3),
            "peak_hz": round(float(np.nanmedian(peak_hz[idx])) if np.isfinite(peak_hz[idx]).any() else float("nan"), 0),
        }
        tiers[name] = t
    return {"tag": tag, "sr": sr, "frames": nf, "tiers": tiers, "_elo": elo}


def main() -> int:
    a_path = FULL / "A_full_post.wav"
    ref_elo = analyze(a_path, "A", None)["_elo"]

    jobs = [
        ("A 现役链(无BWE)", a_path),
        ("D1 BWE", FULL / "D1_bwe_post.wav"),
        ("D3 BWE+滚降", FULL / "D3_bwe_tilted_post.wav"),
        ("G 门控", FULL / "gate/G_gated_8k.wav"),
        ("X 门控+A", FULL / "gate/X_mixed_8k.wav"),
        ("原唱真值", REF),
        ("RVC 干声", SLICE / "P0_原调_raw.wav"),
    ]
    rows = []
    for tag, p in jobs:
        if not p.exists():
            print(f"[跳过] {tag}")
            continue
        r = analyze(p, tag, ref_elo if tag != "原唱真值" and tag != "RVC 干声" else None)
        rows.append(r)
        print(f"[ok] {tag}")

    names = [n for n, _, _ in TIERS]
    for label, key, fmt in [
        ("8–16k 电平 dB（该档中位）", "ehi", "{:>9.1f}"),
        ("8–16k 谱平坦度（纯音→0，白噪→1）", "sfm", "{:>9.4f}"),
        ("8–16k 谱峰锐度 dB（峰/带内中位）", "crest", "{:>9.1f}"),
        ("最强 5 bin 占比（纯音→1）", "peak_share", "{:>9.3f}"),
        ("峰值频率 Hz", "peak_hz", "{:>9.0f}"),
        ("占比 %", "pct", "{:>9.1f}"),
    ]:
        print(f"\n=== {label} ===")
        print(f"{'档':<10}" + "".join(f"{r['tag'][:13]:>15}" for r in rows))
        for nm in names:
            line = f"{nm:<10}"
            for r in rows:
                t = r["tiers"].get(nm)
                line += (fmt.format(t[key]).rjust(15) if t else f"{'-':>15}")
            print(line)

    # 气口的时间分布（按 A 的 silent 帧，10s 一桶）
    print("\n=== 气口（0–3k < −40dB）的时间分布，10s 桶内占比 % ===")
    n_bucket = int(10.0 / HOP_S)
    sil = (ref_elo <= -40.0).astype(float)
    nb = len(sil) // n_bucket
    if nb > 0:
        frac = sil[: nb * n_bucket].reshape(nb, n_bucket).mean(axis=1) * 100
        for i in range(0, nb, 1):
            bar = "#" * int(frac[i] / 4)
            print(f"  {i*10:>4}s {frac[i]:>5.1f}% {bar}")

    (FULL / "elec_final.json").write_text(
        json.dumps([{k: v for k, v in r.items() if k != "_elo"} for r in rows],
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n写入 {FULL / 'elec_final.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
