# -*- coding: utf-8 -*-
"""把已切好的语音片段打包成 RVC 训练集（dataset_raw/<exp>/）。

为什么单独有这支
----------------
RVC 是 voice-to-voice，**不需要任何文字标注**（见 AGENTS.md 袋鼠音色铁律第 4 条），
只需要「一段段干净、不切在字中间的人声 wav」。所以从 TTS 路线切好的切片直接复用即可，
不必重切。

本工具只做三件事，每件都对应一个踩过的坑：
  1. **重采样到 RVC 训练采样率**（40k）—— 用多相滤波（`resample_poly`），
     不是线性插值；比率先约分（24000→40000 = 5/3），避免浮点累积误差。
  2. **峰值归一**（默认 0.95，与 2026-10-12 那次数据集保持同口径）
     —— 换数据集时只动"数据"这一个变量，别把响度也一起改了。
  3. **体检并阻断**：低于 RVC 官方推荐的 10 分钟就明确报警（不是默默产出）。
     2026-10-12 的教训：数据集只有 3.4 分钟却一路训到底，
     结论被记成"素材不够，得再录"—— 而盘上其实躺着 12.23 分钟。

用法：
    D:\\RVC\\.venv\\Scripts\\python.exe tts_trial\\make_rvc_dataset.py \\
        --src media/ft_work/ppt12/clips24k \\
        --dst D:/RVC/dataset_raw/myvoice_ppt12_full_40k
"""
import argparse
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")

#: RVC 官方推荐的最短训练集时长（秒）。低于它训出来的音色通常不充分。
RECOMMENDED_MIN_S = 600.0
#: 超过它就没必要再录了（官方 10~50 分钟理想区间的上界）。
RECOMMENDED_MAX_S = 3000.0


def resample(x: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    """多相滤波重采样（约分后调用 resample_poly）。

    `resample_poly` 要求 up/down 互质，先用 gcd 约分；
    相同采样率直接原样返回（不要走一遍无谓的滤波）。
    """
    if sr_in == sr_out:
        return np.asarray(x, dtype=np.float32)
    from scipy.signal import resample_poly

    g = math.gcd(int(sr_in), int(sr_out))
    up, down = int(sr_out) // g, int(sr_in) // g
    y = resample_poly(np.asarray(x, dtype=np.float64), up, down)
    return np.asarray(y, dtype=np.float32)


def peak_normalize(x: np.ndarray, peak: float) -> np.ndarray:
    """整体等比缩放到目标峰值。全零输入原样返回（别除零出 NaN）。"""
    m = float(np.max(np.abs(x))) if x.size else 0.0
    if m <= 0.0:
        return np.asarray(x, dtype=np.float32)
    return np.asarray(x, dtype=np.float32) * (float(peak) / m)


def load_mono(path) -> tuple:
    """读 wav → (float32 单声道, sr)。多声道取平均。"""
    a, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return a.mean(axis=1).astype(np.float32), int(sr)


def build(src_dir, dst_dir, sr_out=40000, peak=0.95, min_dur_s=1.0, limit=None):
    """把 src_dir/*.wav 打包进 dst_dir。返回报告 dict（也便于测试）。"""
    src = Path(src_dir)
    dst = Path(dst_dir)
    files = sorted(src.glob("*.wav"))
    if not files:
        raise FileNotFoundError(f"源目录没有 wav：{src}")
    if limit:
        files = files[: int(limit)]
    dst.mkdir(parents=True, exist_ok=True)

    rows = []
    skipped = []
    for f in files:
        x, sr = load_mono(f)
        dur = len(x) / sr
        if dur < min_dur_s:
            skipped.append({"file": f.name, "dur": round(dur, 3),
                            "why": f"短于 {min_dur_s}s"})
            continue
        y = resample(x, sr, sr_out)
        if peak:
            y = peak_normalize(y, peak)
        # 峰值归一后仍越界（浮点误差）时兜底夹紧，避免写文件时溢出
        y = np.clip(y, -1.0, 1.0)
        out = dst / f.name
        sf.write(str(out), (y * 32767.0).astype("<i2"), sr_out,
                 subtype="PCM_16")
        rows.append({"file": f.name, "dur": round(dur, 3),
                     "src_sr": sr, "rms_db": round(
                         float(20 * np.log10(np.sqrt(np.mean(y ** 2)) + 1e-12)), 1)})

    durs = np.array([r["dur"] for r in rows], dtype=np.float64) if rows else np.zeros(1)
    total = float(durs.sum())
    report = {
        "src_dir": str(src), "dst_dir": str(dst), "sr_out": int(sr_out),
        "peak_target": peak, "files": len(rows), "skipped": skipped,
        "total_s": round(total, 1), "total_min": round(total / 60.0, 2),
        "dur_min": round(float(durs.min()), 2), "dur_median": round(float(np.median(durs)), 2),
        "dur_mean": round(float(durs.mean()), 2), "dur_max": round(float(durs.max()), 2),
        "enough": total >= RECOMMENDED_MIN_S,
    }
    return report, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="已切好的 wav 目录")
    ap.add_argument("--dst", required=True, help="dataset_raw/<exp> 目标目录")
    ap.add_argument("--sr", type=int, default=40000, help="RVC 训练采样率")
    ap.add_argument("--peak", type=float, default=0.95,
                    help="峰值归一到该值；0 表示不归一")
    ap.add_argument("--min-dur", type=float, default=1.0, help="短于此秒数的切片丢弃")
    ap.add_argument("--limit", type=int, default=None, help="只取前 N 条（冒烟用）")
    ap.add_argument("--report", default=None,
                    help="报告 json 落点（默认放在 dataset_raw/ 下、**目录之外**）")
    args = ap.parse_args()

    report, rows = build(args.src, args.dst, sr_out=args.sr, peak=args.peak,
                         min_dur_s=args.min_dur, limit=args.limit)

    print(f"[src] {report['src_dir']}")
    print(f"[dst] {report['dst_dir']}  sr={report['sr_out']}  peak={report['peak_target']}")
    print(f"[out] {report['files']} 条  总时长 {report['total_s']}s "
          f"({report['total_min']}min)")
    print(f"[out] 片段 min={report['dur_min']} 中位={report['dur_median']} "
          f"均={report['dur_mean']} max={report['dur_max']}")
    if report["skipped"]:
        print(f"[skip] {len(report['skipped'])} 条过短被丢")
    if report["total_s"] < RECOMMENDED_MIN_S:
        print(f"[警告] 训练集 {report['total_min']}min 低于 RVC 官方推荐的 "
              f"{RECOMMENDED_MIN_S/60:.0f}min —— 音色大概率训不足，"
              f"先去补素材，别急着加 epoch。")
    else:
        print(f"[ok] {report['total_min']}min ≥ 官方推荐 {RECOMMENDED_MIN_S/60:.0f}min")

    # ★ 报告**不能写进 dst/**：RVC 的 `train.preprocess` 会把目录里的**每个文件**
    #   都当音频喂给 ffmpeg（实测 81 个 wav + 1 个 json = 82 个任务，
    #   json 那条报 ffmpeg error 并计一次"失败"）。放到 dst 的上一级。
    rp = args.report or os.path.join(os.path.dirname(str(Path(args.dst).resolve())),
                                     f"{Path(args.dst).name}_report.json")
    os.makedirs(os.path.dirname(rp), exist_ok=True)
    with open(rp, "w", encoding="utf-8") as f:
        json.dump({**report, "items": rows}, f, ensure_ascii=False, indent=2)
    print(f"[done] 报告 -> {rp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
