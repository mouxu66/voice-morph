# -*- coding: utf-8 -*-
"""跑 12 分钟版模型的全套转换（真人上限 + 3 条 TTS），供对照评测。

与 2026-10-12 第一版用的是**同一批输入 wav**（in_*），保证"只换模型"这一个变量。
"""
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

RVC_PY = r"D:\RVC\.venv\Scripts\python.exe"
INFER = r"D:\变声\m2_server\offline_vc_infer.py"
PTH = r"D:\RVC\assets\weights\myvoice_ppt12_full.pth"
INDEX = (r"D:\RVC\logs\myvoice_ppt12_full"
         r"\added_IVF817_Flat_nprobe_1_myvoice_ppt12_full_v2.index")
WORK = r"D:\变声\media\ft_work\ppt12\myvoice_rvc_full"

JOBS = [
    ("in_real_40k.wav", "out_real.wav"),
    ("in_tts01_40k.wav", "out_tts01.wav"),
    ("in_tts02_40k.wav", "out_tts02.wav"),
    ("in_tts03_40k.wav", "out_tts03.wav"),
]

for p in (PTH, INDEX):
    if not os.path.exists(p):
        sys.exit(f"[FATAL] 缺文件：{p}")
print(f"pth   = {PTH}")
print(f"index = {INDEX}")

for src, dst in JOBS:
    s = os.path.join(WORK, src)
    d = os.path.join(WORK, dst)
    if not os.path.exists(s):
        print(f"[skip] 缺输入 {src}")
        continue
    cmd = [RVC_PY, INFER, "--pth", PTH, "--index", INDEX,
           "--input", s, "--output", d, "--pitch", "0", "--index-rate", "0.5"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
    if r.returncode != 0:
        print(f"[FAIL] {src}\n{(r.stderr or '')[-900:]}")
        sys.exit(1)
    print(f"[ok] {src} -> {dst}  {tail[0]}")
print("\n全部完成")
