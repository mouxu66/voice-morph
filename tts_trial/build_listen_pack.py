# -*- coding: utf-8 -*-
"""生成「Qwen 零样本 vs RVC 克隆」可听对照包。

用户要的是"先看看效果跟千问相比"，光有数字不够，得能听。本脚本把同一条文本的
两个版本拼成一个文件，中间留固定静音间隔，便于盲听切换：

    [ Qwen 零样本 ] 0.35s [ RVC 克隆 ] 0.9s [ 下一条 ... ]

同时导出单条文件（前缀 A_ = Qwen，B_ = RVC），方便逐条细听。

用法：
    .venv\\Scripts\\python.exe tts_trial\\build_listen_pack.py
"""
import argparse
import json
import os
import shutil
import sys

import numpy as np
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")

SRC_DIR = r"D:\变声\media\ft_work\ppt12\myvoice_rvc"
SWEEP_DIR = os.path.join(SRC_DIR, "index_sweep")
OUT_DIR = r"D:\变声\media\ft_work\ppt12\myvoice_rvc\listen_pack"

TEXTS = {
    1: "这一节我们讲三个概念，先看第一个。",
    2: "把步骤拆开，一步一步做就不会乱。",
    3: "注意这里的条件，符号写反了结论就完全不同。",
}

TARGET_SR = 40000
GAP_WITHIN = 0.35   # A→B 间隔：短一点便于记忆比对
GAP_BETWEEN = 0.90  # 组间间隔：区分"下一组开始了"


def load(path, sr=TARGET_SR):
    a, s = sf.read(path, dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    if s != sr:  # 线性重采样（scipy 可用则用高质量 poly）
        try:
            from scipy.signal import resample_poly
            from math import gcd
            g = gcd(int(s), int(sr))
            a = resample_poly(a, sr // g, s // g)
        except ImportError:
            n = int(round(len(a) * sr / s))
            a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
    return a.astype(np.float32)


def silence(sec, sr=TARGET_SR):
    return np.zeros(int(round(sec * sr)), dtype=np.float32)


def norm(a, peak=0.85):
    """每条统一峰值，避免"响的更响"造成的假偏好（响度偏差是听测最大陷阱）。"""
    m = float(np.max(np.abs(a))) if a.size else 0.0
    return a * (peak / m) if m > 1e-6 else a


def ramp(a, ms=8, sr=TARGET_SR):
    """首尾加短淡入淡出，防拼接爆音。"""
    n = min(int(sr * ms / 1000), len(a) // 2)
    if n <= 0:
        return a
    a = a.copy()
    a[:n] *= np.linspace(0, 1, n, dtype=np.float32)
    a[-n:] *= np.linspace(1, 0, n, dtype=np.float32)
    return a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index-rate", type=float, default=0.3,
                    help="用哪个 index_rate 的 RVC 产物进对照包（默认 0.3，见 sweep_index_rate.py）")
    ap.add_argument("--outdir", default=OUT_DIR)
    ap.add_argument("--src-dir", default=SRC_DIR,
                    help="输入/A/B 产物目录（换模型时指向新目录，别再改源码常量）")
    args = ap.parse_args()
    src_dir = os.path.abspath(args.src_dir)
    sweep_dir = os.path.join(src_dir, "index_sweep")

    os.makedirs(args.outdir, exist_ok=True)
    ir_tag = f"ir{args.index_rate:g}"
    missing = []
    groups = []

    for i, text in TEXTS.items():
        a_path = os.path.join(src_dir, f"in_tts0{i}_40k.wav")
        b_path = os.path.join(sweep_dir, f"{ir_tag}_tts0{i}.wav.wav")
        if not os.path.exists(b_path):  # 扫描脚本曾产出带重复后缀的旧名，兼容两种
            b_path = os.path.join(sweep_dir, f"{ir_tag}_tts0{i}.wav")
        if not (os.path.exists(a_path) and os.path.exists(b_path)):
            missing.append((i, a_path, b_path))
            continue
        A, B = norm(ramp(load(a_path))), norm(ramp(load(b_path)))
        sf.write(os.path.join(args.outdir, f"A_qwen_zs_{i}.wav"), A, TARGET_SR)
        sf.write(os.path.join(args.outdir, f"B_rvc_{i}.wav"), B, TARGET_SR)
        groups.append((i, text, A, B))

    if missing:
        for i, a, b in missing:
            print(f"[missing] 第{i}条：{a if not os.path.exists(a) else b}")
    if not groups:
        print("[FATAL] 没有任何可比对的条目")
        return 1

    # 逐条 AB 拼接
    for i, text, A, B in groups:
        seq = np.concatenate([A, silence(GAP_WITHIN), B])
        sf.write(os.path.join(args.outdir, f"AB_compare_{i}.wav"), seq, TARGET_SR)

    # 全量串烧：A B | A B | A B，一次听完
    chunks = []
    for k, (i, text, A, B) in enumerate(groups):
        chunks.append(A)
        chunks.append(silence(GAP_WITHIN))
        chunks.append(B)
        if k != len(groups) - 1:
            chunks.append(silence(GAP_BETWEEN))
    allseq = np.concatenate(chunks)
    sf.write(os.path.join(args.outdir, "AB_all_in_one.wav"), allseq, TARGET_SR)

    # 真人原音过 RVC = 该模型音色上限（无 TTS 误差），单独放，供判断"瓶颈在哪"
    real = os.path.join(src_dir, "out_real.wav")
    if os.path.exists(real):
        sf.write(os.path.join(args.outdir, "C_rvc_ceiling_real.wav"),
                 norm(ramp(load(real))), TARGET_SR)

    manifest = dict(
        index_rate=args.index_rate,
        sample_rate=TARGET_SR,
        note="每条 AB_compare_i.wav = [Qwen零样本] 0.35s [RVC]；均统一峰值 0.85",
        items=[dict(idx=i, text=t,
                    a=f"A_qwen_zs_{i}.wav", b=f"B_rvc_{i}.wav",
                    ab=f"AB_compare_{i}.wav") for i, t, _, _ in groups],
        duration_s=float(len(allseq) / TARGET_SR),
    )
    with open(os.path.join(args.outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"输出目录：{args.outdir}")
    print(f"index_rate = {args.index_rate:g}，采样率 {TARGET_SR}，共 {len(groups)} 组")
    print(f"串烧总时长 {manifest['duration_s']:.1f}s")
    for f_ in sorted(os.listdir(args.outdir)):
        print("  ", f_)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
