# -*- coding: utf-8 -*-
"""受控实验：在生产训练路径（sft_8gb.py）上验证 lr 量纲假设。

背景（见 probe_ft_isolate.py / probe_ft_variation.py 的真 torch 实测）：
    sft_8gb.py 为省显存把官方 sft_12hz.py 的 AdamW 换成了 Adafactor，
    但**沿用了 AdamW 尺度的 lr=2e-5**。torch Adafactor 是相对步长算法：
        绝对步长 ≈ RMS(θ) · min(lr, 1/√t)
    ⇒ 实际步长 ≈ RMS(θ)·2e-5 ≈ 1e-6，比 bf16 的 ulp（≈2.4e-4）小两个数量级。

本脚本跑两条**只差一个变量（lr）**的真实训练臂，各 1 epoch / 同数据 / 同脚本：
    A 臂：lr=2e-5（生产现值）
    B 臂：lr=1e-2（torch Adafactor 文档默认值）

判据：每 epoch 的 loss 均值。生产历史基线是「10 epoch 只从 14.56 动到 14.51」。
用法：python probe_ft_lr_ab.py            # 顺序跑两臂
      python probe_ft_lr_ab.py --clean    # 跑完删掉产物目录
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(r"D:/变声")
SFT = ROOT / "tts_trial" / "sft_8gb.py"
VENV_PY = ROOT / "tts_trial" / "venv312" / "Scripts" / "python.exe"
JSONL = ROOT / "media" / "ft" / "pengyuyan" / "train_codes_short.jsonl"
ANCHOR = ROOT / "media" / "ft" / "pengyuyan" / "clips" / "seg_011.wav"
OUT_ROOT = ROOT / "tts_trial" / "ft_output" / "_probe_lr_ab"
LOGDIR = ROOT / "media" / "ft_work" / "ppt12" / "lr_ab"

ARMS = [("A_lr2e-5_production", "2e-5"), ("B_lr1e-2_adafactor_default", "1e-2")]


def run_arm(tag: str, lr: str, epochs: int = 1) -> dict:
    out = OUT_ROOT / tag
    out.mkdir(parents=True, exist_ok=True)
    log = LOGDIR / f"{tag}.log"
    LOGDIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(VENV_PY), "-u", str(SFT),
        "--train_jsonl", str(JSONL),
        "--num_epochs", str(epochs),
        "--lr", lr,
        "--speaker_name", "probe",
        "--output_model_path", str(out),
        "--anchor_ref", str(ANCHOR).replace("\\", "/"),
        "--keep_ckpts", "1",
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    print(f"\n[arm] {tag}  lr={lr}  epochs={epochs}")
    print(f"      log -> {log}")

    with open(log, "w", encoding="utf-8") as fh:
        fh.write(f"=== ARM {tag} lr={lr} ===\n")
        fh.flush()
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace")
            fh.write(line)
            fh.flush()
        rc = proc.wait()

    losses = [float(m.group(1)) for m in
              (re.search(r"loss=([\d.]+)", ln) for ln in
               log.read_text("utf-8", errors="replace").splitlines()) if m]
    # 用**线性回归斜率**当趋势判据：比"首尾两窗相减"稳得多（单步 loss 噪声很大，
    # 生产那 10 epoch 的均值波动只有 ±0.01 量级，首尾相差 0.05 已在噪声边缘）。
    slope = float("nan")
    if len(losses) >= 10:
        import numpy as np

        x = np.arange(len(losses), dtype=np.float64)
        slope = float(np.polyfit(x, np.asarray(losses, dtype=np.float64), 1)[0])
    first = sum(losses[:20]) / max(1, len(losses[:20])) if losses else float("nan")
    last = sum(losses[-20:]) / max(1, len(losses[-20:])) if losses else float("nan")
    return {"tag": tag, "lr": lr, "rc": rc, "n": len(losses),
            "first20": first, "last20": last, "slope": slope,
            "min": min(losses) if losses else float("nan")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="跑完删除产物目录")
    ap.add_argument("--epochs", type=int, default=1)
    args = ap.parse_args()

    for p in (JSONL, SFT, VENV_PY, ANCHOR):
        if not p.exists():
            raise SystemExit(f"缺文件：{p}")

    results = []
    for tag, lr in ARMS:
        results.append(run_arm(tag, lr, args.epochs))

    print("\n" + "=" * 78)
    print(f"受控实验结果（各 {args.epochs} epoch，唯一变量 = lr）")
    print("=" * 78)
    print(f"{'臂':<28} {'lr':>8} {'step数':>7} {'前20步均值':>12} {'后20步均值':>12} {'变化':>10} {'斜率/步':>11} {'rc':>4}")
    print("-" * 78)
    for r in results:
        d = r["last20"] - r["first20"]
        print(f"{r['tag']:<28} {r['lr']:>8} {r['n']:>7} {r['first20']:>12.4f} "
              f"{r['last20']:>12.4f} {d:>+10.4f} {r['slope']:>+11.6f} {r['rc']:>4}")

    print("\n[判读] A 臂（生产 lr）若几乎不动、B 臂（Adafactor 原生 lr）显著下降，")
    print("       即在真实训练路径上坐实「lr 量纲错配」这一根因。")
    print("       ★ 主判据看『斜率/步』：单步 loss 噪声很大，首尾两窗相减不稳；")
    print("         斜率约 −0.02/步 意味着 194 步能降 ~4；斜率 ≈0 即冻结。")
    print("       生产历史基线：10 epoch（1940 步）从 14.56 → 14.51（仅 −0.05，斜率 −2.6e-5）。")

    if args.clean:
        shutil.rmtree(OUT_ROOT, ignore_errors=True)
        print(f"\n[clean] 已删 {OUT_ROOT}")


if __name__ == "__main__":
    main()
