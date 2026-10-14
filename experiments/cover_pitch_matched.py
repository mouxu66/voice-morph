# -*- coding: utf-8 -*-
"""★ 同音高对照：把"音域扫描"里**尺子的音高偏置**扣掉，看真正的音色保留。

背景（2026-10-14，`cover_pitch_ruler.py` 实测）
    拿**同一个人**的参考音只做磁带式变调，CAM++ 就从上到下崩掉：
    0 档 1.0 → ±5 档 ~0.21 → +12 档 0.043（**波动 0.9573**）。
    也就是说 CAM++ 对"音高/共振峰不同"极度敏感 —— 它是在判**说话人**，
    而变调后的声音在它看来"不是同一个人"（这其实是它**正确**的行为：
    声道被缩放确实改变了音色）。

    后果：`cover_pitch_scan.py` 那条"变调越高 CAM++ 越低"的曲线，
    **不能**直接读成"高音上你的音色丢了" —— 里面混着尺子对音高的偏置。

做法（把偏置扣掉）
    对每个档位 p，除了原来的"输出 vs 未变调参考"，再加一个
    **"输出 vs 同档位变调后的参考"**（`ref_shifted_p`）。
      · 若 `vs 同档参考` 明显高于 `vs 未变调参考` ⇒ 输出确实带着你的音色，
        只是处在另一个音高 —— 那"像不像"该跟**同音高的你**比才公平；
      · 若两者都低 ⇒ 高音上真的没保住音色。

    ★ 两个数**一起**报，不挑一个信（同族 #177：两个判据打架时先做解剖）。
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
    ap = argparse.ArgumentParser(description="同音高对照：扣掉尺子的音高偏置")
    ap.add_argument("--scan", type=Path, required=True, help="pitch_scan.json")
    ap.add_argument("--ref", type=Path, required=True, help="用户参考音（说话）")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    scan_p = args.scan if args.scan.is_absolute() else ROOT / args.scan
    ref = args.ref if args.ref.is_absolute() else ROOT / args.ref
    out = (args.out or scan_p.parent)
    out = out if out.is_absolute() else ROOT / out
    out.mkdir(parents=True, exist_ok=True)

    import librosa

    import cover_metrics

    scan = json.loads(scan_p.read_text(encoding="utf-8"))

    y, sr = sf.read(str(ref), dtype="float32", always_2d=True)
    y16 = librosa.resample(y.mean(1), orig_sr=sr, target_sr=16000)

    rows = []
    print(f"{'档位':>6} {'输出中位Hz':>11} {'vs 未变调参考':>13} {'vs 同档参考':>12} "
          f"{'同档更好?':>10}")
    for r in scan["rows"]:
        p = int(r["pitch"])
        wav = Path(r["wav"])
        if not wav.exists():
            continue
        # 同档参考：把参考音按同一个 pitch 做磁带式变调
        ref_shifted = out / f"_refshift{p:+03d}.wav"
        s, _ = cpr.tape_shift(y16, 16000, p)
        sf.write(str(ref_shifted), s, 16000)

        sim_orig = cover_metrics.secs(wav, out / "_ref_orig_16k.wav") \
            if (out / "_ref_orig_16k.wav").exists() else None
        if sim_orig is None:
            sf.write(str(out / "_ref_orig_16k.wav"), y16, 16000)
            sim_orig = cover_metrics.secs(wav, out / "_ref_orig_16k.wav")
        sim_same = cover_metrics.secs(wav, ref_shifted)
        rows.append({"pitch": p, "output_median_f0_hz": r.get("median_f0_hz_expected"),
                     "secs_vs_unshifted_ref": sim_orig,
                     "secs_vs_same_pitch_ref": sim_same})
        flag = "✓" if (sim_same or 0) > (sim_orig or 0) else "✗"
        print(f"{p:>+6} {str(r.get('median_f0_hz_expected')):>11} "
              f"{str(sim_orig):>13} {str(sim_same):>12} {flag:>10}")

    rep = {"scan": str(scan_p), "ref": str(ref), "rows": rows,
           "note": "vs 同档参考 = 与『同样变调后的你』比 —— 扣掉了尺子对音高的偏置"}
    (out / "pitch_matched.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print("★ 读法：『vs 同档参考』比『vs 未变调参考』高得越多，说明输出越是在")
    print("   『另一个音高上的你』，而不是『丢了你的音色』。")
    print(f"→ {out / 'pitch_matched.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
