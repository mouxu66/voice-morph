# -*- coding: utf-8 -*-
"""index_rate 扫描：验证社区「降 Search Feature Ratio 可消瑕疵」在本模型上是否成立。

RVC 推理时 index_rate 控制 FAISS 检索特征与模型输出特征的混合比例：
  高 → 更像训练集（音色更准）但可能带检索噪声/电音；
  低 → 更平滑干净但音色可能漂移。
社区（RVC 官方 FAQ / 中文教程）对「电音、哑音、杂音」的处方是降到 0.5 左右；
本机 `offline_vc_infer.py` 默认 0.5，但**没有针对本模型实测过**。

本脚本对同一批输入跑多个 index_rate，输出 CAM++ / ASR / peak，用数据决定取值。

⚠️ 推理随机种子：默认由 `offline_vc_infer.INFER_SEED` 固定（见其注释）——
不固定时同一命令两次的 CAM++ 差可达 0.02，比本表的全部极差还大（2026-10-13 实测）。

用法（必须项目 `.venv`：本脚本自己要 `faster_whisper` + `modelscope`(CAM++) + `zhconv`；
子进程里那个 `offline_vc_infer.py` 由脚本自己用 `D:\\RVC\\.venv` 的 python 拉起）：
    .venv\\Scripts\\python.exe tts_trial\\sweep_index_rate.py
"""
import argparse
import json
import os
import subprocess
import sys

import numpy as np

sys.path.insert(0, r"D:\变声\m2_server")
sys.path.insert(0, r"D:\变声\tts_trial")
sys.stdout.reconfigure(encoding="utf-8")

from ab_evaluate import cam_emb  # noqa: E402
from asr_cer import accuracy as asr_accuracy  # noqa: E402
from offline_vc_infer import INFER_SEED as DEFAULT_SEED  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402
import soundfile as sf  # noqa: E402

RVC_PY = r"D:\RVC\.venv\Scripts\python.exe"
INFER = r"D:\变声\m2_server\offline_vc_infer.py"
#: 默认模型/索引/工作目录。换模型（如 12 分钟版 myvoice_ppt12_full）时用 CLI 覆盖，
#: 别再改源码 —— 2026-10-12 就因为这里写死路径，新模型扫不了。
PTH = r"D:\RVC\assets\weights\myvoice_ppt12.pth"
INDEX = r"D:\RVC\logs\myvoice_ppt12\added_IVF263_Flat_nprobe_1_myvoice_ppt12_v2.index"
WORK = r"D:\变声\media\ft_work\ppt12\myvoice_rvc"
REF = r"D:\变声\media\ft_work\ppt12\ref_24k.wav"

TEXTS = {
    "in_tts01_40k.wav": "这一节我们讲三个概念，先看第一个。",
    "in_tts02_40k.wav": "把步骤拆开，一步一步做就不会乱。",
    "in_tts03_40k.wav": "注意这里的条件，符号写反了结论就完全不同。",
}


def cos(x, y):
    d = float(np.linalg.norm(x)) * float(np.linalg.norm(y))
    return float(np.dot(x, y) / d) if d > 0 else float("nan")


def emb(p):
    v = cam_emb(p)
    return None if v is None else np.asarray(v, dtype=np.float32).reshape(-1)


def overlap(a, b):
    """可懂度（1 - CER）。见 asr_cer 模块头：字符集 Jaccard 会被标点污染。"""
    return asr_accuracy(b, a)


def out_name(rate, src):
    """统一产物名：ir0.3_tts01.wav。

    修 bug：早期版本 `src.replace('_40k','')` 未去掉 `.wav`，产出 `ir0.3_tts01.wav.wav`
    （双后缀，工具链里看着像坏文件）。
    """
    stem = os.path.splitext(src)[0].replace("in_", "").replace("_40k", "")
    return f"ir{rate:g}_{stem}.wav"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rates", default="0.0,0.3,0.5,0.75,1.0")
    ap.add_argument("--pitch", type=int, default=0)
    ap.add_argument("--outdir", default=os.path.join(WORK, "index_sweep"))
    ap.add_argument("--seed", type=int, default=None,
                    help="RVC 推理随机种子；不传则用 offline_vc_infer 的固定默认值")
    ap.add_argument("--pth", default=PTH, help="RVC 推理权重（.pth）")
    ap.add_argument("--index", default=INDEX, help="FAISS 索引（.index）")
    ap.add_argument("--work", default=WORK, help="输入 wav 所在目录")
    ap.add_argument("--ref", default=REF, help="参考音（算 CAM++ 用）")
    args = ap.parse_args()
    rates = [float(x) for x in args.rates.split(",") if x.strip()]
    outdir = os.path.abspath(args.outdir)
    work = os.path.abspath(args.work)
    os.makedirs(outdir, exist_ok=True)
    seed = args.seed if args.seed is not None else DEFAULT_SEED
    # ★ 这个 seed 必须固定（2026-10-13）：RVC v2 的 NSF 声源带两个未播种随机项
    # （`infer/module/models.py` 的 `torch.rand` 谐波初相 + `torch.randn_like` 噪声激励），
    # 不固定时**每次转换都是新抽样** —— 实测同一命令两次 CAM++ 差 0.0212，
    # 比本表任何两个 rate 之间的差还大，即整张表会被随机性吃掉。
    print(f"RVC 推理 seed = {seed}（固定 ⇒ 各 rate 的差异只来自检索混合比例）")

    # 1) 转换（每个 rate × 每条输入）
    jobs = []
    for r in rates:
        for src in TEXTS:
            dst = os.path.join(outdir, out_name(r, src))
            jobs.append((r, src, dst))
    for r, src, dst in jobs:
        if os.path.exists(dst):
            continue
        # 兼容旧版双后缀产物，避免重复跑推理
        legacy = dst + ".wav"
        if os.path.exists(legacy):
            os.replace(legacy, dst)
            print(f"[cache] ir={r:g} {os.path.basename(dst)}")
            continue
        cmd = [RVC_PY, INFER, "--pth", args.pth, "--index", args.index,
               "--input", os.path.join(work, src), "--output", dst,
               "--pitch", str(args.pitch), "--index-rate", str(r)]
        if args.seed is not None:
            cmd += ["--seed", str(args.seed)]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if p.returncode != 0:
            print(f"[FAIL] ir={r} {src}\n{(p.stderr or '')[-800:]}")
            return 1
        print(f"[ok] ir={r:g} {src} -> {os.path.basename(dst)}")

    # 2) 打分
    ref = emb(args.ref)
    wm = WhisperModel("small", device="cuda", compute_type="float16")
    print(f"\n{'index_rate':<12}{'CAM++':>9}{'ASR':>8}{'peak':>8}{'削波%':>8}{'RMS dB':>9}")
    print("-" * 54)
    table = {}
    for r in rates:
        sims, ovs, peaks, clips, rmss = [], [], [], [], []
        for src in TEXTS:
            p = os.path.join(outdir, out_name(r, src))
            sims.append(cos(emb(p), ref))
            segs, _ = wm.transcribe(p, language="zh", vad_filter=False)
            ovs.append(overlap("".join(s.text for s in segs).replace(" ", ""), TEXTS[src]))
            a, sr = sf.read(p, dtype="float32")
            if a.ndim > 1:
                a = a.mean(axis=1)
            peaks.append(float(np.max(np.abs(a))))
            clips.append(float(np.mean(np.abs(a) >= 0.999)))
            rms = float(np.sqrt(np.mean(a ** 2)))
            rmss.append(20 * np.log10(rms) if rms > 0 else -99)
        table[r] = dict(campp=float(np.mean(sims)), asr=float(np.mean(ovs)),
                        peak=float(np.mean(peaks)), clip=float(np.mean(clips)),
                        rms_db=float(np.mean(rmss)))
        t = table[r]
        print(f"{r:<12g}{t['campp']:>9.4f}{t['asr']:>8.3f}{t['peak']:>8.3f}"
              f"{t['clip'] * 100:>8.2f}{t['rms_db']:>9.1f}")
    print("-" * 54)

    best = max(rates, key=lambda r: (table[r]["campp"], table[r]["asr"]))
    print(f"CAM++ 最优 index_rate = {best:g}（{table[best]['campp']:.4f}）")
    with open(os.path.join(outdir, "sweep.json"), "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in table.items()}, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
