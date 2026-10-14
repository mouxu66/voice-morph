# -*- coding: utf-8 -*-
"""量化「高音撞 f0_max 天花板」的比例 —— 把研究员那条线索变成本机数字。

背景（2026-10-14，研究员带回 + 本机源码已核）
    `D:\RVC\infer\vc\pipeline.py:74-85` 硬编码 `f0_min=50 / f0_max=1100`，
    第 137 行 `f0_mel[f0_mel > 255] = 255` 把超出部分**夹断到同一档**。
    而变调发生在 mel 量化**之前** ⇒ 升调会把更多帧推过 1100 Hz。

    ★ 关键：夹断意味着"所有超过 f0_max 的音高都被表示成同一个值" ——
    模型没法区分 1100Hz 与 1500Hz ⇒ 那一段的**音高信息真的丢了**。
    这正是社区报的"一到高音就哑火"的机制候选。

本脚本算三件事（都是可复算的数）：
  ① 目标片段里，F0 在 **变调后**超过 1100 Hz 的帧占多少（当前档位）；
  ② 扫各档位（−12…+12），给出"被夹断帧占比"与"达到夹断所需的输入音高"；
  ③ 用 mel 公式反推**安全上限**：输入音高最多到多少 Hz 才不会被夹。

mel 公式（与源码一致）：mel = 1127·ln(1 + f/700)，量化档位 0–255，
    f0_mel_max = 1127·ln(1 + f0_max/700) ⇒ 超过它的都落进第 255 档。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import numpy as np  # noqa: E402

#: 与源码同值（`infer/vc/pipeline.py:74-75`）
F0_MIN, F0_MAX = 50.0, 1100.0


def mel(f):
    return 1127.0 * np.log(1.0 + np.asarray(f) / 700.0)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="高音撞 f0_max 天花板的占比")
    ap.add_argument("--vocal", type=Path, required=True, help="源歌声 wav（demucs vocals）")
    ap.add_argument("--start", type=float, default=78.0)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--pitches", default="-12,-8,-5,0,4,8,12")
    ap.add_argument("--out", type=Path, default=ROOT / "media/covers/f0ceiling")
    args = ap.parse_args()

    src = args.vocal if args.vocal.is_absolute() else ROOT / args.vocal
    out = args.out if args.out.is_absolute() else ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)

    import librosa

    y, sr = librosa.load(str(src), sr=16000, mono=True, offset=args.start, duration=args.seconds)
    f0, _v, _p = librosa.pyin(y, fmin=F0_MIN, fmax=2000.0, sr=sr)
    f0 = np.asarray(f0, dtype=np.float64)
    voiced = f0[~np.isnan(f0)]
    rep: dict = {
        "vocal": str(src), "start_s": args.start, "seconds": args.seconds,
        "f0_min": F0_MIN, "f0_max": F0_MAX,
        "voiced_frames": int(len(voiced)),
        "input_f0": {
            "median": round(float(np.median(voiced)), 1),
            "p90": round(float(np.percentile(voiced, 90)), 1),
            "p99": round(float(np.percentile(voiced, 99)), 1),
            "max": round(float(voiced.max()), 1),
        },
    }

    # ③ 安全上限：输入（变调后）超过 F0_MAX 就会被夹
    rep["clamp_threshold_hz"] = F0_MAX
    rep["safe_input_max_hz"] = F0_MAX           # 变调后的音高必须 ≤ 它
    # 反查：mel(1100) 对应第 255 档 —— 超过它的输入（变调后）都会撞同一档
    rep["mel_at_f0_max"] = round(float(mel(F0_MAX)), 2)
    rep["mel_of_p99_plus_12st"] = round(float(mel(rep["input_f0"]["p99"] * 2)), 2)

    rows = []
    for p in [int(x) for x in args.pitches.split(",") if x.strip()]:
        shifted = voiced * (2 ** (p / 12.0))
        clamp_pct = 100.0 * float((shifted > F0_MAX).mean())
        rows.append({
            "pitch": p,
            "shifted_median_hz": round(float(np.median(shifted)), 1),
            "shifted_p99_hz": round(float(np.percentile(shifted, 99)), 1),
            "clamped_frame_pct": round(clamp_pct, 2),
            "distinct_pitch_lost_pct": round(
                100.0 * float((shifted > F0_MAX).sum()) / max(len(shifted), 1), 2),
        })
        print(f"pitch {p:+3d} → 中位 {rows[-1]['shifted_median_hz']:6.1f}Hz · "
              f"p99 {rows[-1]['shifted_p99_hz']:7.1f}Hz · "
              f"**{clamp_pct:5.2f}% 的帧被夹到 1100Hz 同一档**")

    rep["rows"] = rows
    (out / "f0_ceiling.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"输入片段（未变调）：中位 {rep['input_f0']['median']}Hz · "
          f"p99 {rep['input_f0']['p99']}Hz · 最高 {rep['input_f0']['max']}Hz")
    print(f"f0_max={F0_MAX:.0f}Hz 对应 mel 第 {rep['mel_at_f0_max']} 档（上限 255）")
    print()
    print("★ 读法：`clamped_frame_pct` 是**该档位下音高信息真的丢失**的帧占比 ——")
    print("   被夹的帧全部表示成同一个音高，模型无法区分 1100 与 1500Hz。")
    print(f"→ {out / 'f0_ceiling.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
