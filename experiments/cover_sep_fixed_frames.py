# -*- coding: utf-8 -*-
"""★ 用**固定帧集**量伴奏残留（修掉"各自取自己的 15 分位"那个混淆）。

为什么要重做（2026-10-14 自查）：
    `cover_sep_segments.py` 里每个模型都取**自己**的最安静 15% 帧 —— 这有混淆：
    整体电平更高的那一个，自己的分位点自然也更高，"读数差"里混进了"整体响度差"
    （实测 ft 的整轨人声 RMS 比 base **高 0.99 dB**）。
    正确做法：**用同一批时间点**比较（在 base 判定为"没人唱"的那些帧上，
    两个模型各自还有多响）。这样时间轴对齐，比的才是"同一处的残留"。

口径：
    1. 用 base（htdemucs）整轨最安静的 15% 帧当"疑似无人声"的**固定帧集**；
    2. 在该帧集上分别量 base 与 ft 的电平；
    3. 各自跑 3 次（噪声底 0.34 dB），看"ft 更干净"这个方向稳不稳。
"""
from __future__ import annotations

import json
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))
sys.path.insert(0, str(ROOT / "experiments"))

import importlib.util  # noqa: E402

import numpy as np  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "cover_sep_models", ROOT / "experiments" / "cover_sep_models.py")
csm = importlib.util.module_from_spec(_spec)
sys.modules["cover_sep_models"] = csm
_spec.loader.exec_module(csm)

N = 3
CASES = [("整首歌", ROOT / "media/covers/sepmodels/_song_44k.wav"),
         ("副歌段", ROOT / "media/covers/sepmodels/seg/seg_100.wav")]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    out = ROOT / "media/covers/sepmodels/fixed_frames"
    out.mkdir(parents=True, exist_ok=True)
    report = {"n_runs": N, "cases": []}

    for label, src in CASES:
        if not src.exists():
            print(f"[!] 缺 {src}")
            continue
        print(f"\n=== {label}（{src.name}）===")
        # ① 先跑 base 一次，用它定"疑似无人声"的固定帧集
        d0 = out / f"{label}_base_ref"
        d0.mkdir(parents=True, exist_ok=True)
        v0, _a, _t = csm.run_demucs(src, csm.MODELS[0], d0)
        base_db0 = csm.frame_db(v0)
        quiet_idx = np.where(base_db0 <= np.percentile(base_db0, 15))[0]
        print(f"固定帧集：base 最安静的 15% = {len(quiet_idx)} 帧（时间轴对齐用）")

        vals: dict[str, list[float]] = {}
        for m in csm.MODELS:
            runs = []
            for i in range(1, N + 1):
                d = out / f"{label}_{m}_{i}"
                d.mkdir(parents=True, exist_ok=True)
                v, _a, _t = csm.run_demucs(src, m, d)
                db = csm.frame_db(v)
                n = min(len(db), len(quiet_idx))
                runs.append(round(float(db[quiet_idx[:n]].mean()) if n else float("nan"), 2))
            vals[m] = runs
            print(f"{m:14s} 固定帧集上：中位 {statistics.median(runs):>7.2f} dB  {runs}")

        base, ft = vals[csm.MODELS[0]], vals[csm.MODELS[1]]
        diffs = [b - f for b, f in zip(base, ft)]      # 正 = ft 在该帧集上更安静 = 残留更少
        med = statistics.median(diffs)
        all_pos = all(d > 0 for d in diffs)
        verdict = (f"ft 残留更少（{sum(1 for d in diffs if d > 0)}/{N} 方向一致，中位 {med:+.2f} dB）"
                   if all_pos and med > 0.34 else
                   f"分辨率之内（中位 {med:+.2f} dB，抖动 0.34 dB）—— 判不了")
        print(f"差值（base−ft，正=ft 更干净）：{['%+.2f' % d for d in diffs]}")
        print(f"★ {verdict}")
        report["cases"].append({"label": label, "fixed_frames": int(len(quiet_idx)),
                                "values": vals, "diffs": [round(d, 2) for d in diffs],
                                "median_diff_db": round(med, 2), "verdict": verdict})

    (out / "fixed_frames.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out / 'fixed_frames.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
