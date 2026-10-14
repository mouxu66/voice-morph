# -*- coding: utf-8 -*-
"""验尺子：上面那把"残留"尺子**到底能不能分辨两个不同的分离结果**？

为什么必须做（2026-10-14，`m1f` 的教训）
    `cover_sep_models.py` 给出的两行数字几乎一样（残留差 0.5 dB、人声差 0.01 dB）。
    但"量出来一样"有两种可能：
      ① 两个模型真的差不多；
      ② **我选的判据/门槛根本分辨不出差异**（尺子失灵）。
    这两种在报告里长得**一模一样**，所以必须先给尺子做一次"已知不同"的对照。

做法：拿**同一首歌**的两条已知不同的轨当输入 —— 原唱人声（`01_original_vocal.wav`）
与**整首歌**（含满伴奏）—— 分别当作"要测的 vocals"过同一套判据。
若尺子有效，整首歌的"安静帧残留"必须**显著高于**原唱人声（因为它字面上就带着伴奏）。
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import numpy as np  # noqa: E402

sys.path.insert(0, str(ROOT / "experiments"))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "cover_sep_models", ROOT / "experiments" / "cover_sep_models.py")
csm = importlib.util.module_from_spec(_spec)
sys.modules["cover_sep_models"] = csm
_spec.loader.exec_module(csm)

CASES = [
    ("原唱人声轨（demucs 分出来的）",
     "media/covers/lorezo_feed_the_one/01_original_vocal.wav"),
    ("整首歌（字面上就带满伴奏）",
     "media/songs/lorezo_feed_the_one.mp3"),
    ("htdemucs 的人声轨",
     "media/covers/sepmodels/htdemucs/_song_44k/vocals.wav"),
    ("htdemucs_ft 的人声轨",
     "media/covers/sepmodels/htdemucs_ft/_song_44k/vocals.wav"),
]


def main() -> int:
    rows = []
    for label, rel in CASES:
        p = ROOT / rel
        if not p.exists():
            print(f"[!] 缺 {rel}")
            continue
        db = csm.frame_db(p)
        rows.append({
            "label": label,
            "mean_dbfs": round(float(db.mean()), 2),
            "quiet15_dbfs": round(float(db[db <= np.percentile(db, 15)].mean()), 2),
            "loud70_dbfs": round(float(db[db >= np.percentile(db, 70)].mean()), 2),
            "p05_dbfs": round(float(np.percentile(db, 5)), 2),
        })
        print(f"{label:34s} 均值 {rows[-1]['mean_dbfs']:>7.2f}dB · "
              f"安静15% {rows[-1]['quiet15_dbfs']:>7.2f}dB · "
              f"主导70% {rows[-1]['loud70_dbfs']:>7.2f}dB")

    print()
    if len(rows) >= 2:
        spread_known = rows[1]["quiet15_dbfs"] - rows[0]["quiet15_dbfs"]
        print(f"★ 已知不同的两例（整首歌 vs 原唱人声）在『安静15%』上差 "
              f"{spread_known:+.2f} dB —— 这就是这把尺子的**分辨率下界**。")
        for r in rows[2:]:
            d = r["quiet15_dbfs"] - rows[1]["quiet15_dbfs"]
            print(f"   {r['label']} 相对原唱人声：{d:+.2f} dB")
    out = ROOT / "media/covers/sepmodels/ruler_check.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
