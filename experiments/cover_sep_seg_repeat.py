# -*- coding: utf-8 -*-
"""副歌段那个 −1.66 dB 抗不抗得住重复（噪声底 0.34 dB，所以它必须显著大于它）。

做法：同一段（副歌 100–130s）各跑 **3 次**两个模型，看"ft 比 base 干净"这个
**方向**是否每次都成立，以及中位差是多少。
    · 若 3/3 方向一致且中位差 > 2×抖动 ⇒ 可作为结论；
    · 若方向翻转 ⇒ 必须如实写"分辨率之内，判不了"。
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


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    seg = ROOT / "media/covers/sepmodels/seg/seg_100.wav"
    out = ROOT / "media/covers/sepmodels/seg_repeat"
    out.mkdir(parents=True, exist_ok=True)

    res: dict[str, list[float]] = {}
    for m in csm.MODELS:
        vals = []
        for i in range(1, N + 1):
            d = out / f"{m}_{i}"
            d.mkdir(parents=True, exist_ok=True)
            v, _a, _t = csm.run_demucs(seg, m, d)
            db = csm.frame_db(v)
            q = float(db[db <= np.percentile(db, 15)].mean())
            vals.append(round(q, 2))
            print(f"{m:14s} 第 {i} 次：{q:>7.2f} dB")
        res[m] = vals

    base, ft = res[csm.MODELS[0]], res[csm.MODELS[1]]
    diffs = [b - f for b, f in zip(base, ft)]   # 正 = ft 更干净（更负的电平）
    print()
    print(f"{csm.MODELS[0]:14s} 中位 {statistics.median(base):>7.2f} dB  {base}")
    print(f"{csm.MODELS[1]:14s} 中位 {statistics.median(ft):>7.2f} dB  {ft}")
    print(f"差值（base−ft，正=ft 更干净）：{['%+.2f' % d for d in diffs]}")
    print(f"中位差 {statistics.median(diffs):+.2f} dB · 抖动门限 0.34 dB")

    all_pos = all(d > 0 for d in diffs)
    med = statistics.median(diffs)
    verdict = ("ft 更干净（3/3 方向一致，中位差 %.2f dB）" % med if all_pos and med > 0.7
               else "分辨率之内 —— 判不了，如实记为未测")
    print(f"★ 结论：{verdict}")

    (out / "seg_repeat.json").write_text(json.dumps(
        {"models": res, "diffs_base_minus_ft": diffs,
         "median_diff_db": med, "jitter_db": 0.34, "verdict": verdict},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {out / 'seg_repeat.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
