#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
录音质检脚本 —— 用于 Qwen3-TTS / RVC 微调前的素材验收。

不依赖 GPU，只用 soundfile + numpy。
阈值参考本项目已知良好样本（ft_s1/2/3：RMS 0.26~0.29、静音 17~26%、几乎无削波）。

用法：
    # 单个文件
    python tools/check_recording.py D:/变声/recordings/me.wav
    # 整个目录（逐个 wav 统计 + 汇总）
    python tools/check_recording.py D:/变声/recordings/
    # 多个文件 / 通配
    python tools/check_recording.py a.wav b.wav
    # 输出 JSON（便于接入 CI / 验收流水线）
    python tools/check_recording.py recordings/ --json out.json

判定（PASS / WARN / FAIL）维度：
    - 采样率须为 24000（与模型一致）
    - 声道须为单声道
    - 总时长（多个文件累加）建议 >= 15 分钟（微调目标 15~30 分钟）
    - RMS 不应过低（<0.03 视为离麦太远 / 增益太低）
    - 削波占比应 < 0.1%（顶满刻度的样本占比）
    - 静音占比应在合理区间（5%~40% 为佳；>50% 死气太多）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np
import soundfile as sf

# ---- 阈值常量（集中放置，便于调参） ----
TARGET_SR = 24000
SILENCE_THRESH = 0.01          # |x| < 0.01 视为静音
RMS_TOO_QUIET = 0.03          # 均值 RMS 低于此值视为太轻
CLIP_RATIO_FAIL = 0.001       # 削波样本占比 > 0.1% 判 FAIL
CLIP_RATIO_WARN = 0.0002      # > 0.02% 提醒
SILENCE_WARN_HIGH = 0.50      # 静音占比 > 50% 死气太多
SILENCE_WARN_LOW = 0.02       # 静音占比 < 2% 几乎无停顿
MIN_TOTAL_SEC = 15 * 60       # 微调建议最低总时长


def analyze_file(path: str) -> dict:
    """对单个 wav 做客观质检，返回指标字典。"""
    info = sf.info(path)
    data, sr = sf.read(path, dtype="float32", always_2d=False)
    # 统一成单声道
    if data.ndim > 1:
        data = data.mean(axis=1)
    n = len(data)
    if n == 0:
        return {"path": path, "error": "empty file", "ok": False}

    peak = float(np.max(np.abs(data))) if n else 0.0
    rms = float(np.sqrt(np.mean(data ** 2))) if n else 0.0
    # 静音占比
    silent = int(np.sum(np.abs(data) < SILENCE_THRESH))
    silence_ratio = silent / n if n else 1.0
    # 削波占比（顶到 0.999 以上视为削波）
    clipped = int(np.sum(np.abs(data) >= 0.999))
    clip_ratio = clipped / n if n else 0.0
    dur = n / sr if sr else 0.0

    return {
        "path": path,
        "sr": sr,
        "channels": info.channels,
        "duration_sec": round(dur, 2),
        "rms": round(rms, 4),
        "peak": round(peak, 4),
        "silence_ratio": round(silence_ratio, 4),
        "clipped_samples": clipped,
        "clip_ratio": round(clip_ratio, 6),
        "ok": True,
    }


def verdict(m: dict) -> str:
    """对单文件做维度判定，返回 'PASS' / 'WARN' / 'FAIL' 与原因。"""
    if not m.get("ok"):
        return "FAIL", ["文件无法解析或为空"]
    issues = []
    if m["sr"] != TARGET_SR:
        issues.append(f"采样率 {m['sr']} != 目标 {TARGET_SR}")
    if m["channels"] != 1:
        issues.append(f"声道数 {m['channels']} != 1(单声道)")
    if m["rms"] < RMS_TOO_QUIET:
        issues.append(f"RMS {m['rms']} 过低(<{RMS_TOO_QUIET})，离麦太远或增益低")
    if m["clip_ratio"] > CLIP_RATIO_FAIL:
        issues.append(f"削波占比 {m['clip_ratio']*100:.3f}% 过高(>{CLIP_RATIO_FAIL*100:.1f}%)")
    elif m["clip_ratio"] > CLIP_RATIO_WARN:
        issues.append(f"削波占比 {m['clip_ratio']*100:.3f}% 略高，留意")
    if m["silence_ratio"] > SILENCE_WARN_HIGH:
        issues.append(f"静音占比 {m['silence_ratio']*100:.1f}% 过高，死气太多")
    elif m["silence_ratio"] < SILENCE_WARN_LOW:
        issues.append(f"静音占比 {m['silence_ratio']*100:.1f}% 过低，几乎无停顿")

    if any("过高" in i or "过低" in i or "!= " in i for i in issues):
        return "FAIL", issues
    if issues:
        return "WARN", issues
    return "PASS", []


def collect_paths(inputs: list[str]) -> list[str]:
    paths: list[str] = []
    for item in inputs:
        if os.path.isdir(item):
            paths.extend(sorted(glob.glob(os.path.join(item, "**", "*.wav"), recursive=True)))
            paths.extend(sorted(glob.glob(os.path.join(item, "**", "*.flac"), recursive=True)))
        elif os.path.isfile(item):
            paths.append(item)
        else:
            # 当作 glob 模式
            paths.extend(sorted(glob.glob(item, recursive=True)))
    # 去重保序
    seen, uniq = set(), []
    for p in paths:
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            uniq.append(p)
    return uniq


def fmt_dur(sec: float) -> str:
    m = int(sec // 60)
    s = int(sec % 60)
    return f"{m}分{s:02d}秒" if m else f"{s}秒"


def main():
    ap = argparse.ArgumentParser(description="录音素材客观质检")
    ap.add_argument("inputs", nargs="+", help="wav 文件路径 / 目录 / glob 模式")
    ap.add_argument("--json", help="可选：把汇总结果写成 JSON 文件", default=None)
    args = ap.parse_args()

    paths = collect_paths(args.inputs)
    if not paths:
        print("未找到任何 wav 文件。", file=sys.stderr)
        sys.exit(2)

    results = []
    total_sec = 0.0
    total_clip = 0
    total_samples = 0
    weighted_rms = 0.0
    weighted_sil = 0.0
    all_warn_fail = []

    print(f"共扫描 {len(paths)} 个文件\n" + "=" * 64)
    for p in paths:
        try:
            m = analyze_file(p)
        except Exception as e:  # noqa: BLE001
            print(f"[ERR ] {p}\n        {e}")
            results.append({"path": p, "error": str(e)})
            continue
        status, reasons = verdict(m)
        all_warn_fail.extend(reasons)
        total_sec += m["duration_sec"]
        total_clip += m["clipped_samples"]
        total_samples += int(m["duration_sec"] * m["sr"]) if m["sr"] else 0
        weighted_rms += m["rms"] * m["duration_sec"]
        weighted_sil += m["silence_ratio"] * m["duration_sec"]
        results.append({**m, "verdict": status, "reasons": reasons})

        tag = {"PASS": "PASS", "WARN": "WARN", "FAIL": "FAIL"}[status]
        print(f"[{tag}] {os.path.basename(p)}")
        print(f"       时长 {fmt_dur(m['duration_sec'])} | sr {m['sr']} | 声道 {m['channels']} "
              f"| RMS {m['rms']} | peak {m['peak']}")
        print(f"       静音 {m['silence_ratio']*100:.1f}% | 削波 {m['clip_ratio']*100:.3f}% "
              f"({m['clipped_samples']} samples)")
        if reasons:
            for r in reasons:
                print(f"       - {r}")
        print()

    # 汇总
    avg_rms = weighted_rms / total_sec if total_sec else 0.0
    avg_sil = weighted_sil / total_sec if total_sec else 0.0
    total_clip_ratio = total_clip / total_samples if total_samples else 0.0

    print("=" * 64)
    print("汇总")
    print(f"  文件数        : {len(results)}")
    print(f"  总时长        : {fmt_dur(total_sec)}  (微调建议 >= {fmt_dur(MIN_TOTAL_SEC)})")
    print(f"  加权平均 RMS  : {avg_rms:.4f}")
    print(f"  加权平均静音  : {avg_sil*100:.1f}%")
    print(f"  总削波占比    : {total_clip_ratio*100:.4f}%")

    # 总时长判定
    dur_status = "PASS" if total_sec >= MIN_TOTAL_SEC else ("WARN" if total_sec >= 5 * 60 else "FAIL")
    print(f"  时长达标      : {dur_status} ({fmt_dur(total_sec)} / {fmt_dur(MIN_TOTAL_SEC)})")

    summary = {
        "files": len(results),
        "total_duration_sec": round(total_sec, 2),
        "avg_rms": round(avg_rms, 4),
        "avg_silence_ratio": round(avg_sil, 4),
        "total_clip_ratio": round(total_clip_ratio, 6),
        "duration_status": dur_status,
        "details": results,
    }

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 已写出: {args.json}")

    # 退出码：有 FAIL 返回 1，便于 CI
    has_fail = any(d.get("verdict") == "FAIL" or "error" in d for d in results)
    sys.exit(1 if has_fail else 0)


if __name__ == "__main__":
    main()
