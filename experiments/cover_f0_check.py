# -*- coding: utf-8 -*-
"""粗判变调有没有落地：列出各轨的中位 / p90 / 最高 F0（量**人声轨**）。

⚠️ 这是**粗判**，精确口径用 `cover_f0_framewise.py`（逐帧、只在两边都有声的帧上算）。
我第一版把 `f0_full_median_hz` 打在整混上 —— 实测 pitch −12 与 pitch 0 的整混中位
只差 8 Hz（123.2 vs 131.4），那是因为**伴奏的贝斯线主导了中位数**，与人声无关。
判"变调有没有落地"必须量**人声轨**；整混那两行留着，正是为了让"被贝斯主导"肉眼可见。

可测性：F0 曲线 → 统计行是纯函数 `f0_stats`（只要 numpy），librosa 在 `main` 里惰性导入。
"""
from __future__ import annotations

import json
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):
    pass

BASE = ROOT / "media/covers/song2"
TARGETS = [
    ("原唱人声（未换声）", BASE / "01_vocals.wav"),
    ("pitch -12 人声", BASE / "mypitch/vocal_p-12.wav"),
    ("pitch  +0 人声", BASE / "mypitch/vocal_p+00.wav"),
    ("pitch -12 整混", BASE / "mypitch/full_p-12.wav"),
    ("pitch  +0 整混", BASE / "mypitch/full_p+00.wav"),
]


def f0_stats(f0, label: str, path: str) -> dict | None:
    """pyin 的 F0 曲线（NaN=无声）→ 中位/p90/最高统计行；整条无声返回 None。"""
    v = np.asarray(f0, dtype=np.float64)
    v = v[~np.isnan(v)]
    if not len(v):
        return None
    return {"label": label, "median_f0_hz": round(float(np.median(v)), 1),
            "p90_hz": round(float(np.percentile(v, 90)), 1),
            "max_hz": round(float(v.max()), 1), "path": path}


def main() -> int:
    import librosa  # 惰性：守卫只测 f0_stats，不需要 pyin

    rows = []
    print(f"{'对象':<22} {'中位F0':>9} {'p90':>8} {'最高':>8}")
    for label, p in TARGETS:
        if not p.exists():
            print(f"{label:<22} （缺）")
            continue
        y, sr = librosa.load(str(p), sr=22050, mono=True)
        f0, _v, _p = librosa.pyin(y, fmin=65, fmax=1200, sr=sr)
        r = f0_stats(f0, label, str(p))
        if r is None:
            print(f"{label:<22} （无有声帧）")
            continue
        rows.append(r)
        print(f"{label:<22} {r['median_f0_hz']:>9.1f} {r['p90_hz']:>8} {r['max_hz']:>8}")

    # 判据：变调量要按 2^(p/12) 落地（用**人声轨**比；整混被贝斯主导，不能用）
    by = {r["label"]: r for r in rows}
    o = by.get("原唱人声（未换声）")
    print()
    if o:
        for lab, expect in (("pitch -12 人声", -12), ("pitch  +0 人声", 0)):
            r = by.get(lab)
            if not r:
                continue
            got = 12 * float(np.log2(r["median_f0_hz"] / o["median_f0_hz"]))
            ok = abs(got - expect) < 1.5
            print(f"{lab:<20} 相对原唱 {got:+6.2f} 半音（期望 {expect:+d}）"
                  f" {'✓' if ok else '✗ 变调没按预期落地'}")

    out = BASE / "mypitch/f0_check.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
