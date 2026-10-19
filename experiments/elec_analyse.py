# -*- coding: utf-8 -*-
"""用（已定标的）周期峰尺子量产物：电音是否随「请求音高」涨。"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from elec_ruler import frame_stats, summarise  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
try:
    sys.path.insert(0, str(ROOT / "m2_server"))
    import audition_score
except Exception:
    audition_score = None


def cam(path, ref):
    if audition_score is None:
        return None
    try:
        audition_score._prepare_paths()
        return round(audition_score._secs(Path(path), Path(ref)), 4)
    except Exception as e:
        return f"err:{type(e).__name__}"


def line(label, path, extra=""):
    rows = frame_stats(path)
    s = summarise(rows)
    if not s.get("frames"):
        print(f"{label:34s} 无有效帧")
        return s
    print(f"{label:34s} 帧{s['frames']:>6}  输出F0中位{s['f0_median']:>7.1f}Hz  "
          f"周期峰 p10={s['periodicity_p10']:.3f} p50={s['periodicity_p50']:.3f} "
          f"p90={s['periodicity_p90']:.3f}{extra}")
    return s


C = ROOT / "media/covers"
ELEC = C / "elec"

print("=" * 108)
print("A · 只改「请求音高」（输入=模型自己的训练素材，域内）—— RVC 臂")
print("=" * 108)
ref = ROOT / "media/ft_work/ppt12/ref_24k.wav"
for p in (0, 5, 7, 12, 16, 19):
    f = ELEC / f"rvc_p{p:+03d}.wav"
    if f.exists():
        line(f"  RVC pitch {p:+3d} (期望 {108.0*2**(p/12):.0f}Hz)", f,
             f"  CAM++(你)={cam(f, ref)}")

print()
print("=" * 108)
print("B · 机械变调对照（同一音频直接重采样，不过模型）—— 它不该随音高劣化")
print("=" * 108)
for p in (0, 5, 7, 12, 16, 19):
    f = ELEC / f"mech_p{p:+03d}.wav"
    if f.exists():
        line(f"  机械 pitch {p:+3d} (期望 {108.0*2**(p/12):.0f}Hz)", f)

print()
print("=" * 108)
print("C · 域内天花板：输入 vs 过模型（pitch 0，模型自己的域）")
print("=" * 108)
for f, lab in ((ELEC / "00_in_domain_40k.wav", "  输入（你自己的说话素材）"),
               (ELEC / "rvc_p+00.wav", "  过同一模型之后")):
    if f.exists():
        line(lab, f)

print()
print("=" * 108)
print("D · 真正的产物（《唯一》）")
print("=" * 108)
for rel, lab in (
    ("song2/01_vocals.wav", "  原唱人声（未换声）"),
    ("song2/mypitch/vocal_p+00.wav", "  换声 · 保原调 (pitch 0)"),
    ("song2/mypitch/vocal_p-12.wav", "  换声 · 降八度 (pitch −12)"),
):
    f = C / rel
    if f.exists():
        line(lab, f, f"  CAM++(你)={cam(f, ref)}")

print()
print("=" * 108)
print("E · 按输出音高分箱：RVC pitch 0 臂（域内，唯一变量=音高）")
print("=" * 108)
for p in (0, 12, 19):
    f = ELEC / f"rvc_p{p:+03d}.wav"
    if not f.exists():
        continue
    s = summarise(frame_stats(f))
    print(f"  RVC pitch {p:+3d}:")
    for k, v in s.get("by_f0", {}).items():
        print(f"      F0 {k:>9s} Hz  n={v['n']:>5}  周期峰 p50={v['p50']:.3f}")
