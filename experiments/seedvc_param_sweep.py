# -*- coding: utf-8 -*-
"""Seed-VC 表达力参数扫描（P1-2 调参对比）。

固定源音频与目标参考音，扫描 convert_style / similarity_cfg_rate / top_p / temperature，
每组跑一次 Seed-VC V2，产出：
  - outputs/seedvc_sweep/<tag>.wav           各组转换结果（可直接 A/B 试听）
  - outputs/seedvc_sweep/results.json        全部指标
  - outputs/seedvc_sweep/report.md           对比报告（含结论建议）

指标：
  - spk_sim_target：输出 vs 目标参考音 的 CAM++ 声纹余弦相似度（音色像不像）
  - spk_sim_source：输出 vs 源音频 的声纹相似度（音色是否漏源）
  - f0_std / f0_range：基频标准差 / 活动范围（表达力代理——RVC 压平韵律时显著偏低）
  - duration：输出时长

跑法：在项目根 .venv 下  python experiments/seedvc_param_sweep.py
"""
import json
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

SRC = SEEDVC_REPO / "examples" / "source" / "jay_0.wav"
TGT = SEEDVC_REPO / "examples" / "reference" / "trump_0.wav"
OUT_DIR = ROOT / "outputs" / "seedvc_sweep"

DIFFUSION_STEPS = 10
CONFIGS = [
    dict(tag="plain",        convert_style=False, sim=0.7, top_p=0.9, temp=1.0),
    dict(tag="style_sim03",  convert_style=True,  sim=0.3, top_p=0.9, temp=1.0),
    dict(tag="style_sim05",  convert_style=True,  sim=0.5, top_p=0.9, temp=1.0),
    dict(tag="style_sim07",  convert_style=True,  sim=0.7, top_p=0.9, temp=1.0),
    dict(tag="style_sim09",  convert_style=True,  sim=0.9, top_p=0.9, temp=1.0),
    dict(tag="style_topp05", convert_style=True,  sim=0.7, top_p=0.5, temp=1.0),
    dict(tag="style_temp14", convert_style=True,  sim=0.7, top_p=0.9, temp=1.4),
]


def convert(cfg: dict, out_wav: Path) -> None:
    """跑一次 inference_v2.py 子进程（权重已缓存）。"""
    tmp = OUT_DIR / f"_tmp_{cfg['tag']}"
    tmp.mkdir(parents=True, exist_ok=True)
    cmd = [str(PY), str(INFER),
           "--source", str(SRC), "--target", str(TGT), "--output", str(tmp),
           "--diffusion-steps", str(DIFFUSION_STEPS),
           "--convert-style", "true" if cfg["convert_style"] else "false",
           "--similarity-cfg-rate", str(cfg["sim"]),
           "--top-p", str(cfg["top_p"]),
           "--temperature", str(cfg["temp"])]
    import os
    env = dict(os.environ)
    env["HF_ENDPOINT"] = "https://hf-mirror.com"
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800,
                       encoding="utf-8", errors="replace", cwd=str(SEEDVC_REPO), env=env)
    wavs = list(tmp.glob("*.wav"))
    if r.returncode != 0 or not wavs:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-5:]
        raise RuntimeError(f"[{cfg['tag']}] Seed-VC 失败: {' | '.join(tail)[-400:]}")
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
    """基频表达力代理：有声段 F0 的 std 与 p5–p95 活动范围（Hz）。"""
    import numpy as np
    import librosa
    y, sr = librosa.load(str(path), sr=16000)
    f0, vflag, _ = librosa.pyin(y, fmin=60, fmax=400, sr=sr, frame_length=1024)
    f0 = f0[vflag]
    f0 = f0[~np.isnan(f0)]
    if len(f0) < 5:
        return {"f0_std": 0.0, "f0_range": 0.0, "voiced_ratio": 0.0}
    return {
        "f0_std": round(float(np.std(f0)), 2),
        "f0_range": round(float(np.percentile(f0, 95) - np.percentile(f0, 5)), 2),
        "voiced_ratio": round(float(vflag.mean()), 3),
    }


def parse_args():
    """可选 CLI：不传参时行为与原来一致（jay_0 → trump_0 参数扫描）。

    传参即可拿来做项目内素材的 A/B，例如：
        python experiments/seedvc_param_sweep.py ^
            --source outputs/enhance_ab/xxx_standard.wav ^
            --target media/voicebank/kangaroo/reference.wav ^
            --out-dir outputs/seedvc_e2e_ab ^
            --configs "plain:0:0.7:0.9:1.0,style_sim05:1:0.5:0.9:1.0"
        --configs 每项格式：tag:convert_style(0/1):sim:top_p:temperature
    """
    import argparse
    ap = argparse.ArgumentParser(description="Seed-VC 表达力参数扫描 / A/B")
    ap.add_argument("--source", default="")
    ap.add_argument("--target", default="")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--configs", default="",
                    help="逗号分隔：tag:style(0/1):sim:top_p:temp")
    args = ap.parse_args()
    return args


def main():
    global SRC, TGT, OUT_DIR, CONFIGS
    args = parse_args()
    if args.source:
        SRC = Path(args.source)
    if args.target:
        TGT = Path(args.target)
    if args.out_dir:
        OUT_DIR = Path(args.out_dir)
    if args.configs:
        cfgs = []
        for item in args.configs.split(","):
            parts = item.strip().split(":")
            if len(parts) != 5:
                raise SystemExit(f"--configs 格式错误: {item}")
            tag, style, sim, top_p, temp = parts
            cfgs.append(dict(tag=tag, convert_style=(style == "1"),
                             sim=float(sim), top_p=float(top_p), temp=float(temp)))
        CONFIGS = cfgs
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(M2))
    import speaker_sep  # CAM++ 声纹（与切片质检/说话人分离同一实现）
    import numpy as np
    import soundfile as sf

    print("=== baselines ===", flush=True)
    emb_src = speaker_sep._sv_embed(speaker_sep._read16k(SRC))
    emb_tgt = speaker_sep._sv_embed(speaker_sep._read16k(TGT))
    base = {
        "source": {"spk_sim_target": round(cos(emb_src, emb_tgt), 3), **f0_stats(SRC)},
        "target_ref": {"f0_stats": f0_stats(TGT)},
    }
    print(json.dumps(base, ensure_ascii=False), flush=True)

    results = []
    for i, cfg in enumerate(CONFIGS, 1):
        tag = cfg["tag"]
        out_wav = OUT_DIR / f"{tag}.wav"
        if out_wav.exists():
            print(f"[{i}/{len(CONFIGS)}] {tag} 已存在，跳过", flush=True)
        else:
            t0 = time.time()
            print(f"[{i}/{len(CONFIGS)}] {tag} 转换中…", flush=True)
            convert(cfg, out_wav)
            print(f"[{i}/{len(CONFIGS)}] {tag} 完成，用时 {time.time()-t0:.0f}s", flush=True)

        emb = speaker_sep._sv_embed(speaker_sep._read16k(out_wav))
        d, sr = sf.read(str(out_wav))
        row = {
            "tag": tag,
            "convert_style": cfg["convert_style"],
            "similarity_cfg_rate": cfg["sim"],
            "top_p": cfg["top_p"],
            "temperature": cfg["temp"],
            "spk_sim_target": round(cos(emb, emb_tgt), 3),
            "spk_sim_source": round(cos(emb, emb_src), 3),
            **f0_stats(out_wav),
            "duration_s": round(len(d) / sr, 2),
            "file": str(out_wav),
        }
        results.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    report = build_report(base, results)
    (OUT_DIR / "results.json").write_text(
        json.dumps({"baselines": base, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / "report.md").write_text(report, encoding="utf-8")
    print("\n" + report, flush=True)
    print(f"\nOK -> {OUT_DIR / 'report.md'}", flush=True)


def build_report(base: dict, rows: list[dict]) -> str:
    src_f0 = base["source"]
    lines = [
        "# Seed-VC 表达力参数扫描报告",
        "",
        f"- 源：`{SRC.name}`  目标参考：`{TGT.name}`  diffusion_steps={DIFFUSION_STEPS}",
        f"- 源 vs 参考音 声纹相似度（越低说明换得越干净）：{base['source']['spk_sim_target']}",
        f"- 源 F0 表达力基线：std={src_f0['f0_std']}Hz，range={src_f0['f0_range']}Hz",
        "",
        "| 配置 | style | sim | top_p | temp | 像目标↑ | 漏源↓ | F0 std | F0 range | 时长 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['tag']} | {'开' if r['convert_style'] else '关'} | {r['similarity_cfg_rate']} "
            f"| {r['top_p']} | {r['temperature']} | {r['spk_sim_target']} | {r['spk_sim_source']} "
            f"| {r['f0_std']} | {r['f0_range']} | {r['duration_s']}s |")
    lines += [
        "",
        "## 怎么读",
        "- **像目标**：与参考音的 CAM++ 声纹余弦相似度，越高音色越贴。",
        "- **漏源**：与源说话人的相似度，越低说明源音色残留越少。",
        "- **F0 std / range**：韵律起伏代理。RVC 压平韵律时这两项明显掉；对比源基线看表达力保住/放大了多少。",
        f"- 主观听感以 `{OUT_DIR}` 下各 wav 为准，建议按 "
    f"{' → '.join(r['tag'] for r in rows)} 顺序盲听。",
    ]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
