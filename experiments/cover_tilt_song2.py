# -*- coding: utf-8 -*-
"""第二首歌《唯一》的绝对频段核验（排除"只是变响"）。"""
import importlib.util
import pathlib
import sys

# ★ 入口自己钉 stdout 编码（本项目铁律）：GBK 控制台下 `−`(U+2212) 会
# UnicodeEncodeError（`test_tool_stdout_encoding.py` 那条规矩）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):
    pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))
sys.path.insert(0, str(ROOT / "experiments"))

_spec = importlib.util.spec_from_file_location(
    "ab", ROOT / "experiments" / "cover_tilt_absbands.py")
ab = importlib.util.module_from_spec(_spec)
sys.modules["ab"] = ab
_spec.loader.exec_module(ab)

BASE = ROOT / "media/covers/song2/tilt_eval"
PAIRS = [("song2（唯一 85-105s）", BASE / "tilt__myvoice_tilt_t00.wav",
          BASE / "tilt__myvoice_tilt_t03.wav")]

for label, pa, pb in PAIRS:
    if not pa.exists() or not pb.exists():
        print(f"[!] 缺产物：{pa.name} / {pb.name}")
        continue
    a, b = ab.abs_bands(pa), ab.abs_bands(pb)
    print(f"=== {label} ===")
    print(f"{'':14s} {'RMS':>8} {'0-4k':>8} {'4-8k':>8} {'8-11k':>8} {'11-16k':>8}")
    for lab, m in (("t00 原样", a), ("t03 预加重", b)):
        print(f"{lab:14s} {m['rms_dbfs']:>8} {m['0-4k_dbfs']:>8} "
              f"{m['4-8k_dbfs']:>8} {m['8-11k_dbfs']:>8} {m['11-16k_dbfs']:>8}")
    rms = b["rms_dbfs"] - a["rms_dbfs"]
    print("\nt03 − t00：")
    for k in ("rms_dbfs", "0-4k_dbfs", "4-8k_dbfs", "8-11k_dbfs", "11-16k_dbfs"):
        print(f"   {k:16s} {b[k] - a[k]:+.2f} dB")
    print()
    print(f"★ 总电平差 {rms:+.2f} dB")
    print(f"   4-8k 扣掉总电平后的净变化 {(b['4-8k_dbfs'] - a['4-8k_dbfs']) - rms:+.2f} dB")
    print(f"   8-11k 净变化 {(b['8-11k_dbfs'] - a['8-11k_dbfs']) - rms:+.2f} dB")
