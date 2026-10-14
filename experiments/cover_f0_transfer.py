# -*- coding: utf-8 -*-
"""**高音是哪来的？** —— 量换声前后的 F0 曲线关系（回答用户那个问题）。

用户的问题（2026-10-14）：
    「最近很火的 AI 唱歌他们怎么做到的？我们就不能把我的声音复刻进去吗？
      他那种调唱得很高，那种普通人根本唱不来的那种」

要回答它，必须先钉死一个事实：**我们这条链路（SVC / 音色转换）保不保留原唱的旋律？**
    · 若保留 ⇒ 「唱得高」**不是**目标音色的能力，而是**原唱唱上去的**；
      我们只是把音色换掉 ⇒ 用户的音色**也能"唱"到那个高度**（借原唱的嗓子）。
    · 若不保留 ⇒ 那才需要目标音色自己有那个音域。

做法（都是可复算的量）：
  ① 量原唱人声轨的 F0 曲线；② 量换声产物的 F0 曲线；③ 逐帧对齐比较：
     相关系数 / 中位差（半音）/ 超出目标音色训练音域的比例。
  ★ 同时比 pitch=0（保原调）与 pitch=−12（降八度）两版 —— 这正是用户听过的那两版。

跑法（主 venv）：
    .venv/Scripts/python.exe experiments/cover_f0_transfer.py \
        --orig media/covers/lorezo_feed_the_one/01_original_vocal.wav \
        --conv media/covers/lorezo_feed_the_one/02_my_voice_vocal.wav \
        --ref  media/ft_work/ppt12/ref_24k.wav
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import numpy as np  # noqa: E402


def f0_curve(path: Path, sr_target: int = 100) -> tuple[np.ndarray, float]:
    """逐帧 F0（Hz，0 = 未检出）+ 该段的中位基频。

    用 librosa.pyin（与 `_pitch_suggest` 的 pyin 口径一致），固定 hop 让它
    变成"每 10ms 一个点"的等间隔序列，两份才能逐帧对齐。
    """
    import librosa

    y, sr = librosa.load(str(path), sr=16000, mono=True)
    f0, voiced, _ = librosa.pyin(
        y, fmin=65.0, fmax=1000.0, sr=sr,
        hop_length=int(16000 / sr_target),
    )
    f0 = np.nan_to_num(np.asarray(f0, dtype=np.float64), nan=0.0)
    voiced_vals = f0[f0 > 0]
    median = float(np.median(voiced_vals)) if len(voiced_vals) else 0.0
    return f0, median


def semitones(a: float, b: float) -> float:
    return 12.0 * float(np.log2(a / b)) if a > 0 and b > 0 else float("nan")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="换声前后 F0 曲线关系（高音哪来的）")
    ap.add_argument("--orig", required=True, type=Path, help="原唱人声轨")
    ap.add_argument("--conv", required=True, type=Path, help="换声产物（vocal）")
    ap.add_argument("--ref", type=Path, default=None,
                    help="目标音色的参考音（用来定它的训练音域）")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    orig = args.orig if args.orig.is_absolute() else ROOT / args.orig
    conv = args.conv if args.conv.is_absolute() else ROOT / args.conv
    out = (args.out or (ROOT / "media/covers/f0transfer"))
    out = out if out.is_absolute() else ROOT / out
    out.mkdir(parents=True, exist_ok=True)

    rep: dict = {"orig": str(orig), "conv": str(conv)}

    f0_o, med_o = f0_curve(orig)
    f0_c, med_c = f0_curve(conv)
    n = min(len(f0_o), len(f0_c))
    a, b = f0_o[:n], f0_c[:n]

    rep["median_f0_hz"] = {"orig": round(med_o, 1), "conv": round(med_c, 1)}
    rep["median_shift_semitones"] = round(semitones(med_c, med_o), 2)

    # ① 只在两边都检出（有声）的帧上比 —— 静音帧会把相关性抬成假的 1.0
    both = (a > 0) & (b > 0)
    rep["frames"] = {"total": int(n), "both_voiced": int(both.sum()),
                     "orig_only": int(((a > 0) & (b <= 0)).sum()),
                     "conv_only": int(((a <= 0) & (b > 0)).sum())}
    if both.sum() > 100:
        aa, bb = np.log2(a[both]), np.log2(b[both])
        rep["log_f0_corr"] = round(float(np.corrcoef(aa, bb)[0, 1]), 4)
        diffs = 12.0 * (bb - aa)
        rep["frame_shift_semitones"] = {
            "median": round(float(np.median(diffs)), 2),
            "p10": round(float(np.percentile(diffs, 10)), 2),
            "p90": round(float(np.percentile(diffs, 90)), 2),
            "spread_p90_p10": round(float(np.percentile(diffs, 90) - np.percentile(diffs, 10)), 2),
        }

    # ② 高音段：原唱最高的那 10% 帧，在换声产物里保住了多少
    hi = a > 0
    if hi.sum() > 100:
        thr = float(np.percentile(a[hi], 90))
        top = a >= thr
        rep["top10pct_frames"] = {
            "orig_threshold_hz": round(thr, 1),
            "count": int(top.sum()),
            "kept_voiced_in_conv_pct": round(100.0 * float((b[top] > 0).mean()), 1),
        }
        kept = b[top] > 0
        if kept.sum() > 20:
            rep["top10pct_frames"]["median_conv_hz"] = round(float(np.median(b[top][kept])), 1)
            rep["top10pct_frames"]["median_shift_semitones"] = round(
                semitones(float(np.median(b[top][kept])), float(np.median(a[top][kept]))), 2)

    # ③ 目标音色的训练音域（用它的参考音中位基频当代表）
    if args.ref:
        ref = args.ref if args.ref.is_absolute() else ROOT / args.ref
        if ref.exists():
            _f, med_r = f0_curve(ref)
            rep["ref_median_f0_hz"] = round(med_r, 1)
            if hi.sum() > 100:
                # 原唱有多少比例的音高超过"目标音色参考音高 + 一个八度"（= 远超出其舒适区）
                far = (a > 0) & (a > med_r * 2)
                rep["beyond_ref_plus_octave_pct"] = round(100.0 * float(far.mean()), 1)

    if args.ref and rep.get("ref_median_f0_hz") and rep["median_f0_hz"]["orig"]:
        rep["orig_vs_ref_semitones"] = round(
            semitones(rep["median_f0_hz"]["orig"], rep["ref_median_f0_hz"]), 1)

    (out / "f0_transfer.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(rep, ensure_ascii=False, indent=2))
    print(f"\n→ {out / 'f0_transfer.json'}")
    print("\n★ 读法：`log_f0_corr` 接近 1 且 `frame_shift_semitones.spread` 很小")
    print("   ⇒ 换声**逐帧跟随原唱的音高**（旋律是原唱的，不是目标音色自己唱的）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
