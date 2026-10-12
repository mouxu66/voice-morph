# -*- coding: utf-8 -*-
"""跑某个 RVC 模型的全套转换（真人上限 + 3 条 TTS），供对照评测。

与第一版用的是**同一批输入 wav**（`--work` 下的 `in_*`），保证「只换模型」这一个变量。

⚠️ 参数化而非写死路径：2026-10-12 就因为这脚本写死新模型路径，老模型（3.4 分钟版）
想用同一口径复测只能手敲命令 —— 而「两个模型各跑一次」正是本脚本存在的意义。

用法（默认 = 12 分钟版）：
    .venv\\Scripts\\python.exe tts_trial\\rvc_convert_full.py
老模型（3.4 分钟版，2026-10-13 补跑口径）：
    .venv\\Scripts\\python.exe tts_trial\\rvc_convert_full.py \\
        --pth D:\\RVC\\assets\\weights\\myvoice_ppt12.pth \\
        --index D:\\RVC\\logs\\myvoice_ppt12\\added_IVF263_Flat_nprobe_1_myvoice_ppt12_v2.index \\
        --work media\\ft_work\\ppt12\\myvoice_rvc

★ 随机种子：默认不传 `--seed`，由 `offline_vc_infer` 用固定默认值（`INFER_SEED = 1234`）。
RVC v2 的 NSF 声源带两个未播种随机项（SineGen 的谐波初相 + 噪声激励），不固定时
同一输入**每次结果都不同**（实测同一命令两次 CAM++ 差 0.0212，比 ir 扫描表全部极差
0.0039 还大）。所以两个模型必须用同一套「固定 seed + 同一批输入」的口径，比值才有意义。
"""
import argparse
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

RVC_PY = r"D:\RVC\.venv\Scripts\python.exe"
INFER = r"D:\变声\m2_server\offline_vc_infer.py"
DEFAULT_PTH = r"D:\RVC\assets\weights\myvoice_ppt12_full.pth"
DEFAULT_INDEX = (
    r"D:\RVC\logs\myvoice_ppt12_full"
    r"\added_IVF817_Flat_nprobe_1_myvoice_ppt12_full_v2.index"
)
DEFAULT_WORK = r"D:\变声\media\ft_work\ppt12\myvoice_rvc_full"

JOBS = [
    ("in_real_40k.wav", "out_real.wav"),
    ("in_tts01_40k.wav", "out_tts01.wav"),
    ("in_tts02_40k.wav", "out_tts02.wav"),
    ("in_tts03_40k.wav", "out_tts03.wav"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pth", default=DEFAULT_PTH, help="RVC 推理权重（.pth）")
    ap.add_argument("--index", default=DEFAULT_INDEX, help="FAISS 索引（.index）")
    ap.add_argument("--work", default=DEFAULT_WORK, help="输入/输出目录（含 in_*.wav）")
    ap.add_argument("--pitch", type=int, default=0, help="变调半音数")
    ap.add_argument("--index-rate", type=float, default=0.5)
    ap.add_argument(
        "--seed",
        type=int,
        default=None,
        help="不传则用 offline_vc_infer 的固定默认 seed（保证可复现）",
    )
    args = ap.parse_args()

    work = os.path.abspath(args.work)
    for p in (args.pth, args.index):
        if not os.path.exists(p):
            print(f"[FATAL] 缺文件：{p}")
            return 1
    print(f"pth    = {args.pth}")
    print(f"index  = {args.index}")
    print(f"work   = {work}")
    print(f"seed   = {args.seed if args.seed is not None else '默认（offline_vc_infer.INFER_SEED）'}")

    for src, dst in JOBS:
        s = os.path.join(work, src)
        d = os.path.join(work, dst)
        if not os.path.exists(s):
            print(f"[skip] 缺输入 {src}")
            continue
        cmd = [
            RVC_PY, INFER,
            "--pth", args.pth, "--index", args.index,
            "--input", s, "--output", d,
            "--pitch", str(args.pitch), "--index-rate", str(args.index_rate),
        ]
        if args.seed is not None:
            cmd += ["--seed", str(args.seed)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
        if r.returncode != 0:
            print(f"[FAIL] {src}\n{(r.stderr or '')[-900:]}")
            return 1
        print(f"[ok] {src} -> {dst}  {tail[0]}")
    print("\n全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
