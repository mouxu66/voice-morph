# -*- coding: utf-8 -*-
"""分离模型对照：换一段**副歌**再验一次（防"结论只对这一段成立"）。

为什么需要（2026-10-14）：
    第一轮用的是整首歌的**安静帧**判据，结论"两个模型差不多"。
    但整首歌里安静帧大多是间奏/前奏，而**副歌**（鼓+贝斯+合成器全开）才是
    分离器最吃力的地方 —— 若 ft 的优势只在密集段落体现，用整首平均就会看不见。

做法：把整首歌切成两段（主歌段 30–60s / 副歌段 100–130s），各自跑两个模型，
比较"该段内人声轨在最安静 15% 帧上的电平"（同 ruler_check 的口径）。
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))
sys.path.insert(0, str(ROOT / "experiments"))

import importlib.util  # noqa: E402

import numpy as np  # noqa: E402

from common import find_ffmpeg  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "cover_sep_models", ROOT / "experiments" / "cover_sep_models.py")
csm = importlib.util.module_from_spec(_spec)
sys.modules["cover_sep_models"] = csm
_spec.loader.exec_module(csm)

SEGMENTS = [("主歌段", 30.0, 30.0), ("副歌段", 100.0, 30.0)]


def cut(src: pathlib.Path, dst: pathlib.Path, start: float, secs: float) -> None:
    cmd = [find_ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{start:.2f}",
           "-t", f"{secs:.2f}", "-i", str(src), "-ar", "44100", "-ac", "2", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError("切段失败：" + (r.stderr or "")[:300])


def main() -> int:
    # 重定向/GBK 控制台下 `−`（U+2212）会 UnicodeEncodeError —— 本项目反复踩过，
    # 入口一律自己钉 UTF-8（见 `test_tool_stdout_encoding.py` 的规矩）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    out = ROOT / "media/covers/sepmodels/seg"
    out.mkdir(parents=True, exist_ok=True)
    full = ROOT / "media/covers/sepmodels/_song_44k.wav"
    if not full.exists():
        print(f"[!] 缺 {full}（先跑 cover_sep_models.py）")
        return 2

    report = {"segments": []}
    for label, start, secs in SEGMENTS:
        seg = out / f"seg_{int(start)}.wav"
        cut(full, seg, start, secs)
        row = {"label": label, "start_s": start, "seconds": secs, "models": {}}
        for m in csm.MODELS:
            t = time.time()
            v, _a, _took = csm.run_demucs(seg, m, out)
            db = csm.frame_db(v)
            q = float(db[db <= np.percentile(db, 15)].mean())
            row["models"][m] = {"quiet15_dbfs": round(q, 2),
                                "wall_s": round(time.time() - t, 1),
                                "vocals": str(v)}
            print(f"[{label}] {m:14s} 安静15% {q:>7.2f}dB  （{row['models'][m]['wall_s']}s）")
        a = row["models"][csm.MODELS[0]]["quiet15_dbfs"]
        b = row["models"][csm.MODELS[1]]["quiet15_dbfs"]
        row["ft_minus_base_db"] = round(b - a, 2)
        print(f"[{label}] ft − base = {row['ft_minus_base_db']:+.2f} dB "
              f"（负=ft 更干净）")
        report["segments"].append(row)

    (out / "seg_compare.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out / 'seg_compare.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
