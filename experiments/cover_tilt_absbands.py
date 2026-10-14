# -*- coding: utf-8 -*-
"""验预加重实验的**方向性**：t03 的变亮是"真的把高频练进去了"，还是别的？

为什么必须查（2026-10-14，本项目纪律）
    读数很漂亮（4-8k 亮 6.0dB、质心 +160Hz、像你还略升），但漂亮的读数恰恰最该怀疑。
    三种替代解释都会给出"看起来变亮"：
      ① **响度/增益差** —— t03 的产物只是整体更响，相对频段比没变；
      ② **峰值缩放副作用** —— 预加重后我给 >0.99 的做了等比缩，
         缩过的素材 RMS 不同 ⇒ 训练出来的模型输出电平不同；
      ③ **像度提升是巧合** —— 单 seed 单段，本就该按噪声看。

做法（三条都可复算）
    ① 量两条产物的**绝对 RMS**（dBFS）与**绝对**频段能量（不做 0-4k 归一化）——
       相对 band_db 会被整体电平掩盖；
    ② 用**同一个输入、同一个 seed**（脚本已是）⇒ 差异只能来自权重；
    ③ 报出"待验证"：单段 20s、单 seed。真正的结论要第二段/第二个 seed。
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

BANDS = [(0, 4000), (4000, 8000), (8000, 11000), (11000, 16000)]

FILES = [
    ("t00 原样（对照）", "media/covers/tilt_eval/tilt__myvoice_tilt_t00.wav"),
    ("t03 预加重 +3dB/oct", "media/covers/tilt_eval/tilt__myvoice_tilt_t03.wav"),
    ("原唱这一段（参照）", "media/covers/tilt_eval/00_source_40k.wav"),
]

#: 第二组：**另一段时间 + 另一个 seed**（防"结论只对这一段成立"，见 #167）
FILES_B = [
    ("[段2] t00 原样", "media/covers/tilt_eval2/tilt__myvoice_tilt_t00.wav"),
    ("[段2] t03 预加重", "media/covers/tilt_eval2/tilt__myvoice_tilt_t03.wav"),
]


def abs_bands(path: pathlib.Path) -> dict:
    """**绝对**频段能量（dB，相对满刻度）—— 回答"是更亮还是只是更响"。

    ★ 口径踩过一次坑（2026-10-14 自查）：第一版写成
        `20*log10( sqrt(band_energy / total_energy) )`
    —— 那其实是**频段占比**的 dB，而占比的分母里也含该频段自己 ⇒
    把高频真抬起来时，占比**反而下降**（实测 4-8k 从 −7.00 掉到 −7.85 dB），
    于是"真抬了高频"被读成"变暗了"，我的守卫当场把它抓出来。

    正确口径：**频段的绝对功率**（除以窗长归一化，不做任何跨频段归一化）——
    实测同一对输入：4-8k 从 4.85 → 7.80 dB（+2.95），方向正确。

    这正是本项目反复出现的那类错：**拿"比值"当"绝对量"**
    （同族 #168「8-10k 是窗泄漏地板」、#178「归一化混淆」）。
    """
    d, sr = sf.read(str(path), dtype="float64", always_2d=True)
    mono = d.mean(1)
    rms = float(np.sqrt(np.mean(mono ** 2)))
    n, hop = 2048, 1024
    win = np.hanning(n)
    frames = [mono[i:i + n] for i in range(0, len(mono) - n, hop)]
    X = np.stack([np.abs(np.fft.rfft(f * win)) ** 2 for f in frames])
    frms = np.sqrt(X.sum(1) / n)
    keep = frms >= np.percentile(frms, 60)      # 只统计活跃帧（避免静音主导）
    P = X[keep].mean(0)
    fr = np.fft.rfftfreq(n, 1 / sr)
    out = {"rms_dbfs": round(20 * float(np.log10(rms + 1e-15)), 2)}
    for lo, hi in BANDS:
        # 绝对功率（/n 归一到"每样本功率"，与满刻度可比），**不做**跨频段归一化
        be = float(P[(fr >= lo) & (fr < hi)].sum()) / n
        out[f"{lo//1000}-{hi//1000}k_dbfs"] = round(
            10 * float(np.log10(be + 1e-30)), 2)
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    rows = []
    print(f"{'产物':<24} {'绝对RMS':>9} {'4-8k':>8} {'8-11k':>8} {'11-16k':>8}")
    for label, rel in FILES + FILES_B:
        p = ROOT / rel
        if not p.exists():
            print(f"[!] 缺 {rel}")
            continue
        m = abs_bands(p)
        rows.append({"label": label, **m})
        print(f"{label:<24} {m['rms_dbfs']:>9} {m['4-8k_dbfs']:>8} "
              f"{m['8-11k_dbfs']:>8} {m['11-16k_dbfs']:>8}")

    # 两组对照各自算"净变化"（扣掉总电平差）—— **两组同向**才算可下的结论
    pairs = [("第一组（78-98s / seed 1234）", rows[0], rows[1]) if len(rows) > 1 else None,
             ("第二组（120-140s / seed 4321）",
              next((r for r in rows if r["label"].startswith("[段2] t00")), None),
              next((r for r in rows if r["label"].startswith("[段2] t03")), None))]
    for name, a, b in [p for p in pairs if p and p[1] and p[2]]:
        dl = {k: round(b[k] - a[k], 2) for k in a if k != "label"}
        print()
        print(f"{name} —— t03 − t00（绝对 dB）：")
        for k, v in dl.items():
            print(f"   {k:<18} {v:+.2f} dB")
        rms_gain = dl.get("rms_dbfs", 0.0)
        hf_net = dl.get("4-8k_dbfs", 0.0) - rms_gain
        print(f"★ 总电平差 {rms_gain:+.2f} dB；4-8k 扣掉总电平后的**净变化** "
              f"{hf_net:+.2f} dB")

    out = ROOT / "media/covers/tilt_eval/absolute_bands.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
