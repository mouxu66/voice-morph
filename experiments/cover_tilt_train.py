# -*- coding: utf-8 -*-
"""把训练切片**按频谱倾斜预加重**，看能不能治"闷"（不需要用户重录）。

为什么值得试（2026-10-14，m1g 的结论 + 一条算术）
    `m1g` 把"闷"归到**训练素材的频谱倾斜**：模型把自己的 16kHz 前端能看到的
    4–8kHz 那一段形状**原样搬进输出**。而 RVC 的前端瓶颈是 **8kHz**
    （`load_audio(src, 16000)`）—— 关键点：**4–8kHz 在瓶颈之内**，
    所以"把这一带在训练集里抬起来"是**模型看得见**的改动，不是去改它看不见的东西。

    这不是"用户重录"的替代品（重录能拿到真正的高频，预加重只是改斜率），
    但它回答一个便宜的问题：**"闷"有多少来自可调的倾斜、多少来自缺失的内容**。

做法（单变量）
    一份数据集 → 生成 N 个"倾斜档"（对 2kHz 以上做不同斜率的抬升）→ 各自训同等 epoch
    → 用同一段歌声过一遍 → 量 4–8k 与谱质心（与 `cover_metrics.py` 同口径）。

★ 幂等与安全：每档写进**独立目录**（`<out>/ds_<tag>`），互不覆盖；
  源数据目录只读。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

RVC = Path("D:/RVC")

#: 倾斜档：(标签, 说明, 每倍频程抬多少 dB @2kHz 以上)。
#: 0 = 原样（对照臂）；正数 = 越往高频抬得越多。
TILTS: list[tuple[str, str, float]] = [
    ("t00", "原样（对照臂）", 0.0),
    ("t03", "2kHz 以上每倍频 +3dB", 3.0),
    ("t06", "2kHz 以上每倍频 +6dB", 6.0),
]

#: 预加重的起点：2kHz。理由：语音主能量在 0–2kHz，"闷"的抱怨针对的是
#: 2k 以上（清晰度/临场感）。从 2k 起抬不会把基频也抬上去（那会改变音高感）。
TILT_PIVOT_HZ = 2000.0


def preemphasize(x: np.ndarray, sr: int, db_per_octave: float) -> np.ndarray:
    """按频谱倾斜整形：`TILT_PIVOT_HZ` 以上每倍频程抬 `db_per_octave` dB。

    实现走 **STFT 逐频点增益**（而不是一阶差分/IIR）：斜率要精确可控、
    且不能引入相位失真以外的时域染色。增益曲线在 pivot 以下是 1.0。
    """
    if db_per_octave == 0.0:
        return x
    n = 2048
    hop = n // 4
    win = np.hanning(n).astype(np.float32)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    with np.errstate(divide="ignore"):
        octaves = np.log2(np.maximum(freqs, 1.0) / TILT_PIVOT_HZ)
    gain = np.where(freqs >= TILT_PIVOT_HZ, 10 ** (db_per_octave * octaves / 20.0), 1.0)
    out = np.zeros(len(x) + n, dtype=np.float64)
    wsum = np.zeros(len(x) + n, dtype=np.float64)
    for i in range(0, max(len(x) - n, 1), hop):
        seg = x[i:i + n]
        if len(seg) < n:
            seg = np.pad(seg, (0, n - len(seg)))
        spec = np.fft.rfft(seg * win)
        y = np.fft.irfft(spec * gain, n=n) * win
        out[i:i + n] += y
        wsum[i:i + n] += win ** 2
    out = out[:len(x)] / np.maximum(wsum[:len(x)], 1e-6)
    return out.astype(np.float32)


def build_dataset(src: Path, dst: Path, db_per_octave: float) -> int:
    """把 `src` 下所有 wav 预加重后写进 `dst`（幂等：先清空 dst）。"""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in sorted(src.glob("*.wav")):
        d, sr = sf.read(str(p), dtype="float32", always_2d=True)
        mono = d.mean(1)
        y = preemphasize(mono, sr, db_per_octave)
        peak = float(np.abs(y).max())
        if peak > 0.99:                      # 抬完可能过顶 —— 等比缩，别硬削
            y = y * (0.99 / peak)
        sf.write(str(dst / p.name), y, sr, subtype="PCM_16")
        n += 1
    return n


def train(exp: str, dataset: Path, epochs: int, log: Path) -> None:
    py = RVC / ".venv" / "Scripts" / "python.exe"
    cmd = [str(py), str(RVC / "train_rvc_voice.py"),
           "--exp", exp, "--dataset", str(dataset), "--epochs", str(epochs)]
    print(f"[训练] {exp} ← {dataset.name}（{epochs} epoch）…")
    with log.open("a", encoding="utf-8") as f:
        f.write(f"\n=== {exp} START {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        r = subprocess.run(cmd, cwd=str(RVC), stdout=f, stderr=subprocess.STDOUT,
                           text=True, timeout=14400)
    if r.returncode != 0:
        raise RuntimeError(f"{exp} 训练失败（rc={r.returncode}，见 {log}）")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="训练集频谱倾斜实验（治闷，不需重录）")
    ap.add_argument("--src", type=Path, required=True, help="源数据集（40k wav 目录）")
    ap.add_argument("--out", type=Path, required=True, help="产物根目录")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--tilts", default=",".join(t[0] for t in TILTS))
    ap.add_argument("--dry-run", action="store_true", help="只造数据集，不训练")
    args = ap.parse_args()

    src = (args.src if args.src.is_absolute() else ROOT / args.src).resolve()
    out = (args.out if args.out.is_absolute() else ROOT / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    wanted = {t.strip() for t in args.tilts.split(",") if t.strip()}
    picks = [t for t in TILTS if t[0] in wanted]
    if not picks:
        print(f"[!] --tilts 没匹配到任何档：{sorted(wanted)}")
        return 2

    log = out / "train_run.log"
    report: dict = {"src": str(src), "epochs": args.epochs, "tilts": {}}

    for tag, why, slope in picks:
        ds = out / f"ds_{tag}"
        n = build_dataset(src, ds, slope)
        d, sr = sf.read(str(sorted(ds.glob("*.wav"))[0]), dtype="float32", always_2d=True)
        report["tilts"][tag] = {"why": why, "db_per_octave": slope, "clips": n,
                                "dataset": str(ds), "sr": int(sr)}
        print(f"[数据] {tag} {why} → {n} 条 @{sr}Hz")
        if args.dry_run:
            print(f"[训练] （dry-run 跳过）")
            continue
        exp = f"myvoice_tilt_{tag}"
        train(exp, ds, args.epochs, log)
        report["tilts"][tag]["exp"] = exp
        report["tilts"][tag]["pth"] = str(RVC / "assets" / "weights" / f"{exp}.pth")

    (out / "tilt_sweep.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out / 'tilt_sweep.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
