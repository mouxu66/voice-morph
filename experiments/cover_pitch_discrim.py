# -*- coding: utf-8 -*-
"""★ 音高匹配的判别力：同一音高下，输出更像**你**还是更像**原唱**？

为什么这样设计（2026-10-14，前面三个脚本都栽在这上面）
    ① `cover_pitch_scan` 直接比"输出 vs 未变调的你"，得到随音高单调下降的曲线 ——
       但 `cover_pitch_ruler` 实测**尺子本身有巨大音高偏置**（同一个人变调 0→±12，
       CAM++ 从 1.0 掉到 0.04~0.25），所以那条曲线**不能**读成"音色丢了"。
    ② `cover_pitch_matched` 想用"同档变调后的你"当对照扣掉偏置 —— **设计错了**：
       磁带式变调把**共振峰一起**缩放，而 RVC 离线路径**不移动共振峰**
       （研究员源码级确认：RVC 无 formant 参数）⇒ 两边根本不是同一类信号。

    ✔ 正确设计：**让两边音高相同**，然后比"更像谁"。因为偏置来自音高差，
    两边音高一样 ⇒ 偏置对两边**同等作用** ⇒ 判别力（你 vs 原唱）是干净的。

    | 比较对象 | 音高 | 频谱包络 |
    |---|---|---|
    | 输出 @ pitch p | F0×ratio | RVC 保共振峰 |
    | 你的参考音 @ 同 F0 | F0×ratio | 磁带式（移动共振峰） |
    | 原唱人声 @ 同 F0 | F0×ratio | 磁带式（移动共振峰） |

    ⚠️ 仍有残留不对称（输出的共振峰没动，两个参照的都动了）。所以本脚本**只报
    判别方向与其跨音高的一致性**，不报"像度绝对值"。这是它能诚实支持的最强结论。

判读
    · 若每个音高档上 `对你 > 对原唱` ⇒ 在任何音高上输出都更像你 ⇒ **音色没有在高音崩掉**；
    · 若某档开始反转 ⇒ 那一档之后原唱开始"赢" ⇒ 那才是音色失守的位置。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))
sys.path.insert(0, str(ROOT / "experiments"))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "cover_pitch_ruler", ROOT / "experiments" / "cover_pitch_ruler.py")
cpr = importlib.util.module_from_spec(_spec)
sys.modules["cover_pitch_ruler"] = cpr
_spec.loader.exec_module(cpr)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="音高匹配的判别力（更像你还是更像原唱）")
    ap.add_argument("--scan", type=Path, required=True, help="pitch_scan.json")
    ap.add_argument("--ref", type=Path, required=True, help="你的参考音")
    ap.add_argument("--orig", type=Path, required=True, help="原唱人声（整首或同段）")
    ap.add_argument("--orig-offset", type=float, default=78.0, help="原唱里对应的起点秒")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    scan_p = args.scan if args.scan.is_absolute() else ROOT / args.scan
    ref = args.ref if args.ref.is_absolute() else ROOT / args.ref
    orig = args.orig if args.orig.is_absolute() else ROOT / args.orig
    out = (args.out or scan_p.parent)
    out = out if out.is_absolute() else ROOT / out
    out.mkdir(parents=True, exist_ok=True)

    import librosa

    import cover_metrics

    scan = json.loads(scan_p.read_text(encoding="utf-8"))

    # 两条参照：你的参考音（整条 6s）、原唱同一段（与产物同一时间段）
    yr, sr_r = sf.read(str(ref), dtype="float32", always_2d=True)
    yr = librosa.resample(yr.mean(1), orig_sr=sr_r, target_sr=16000)[: 16000 * 6]
    yo, _sr_o = librosa.load(str(orig), sr=16000, mono=True,
                            offset=args.orig_offset, duration=args.seconds)

    def to_pitch(x: np.ndarray, target_f0: float, cur_f0: float) -> np.ndarray:
        """把 x 用磁带式变调到 target_f0（等比例 ⇒ 共振峰一起动）。"""
        if cur_f0 <= 0 or target_f0 <= 0:
            return x
        st = 12.0 * float(np.log2(target_f0 / cur_f0))
        if abs(st) < 0.05:
            return x
        return cpr.tape_shift(x, 16000, st)[0]

    f0_ref = float(np.median(librosa.yin(yr, fmin=60, fmax=900, sr=16000)))
    _f0o, _vo, _po = librosa.pyin(yo, fmin=60, fmax=1200, sr=16000)
    _vo_vals = np.asarray(_f0o, dtype=np.float64)
    _vo_vals = _vo_vals[~np.isnan(_vo_vals)]
    f0_orig = float(np.median(_vo_vals)) if len(_vo_vals) else 0.0

    print(f"你的参考音中位 F0 {f0_ref:.1f}Hz（原音高）")
    print(f"原唱同段中位 F0 {f0_orig:.1f}Hz（原音高）")
    print()
    print(f"{'档位':>6} {'输出中位F0':>11} {'对你(同音高)':>13} {'对原唱(同音高)':>15} "
          f"{'更像':>6}")

    rows = []
    for r in scan["rows"]:
        p = int(r["pitch"])
        wav = Path(r["wav"])
        if not wav.exists():
            continue
        f0_out = float(r.get("median_f0_hz_expected") or 0)
        if f0_out <= 0:
            continue
        a = to_pitch(yr, f0_out, f0_ref)
        b = to_pitch(yo, f0_out, f0_orig)
        pa = out / f"_ref_at{f0_out:.0f}.wav"
        pb = out / f"_orig_at{f0_out:.0f}.wav"
        sf.write(str(pa), a, 16000)
        sf.write(str(pb), b, 16000)
        sim_you = cover_metrics.secs(wav, pa)
        sim_orig = cover_metrics.secs(wav, pb)
        who = "你" if (sim_you or -9) > (sim_orig or -9) else "原唱"
        rows.append({"pitch": p, "output_f0_hz": round(f0_out, 1),
                     "secs_vs_you_at_same_pitch": sim_you,
                     "secs_vs_orig_at_same_pitch": sim_orig, "closer_to": who})
        print(f"{p:>+6} {f0_out:>11.1f} {str(sim_you):>13} {str(sim_orig):>15} {who:>6}")

    wins = sum(1 for r in rows if r["closer_to"] == "你")
    flips = [r["pitch"] for r in rows if r["closer_to"] != "你"]
    rep = {"scan": str(scan_p), "ref": str(ref), "orig": str(orig),
           "orig_offset_s": args.orig_offset, "seconds": args.seconds,
           "rows": rows, "you_win": wins, "total": len(rows), "flip_at": flips,
           "note": ("音高匹配 ⇒ 尺子的音高偏置对两边同等作用 ⇒ 判别方向干净；"
                    "但输出的共振峰未动、参照的动了，故只读方向不读绝对值")}
    (out / "pitch_discrim.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"★ 输出更像**你**的档位：{wins}/{len(rows)}"
          + (f"；反转发生在 {flips}" if flips else "（全档一致）"))
    print(f"→ {out / 'pitch_discrim.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
