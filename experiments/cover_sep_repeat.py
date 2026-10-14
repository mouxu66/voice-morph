# -*- coding: utf-8 -*-
"""尺子的**噪声底**：同一模型跑两遍，那条"安静15%"读数会不会变？

为什么必须做（2026-10-14）：
    分段对照给出 ft − base = **−0.48 dB（主歌）/ −1.66 dB（副歌）**。
    但"已知不同的一对差 48 dB"只证明这把尺子**能分辨大差异**，
    不证明它**能分辨 1.66 dB** —— 要下"ft 更干净"的结论，必须先知道
    同一输入重复测量的抖动有多大（#167 的同量级纪律：效应与噪声同量级就不能下结论）。

判据：若两次跑的分离开果逐字节相同，则噪声底 = 0，1.66 dB 是**真差异**；
      若不同，就把抖动与 1.66 dB 比大小。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
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


def md5(p: pathlib.Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()[:12]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    seg = ROOT / "media/covers/sepmodels/seg/seg_100.wav"     # 副歌段（差异最大的那段）
    out = ROOT / "media/covers/sepmodels/repeat"
    out.mkdir(parents=True, exist_ok=True)

    rows = []
    for i in (1, 2):
        target = out / f"run{i}"
        target.mkdir(parents=True, exist_ok=True)
        v, _a, took = csm.run_demucs(seg, "htdemucs", target)
        db = csm.frame_db(v)
        q = float(db[db <= np.percentile(db, 15)].mean())
        rows.append({"run": i, "quiet15_dbfs": round(q, 2), "md5": md5(v),
                     "wall_s": round(took, 1), "vocals": str(v)})
        print(f"第 {i} 次：安静15% {q:>7.2f}dB · md5={rows[-1]['md5']} · {took:.1f}s")

    same_bytes = rows[0]["md5"] == rows[1]["md5"]
    jitter = abs(rows[0]["quiet15_dbfs"] - rows[1]["quiet15_dbfs"])
    print()
    print(f"两次产物逐字节相同：{same_bytes}")
    print(f"读数抖动：{jitter:.2f} dB")
    print("★ 判读：" + ("噪声底 = 0 ⇒ 分段对照的 0.48/1.66 dB 是**真差异**"
                       if same_bytes else
                       f"抖动 {jitter:.2f} dB，要与 1.66 dB 比大小再下结论"))

    (out / "repeat.json").write_text(json.dumps(
        {"rows": rows, "identical_bytes": same_bytes, "jitter_db": jitter},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
