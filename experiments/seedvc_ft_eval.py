# -*- coding: utf-8 -*-
"""Seed-VC 自定义权重微调效果评估（2026-09-05 优化项②）。

同一源音频 + 袋鼠参考音，分别用【零样本默认权重】与【--ckpt 指定的微调 CFM 检查点】
跑 inference_v2.py；用 CAM++ 声纹上报 spk_sim_target（像袋鼠度）/ spk_sim_source（漏源度），
加 F0 表达力与时长。方法与 seedvc_param_sweep.py 一致（speaker_sep._sv_embed）。

用法:
    python experiments/seedvc_ft_eval.py                    # 零样本 + 微调 100 步档对比
    python experiments/seedvc_ft_eval.py --ckpt runs/kangaroo_ft_xxx/CFM_*.pth
    python experiments/seedvc_ft_eval.py --src <wav> --tgt <wav> --out-dir <dir>
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEEDVC_REPO = ROOT / "seed_vc_repo"
INFER = SEEDVC_REPO / "inference_v2.py"
PY = ROOT / ".venv" / "Scripts" / "python.exe"
M2 = ROOT / "m2_server"

DIFFUSION_STEPS = 10
SIM = 0.5            # 产品默认（sweep 实测最优）
CONVERT_STYLE = False  # 产品默认
TOP_P, TEMP = 0.9, 1.0


def convert(out_wav: Path, ckpt: Path | None) -> None:
    """零样本或带微调检查点跑一次推理；返回输出 wav 已搬移到 out_wav。"""
    tmp = out_wav.parent / f"_tmp{'' if ckpt is None else '_ft'}"
    tmp.mkdir(parents=True, exist_ok=True)
    cmd = [str(PY), str(INFER),
           "--source", str(ARGS.src), "--target", str(ARGS.tgt),
           "--output", str(tmp),
           "--diffusion-steps", str(DIFFUSION_STEPS),
           "--convert-style", "true" if CONVERT_STYLE else "false",
           "--similarity-cfg-rate", str(SIM),
           "--top-p", str(TOP_P),
           "--temperature", str(TEMP)]
    if ckpt is not None:
        cmd += ["--cfm-checkpoint-path", str(ckpt)]
    env = dict(os.environ)
    env["HF_ENDPOINT"] = "https://hf-mirror.com"
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                       encoding="utf-8", errors="replace", cwd=str(SEEDVC_REPO), env=env)
    tag = "zero-shot" if ckpt is None else f"ft:{Path(ckpt).parent.name}"
    wavs = list(tmp.glob("*.wav"))
    if r.returncode != 0 or not wavs:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-5:]
        raise RuntimeError(f"[{tag}] Seed-VC 失败: {' | '.join(tail)[-400:]}")
    shutil.move(str(wavs[0]), str(out_wav))
    try:
        tmp.rmdir()
    except OSError:
        pass


def cos(a, b) -> float:
    import numpy as np
    a, b = a / (np.linalg.norm(a) + 1e-9), b / (np.linalg.norm(b) + 1e-9)
    return float(np.dot(a, b))


def f0_stats(path: Path) -> dict:
    import numpy as np
    import librosa
    y, sr = librosa.load(str(path), sr=16000)
    f0, vflag, _ = librosa.pyin(y, fmin=60, fmax=400, sr=sr, frame_length=1024)
    f0 = f0[vflag]
    f0 = f0[~np.isnan(f0)]
    if len(f0) < 5:
        return {"f0_std": 0.0, "f0_range": 0.0, "voiced_ratio": 0.0}
    return {"f0_std": round(float(np.std(f0)), 2),
            "f0_range": round(float(np.percentile(f0, 95) - np.percentile(f0, 5)), 2),
            "voiced_ratio": round(float(vflag.mean()), 3)}


def main():
    sys.path.insert(0, str(M2))
    import speaker_sep
    import soundfile as sf

    emb_src = speaker_sep._sv_embed(speaker_sep._read16k(ARGS.src))
    emb_tgt = speaker_sep._sv_embed(speaker_sep._read16k(ARGS.tgt))

    rows = []
    ckpts = [None] + ([ARGS.ckpt] if ARGS.ckpt else [])
    for ckpt in ckpts:
        tag = "zero_shot" if ckpt is None else f"ft_{Path(ckpt).parent.name}"
        out_wav = ARGS.out_dir / f"{tag}.wav"
        if ckpt is None and ARGS.skip_zero_shot:
            print("跳过零样本组（--skip-zero-shot）", flush=True)
            continue
        if out_wav.exists():
            print(f"{tag} 已存在，跳过转换", flush=True)
        else:
            t0 = time.time()
            print(f"{tag} 转换中…", flush=True)
            convert(out_wav, ckpt)
            print(f"{tag} 完成，用时 {time.time() - t0:.0f}s", flush=True)
        emb = speaker_sep._sv_embed(speaker_sep._read16k(out_wav))
        d, sr = sf.read(str(out_wav))
        row = {"tag": tag, "ckpt": str(ckpt) if ckpt else "zero-shot(default)",
               "spk_sim_target": round(cos(emb, emb_tgt), 3),
               "spk_sim_source": round(cos(emb, emb_src), 3),
               **f0_stats(out_wav),
               "duration_s": round(len(d) / sr, 2),
               "file": str(out_wav)}
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    report = {
        "baselines": {"spk_sim_src_tgt": round(cos(emb_src, emb_tgt), 3), "f0_stats_src": f0_stats(ARGS.src)},
        "results": rows,
    }
    (ARGS.out_dir / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nOK -> {ARGS.out_dir / 'results.json'}", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--src", type=Path,
                   default=SEEDVC_REPO / "examples" / "source" / "jay_0.wav")
    p.add_argument("--tgt", type=Path,
                   default=ROOT / "media" / "voicebank" / "kangaroo" / "reference.wav")
    p.add_argument("--ckpt", type=Path, default=None)
    p.add_argument("--out-dir", type=Path, default=ROOT / "outputs" / "seedvc_ft_eval")
    p.add_argument("--skip-zero-shot", action="store_true")
    ARGS = p.parse_args()
    ARGS.out_dir.mkdir(parents=True, exist_ok=True)
    main()