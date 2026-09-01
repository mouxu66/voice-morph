# -*- coding: utf-8 -*-
"""固定源 A/B：同一段源音频 → 多个 RVC 音色 →（可选）人机感效果链。

存在的意义（遗留问题 #1）：
    「音色变来变去」的体感，根源是每次试听换了不同的源音频。本脚本把源固定死，
    只让音色做变量，一次产出全部候选，听感才能收敛到一条可验收的线上。

产物：
    outputs/ab_fixed/<标签>_<模型>_p<pitch>.wav          —— 干声（未加效果）
    outputs/ab_fixed/<标签>_<模型>_p<pitch>_fx_<档位>.wav —— 加效果链

用法：
    python tools/ab_fixed_source.py --src <源wav> [--models a,b,c] [--pitch 0]
                                    [--index-rate 0.5] [--fx] [--fx-model meituan_rat]
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
RVC_ROOT = Path(r"D:\RVC")
RVC_PY = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
INFER_PY = ROOT / "m2_server" / "offline_vc_infer.py"
OUT_DIR = ROOT / "outputs" / "ab_fixed"

# 人机感效果链三档：ffmpeg 原生滤镜（chorus/vibrato/aphaser/acrusher），无需额外依赖。
# 强度递增：轻=微颤+轻合唱，中=加相位，重=再加重金属激励与位深压碎。
#
# 每档都以 volume 先行衰减：chorus 会把多路延迟副本相加，aphaser/crystalizer 也会抬峰，
# 不预留余量必然削波（实测 mid 档曾炸到 RMS 0dB / 峰值 1.0）。
# 削波是链式内部的、不可逆的，所以必须在进滤镜前压，出滤镜后再靠 normalize 拉回听感电平。
FX_TIERS = {
    "light": "volume=0.45,"
             "vibrato=f=5.5:d=0.20,"
             "chorus=0.4:0.85:45|55:0.35|0.30:0.22|0.28:1.8|2.0,"
             "afade=in:st=0:d=0.02",
    "mid": "volume=0.30,"
           "vibrato=f=6.5:d=0.35,"
           "chorus=0.5:0.9:50|60|40:0.4|0.32|0.3:0.25|0.40|0.3:2|2.2|2.2,"
           "aphaser=in_gain=0.4:out_gain=0.5:delay=3:decay=0.4:speed=0.5:type=t,"
           "afade=in:st=0:d=0.02",
    "heavy": "volume=0.25,"
             "vibrato=f=8:d=0.5,"
             "chorus=0.6:0.95:55|65|45:0.5|0.4|0.35:0.3|0.45|0.35:2.2|2.5|2.5,"
             "aphaser=in_gain=0.4:out_gain=0.5:delay=3:decay=0.4:speed=0.6:type=t,"
             "crystalizer=i=4,acrusher=bits=6:mix=0.5:mode=lin:aa=1,"
             "afade=in:st=0:d=0.02",
}

TARGET_RMS_DB = -18.0   # 与 offline_vc_infer.py 的干声基准一致
PEAK_CEILING = 0.99     # 硬上限，保证转 int16 播放不溢出
SOFT_KNEE = 0.85        # 软限幅起始阈值：以下完全不动，以上平滑压缩到 PEAK_CEILING


def _run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", **kw)


def preprocess(src: Path, dst: Path) -> None:
    """统一转 16k 单声道——RVC 推理链路的入口要求。"""
    r = _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
              "-af", "aresample=16000", "-ac", "1", str(dst)])
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg 预处理失败: {(r.stderr or '').strip()[:300]}")


def resolve_model(model: str):
    """定位推理权重与检索 index；都缺失则报错。"""
    pth = RVC_ROOT / "assets" / "weights" / f"{model}.pth"
    if not pth.exists():
        raise FileNotFoundError(f"缺少推理权重: {pth}")
    index = next(iter(sorted((RVC_ROOT / "logs" / model).glob("added_*.index"))), None)
    return pth, index


def convert(model: str, src16k: Path, out: Path, pitch: int, index_rate: float) -> float:
    pth, index = resolve_model(model)
    t0 = time.time()
    r = _run([RVC_PY, INFER_PY,
              "--pth", pth,
              "--index", index if index else "",
              "--input", src16k, "--output", out,
              "--pitch", pitch, "--index-rate", index_rate],
             cwd=str(RVC_ROOT), timeout=1800)
    if r.returncode != 0 or not out.exists():
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"[{model}] RVC 推理失败: " + " | ".join(tail)[-400:])
    return time.time() - t0


def apply_fx(track: Path, out: Path, tier: str) -> None:
    r = _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(track),
              "-af", FX_TIERS[tier], "-ar", "48000", str(out)])
    if r.returncode != 0 or not out.exists():
        raise RuntimeError(f"[{tier}] 效果链失败: {(r.stderr or '').strip()[:300]}")


def normalize(path: Path, target_db: float = TARGET_RMS_DB) -> float:
    """把产物归一到同一响度，返回归一化后的峰值。

    A/B 试听最忌响度不齐：听感偏好会被「谁更响」带偏，而不是音色本身。
    干声/效果档统一到 -18 dBFS，峰值封顶防削波。
    """
    y, sr = sf.read(str(path), dtype="float32")
    if y.ndim > 1:
        y = y.mean(axis=1)
    # 防御：滤镜预热/编解码常在首样本留下尖峰，一个样本就能把峰值限制器触发、
    # 拖垮整条音轨的增益（实测导致 light 档被压低 10dB）。统一做 5ms 淡入根除。
    fade = min(len(y), int(sr * 0.005))
    if fade > 1:
        y[:fade] *= np.linspace(0.0, 1.0, fade, dtype="float32")
    rms = float(np.sqrt((y ** 2).mean()))
    if rms > 1e-9:
        y = y * (10 ** (target_db / 20.0)) / rms
    # 只压超阈的峰，不整体降增益。
    # 干声被下游硬削到 0.99（波峰因数低），效果档波峰更高；若用"整体除以峰值"，
    # 效果档会被连带压低 1~10dB，反而制造出新的响度偏差（这才是要消除的东西）。
    over = np.abs(y) > SOFT_KNEE
    if over.any():
        excess = np.abs(y[over]) - SOFT_KNEE
        y[over] = np.sign(y[over]) * (
            SOFT_KNEE + (PEAK_CEILING - SOFT_KNEE)
            * np.tanh(excess / (PEAK_CEILING - SOFT_KNEE)))
    sf.write(str(path), y.astype("float32"), sr)
    return float(np.abs(y).max())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="固定源音频（你自己的录音）")
    ap.add_argument("--models", default="meituan_rat,kangaroo,kangaroo_clean",
                    help="要对比的 RVC 模型，逗号分隔")
    ap.add_argument("--pitch", type=int, default=0)
    ap.add_argument("--index-rate", type=float, default=0.5)
    ap.add_argument("--tag", default="ab", help="产物文件名前缀")
    ap.add_argument("--fx", action="store_true", help="额外产出效果链三档")
    ap.add_argument("--fx-model", default="", help="对哪个模型加效果，默认取第一个")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        sys.exit(f"源音频不存在: {src}")
    if not RVC_PY.exists():
        sys.exit(f"RVC 环境缺失: {RVC_PY}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    src16k = OUT_DIR / "_src16k.wav"

    print(f"固定源: {src}")
    preprocess(src, src16k)
    print(f"预处理完成 -> {src16k}")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    results = []
    for m in models:
        out = OUT_DIR / f"{args.tag}_{m}_p{args.pitch}.wav"
        print(f"[干声] {m} …", end="", flush=True)
        try:
            el = convert(m, src16k, out, args.pitch, args.index_rate)
        except Exception as e:
            print(f" 失败\n    {e}")
            continue
        pk = normalize(out)
        print(f" {el:.1f}s -> {out.name}  (峰值 {pk:.3f})")
        results.append((m, out))

    if args.fx and results:
        base_model = args.fx_model or results[0][0]
        base = next((o for m, o in results if m == base_model), None)
        if base is None:
            print(f"警告: 未找到 {base_model} 的干声，跳过效果链")
        else:
            print(f"\n[效果链] 基于 {base_model}:")
            for tier in ("light", "mid", "heavy"):
                out = OUT_DIR / f"{args.tag}_{base_model}_p{args.pitch}_fx_{tier}.wav"
                try:
                    apply_fx(base, out, tier)
                    pk = normalize(out)
                    print(f"  {tier:6s} -> {out.name}  (峰值 {pk:.3f})")
                except Exception as e:
                    print(f"  {tier:6s} 失败: {e}")

    print(f"\n产物目录: {OUT_DIR}")
    print(f"全部已归一到 {TARGET_RMS_DB} dBFS，可直接盲听对比")


if __name__ == "__main__":
    main()
