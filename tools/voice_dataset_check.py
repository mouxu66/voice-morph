"""音色素材带宽体检：判断一批录音能不能训出全带宽的 RVC 模型。

为什么需要它（2026-10-20 实测）：
用户裁决「开了 BWE 就全是电音，还夹着嗓子」。追下去发现根不在 BWE ——
`myvoice_supp2026` 的训练素材（85 个 ppt12_* PPT 讲解录音）相对 0.2–3k 的谱形是：

    4–5k −33.9 dB   5–6k −50.0 dB   8–12k −61.1 dB

5k 处一个 16 dB 的断崖。模型只学得到它听过的频段，输出干声也在同一位置断崖
（实测干声 4.0–4.5k 比原唱低 25.8 dB）。所以 8k 以上根本没有可补的信息，
任何 BWE（神经网络 / 谱折叠 / 梳状整形）都只能编 —— 气口编成纯音（SFM 0.10
vs 真人 0.48），响段编成宽带噪声（0.28 vs 0.18），听感就是「嗡」和「嘶」。

对照：Qwen TTS 生成的 kangaroo 素材 5–6k 是 −36.4、8–12k 还有 −39.6，不掉崖。
→ 素材的带宽上限直接决定模型的输出带宽，这是训练前就该查的一项。

用法：
    python tools/voice_dataset_check.py <目录或 wav...> [--json out.json]
    python tools/voice_dataset_check.py D:/RVC/dataset_raw/myvoice_supp2026_40k

判定：以 0.2–3k 为 0 dB 基准（消除录音音量差异）。
    · 8–12k ≥ −50 dB  合格（有真实高频，训出来的模型能补高频）
    · 8–12k −50…−58   边缘（能训，但 8k 以上会空）
    · 8–12k < −58 dB  不合格（训出来的模型只能到 4–5k，BWE 无解）
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

#: 频带（相对 0.2–3k）
BANDS = [(200, 1000), (1000, 2000), (2000, 3000), (3000, 4000),
         (4000, 5000), (5000, 6000), (6000, 8000), (8000, 12000), (12000, 16000)]
#: 判定用频带
KEY_BAND = (8000, 12000)
PASS_DB = -50.0
MARGINAL_DB = -58.0
#: 分析帧
NFFT = 2048
HOP = 1024
#: 只统计有声帧（0.2–3k 高于此值），避免把静音帧的噪声底算进来
VOICED_DB = -30.0


def band_profile(path: Path) -> tuple[dict, float, int]:
    """返回 (各频带相对 0.2–3k 的中位电平 dB, 采样率, 有声帧数)。"""
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    y = y.mean(axis=1)
    freqs = np.fft.rfftfreq(NFFT, 1.0 / sr)
    win = np.hanning(NFFT)
    m_lo = (freqs >= 200) & (freqs < 3000)
    masks = [(freqs >= lo) & (freqs < hi) for lo, hi in BANDS]
    rows = []
    for s in range(0, max(len(y) - NFFT, 1), HOP):
        seg = y[s:s + NFFT]
        if seg.size < NFFT:
            break
        p = np.abs(np.fft.rfft((seg - seg.mean()) * win)) ** 2
        lv = 10 * np.log10(max(p[m_lo].sum(), 1e-20))
        if lv < VOICED_DB:
            continue
        rows.append([10 * np.log10(max(p[m].sum(), 1e-20)) - lv for m in masks])
    if not rows:
        return {}, float(sr), 0
    med = np.median(np.array(rows), axis=0)
    return {b: float(v) for b, v in zip(BANDS, med)}, float(sr), len(rows)


def verdict(key_db: float) -> str:
    if not np.isfinite(key_db):
        return "无数据"
    if key_db >= PASS_DB:
        return "合格"
    if key_db >= MARGINAL_DB:
        return "边缘"
    return "不合格"


def main() -> int:
    ap = argparse.ArgumentParser(description="音色素材带宽体检")
    ap.add_argument("inputs", nargs="+", help="目录或 wav 文件")
    ap.add_argument("--limit", type=int, default=0, help="每目录最多检查几个（0=全部）")
    ap.add_argument("--json", help="把结果写到这个 json")
    a = ap.parse_args()

    files: list[Path] = []
    for item in a.inputs:
        p = Path(item)
        if p.is_dir():
            got = sorted(Path(x) for x in glob.glob(str(p / "*.wav")))
            files.extend(got[: a.limit] if a.limit else got)
        elif p.exists():
            files.append(p)
        else:
            print(f"跳过（不存在）：{p}", file=sys.stderr)
    if not files:
        print("没有可检查的文件", file=sys.stderr)
        return 2

    print(f"检查 {len(files)} 个文件；基准 = 0.2–3k，只统计有声帧（>{VOICED_DB}dB）\n")
    head = "文件".ljust(34) + "".join(f"{lo//1000}-{hi//1000}k".rjust(9) for lo, hi in BANDS)
    head += "    判定"
    print(head)
    print("-" * len(head))

    rows = []
    for f in files:
        prof, sr, nv = band_profile(f)
        if not prof:
            print(f"{f.name[:33]:<34}(无有声帧)")
            continue
        key = prof[KEY_BAND]
        v = verdict(key)
        line = f"{f.name[:33]:<34}" + "".join(f"{prof[b]:>9.1f}" for b in BANDS)
        line += f"  {v}"
        print(line)
        rows.append({"file": str(f), "sr": sr, "voiced_frames": nv,
                     "profile": {f"{lo}-{hi}": round(prof[(lo, hi)], 1) for lo, hi in BANDS},
                     "key_8_12k": round(key, 1), "verdict": v})

    if not rows:
        return 1

    # 汇总：取每个频带的中位数（这批素材整体能做到什么带宽）
    agg = {b: float(np.median([r["profile"][f"{b[0]}-{b[1]}"] for r in rows])) for b in BANDS}
    key_agg = agg[KEY_BAND]
    print("\n" + "=" * 60)
    print("整体（各频带中位）：" + "  ".join(f"{b[0]//1000}-{b[1]//1000}k {agg[b]:.1f}" for b in BANDS))
    print(f"8–12k = {key_agg:.1f} dB → {verdict(key_agg)}"
          f"（合格线 {PASS_DB:.0f}，边缘线 {MARGINAL_DB:.0f}）")
    print()
    print("判读：")
    print("  · 5–6k 比 4–5k 低 12dB 以上 = 5k 处有断崖，模型输出也会断在同一处")
    print("  · 8–12k 低于 −58dB = 训出来的模型 8k 以上是空的，补高频只能靠编")
    print("  · 常见原因：会议软件降噪、蓝牙耳机麦克风、录屏软件压缩、素材本身是电话音质")

    if a.json:
        Path(a.json).write_text(json.dumps(
            {"files": rows, "aggregate": {f"{b[0]}-{b[1]}": round(v, 1) for b, v in agg.items()},
             "key_8_12k": round(key_agg, 1), "verdict": verdict(key_agg)},
            ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n→ {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
