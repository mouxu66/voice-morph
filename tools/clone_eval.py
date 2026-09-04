# -*- coding: utf-8 -*-
"""克隆音色客观评测：相似度(SECS) + 自然度(NatScore) + 能量/时长硬指标 -> 评分 CSV。

设计要点（与项目现有设施对齐，不重复造轮子）：
    - 说话人相似度 SECS：复用 m2_server worker 8001 的 /emb 接口（底层即 CAM++，
      项目已有，无需本地再加载）。worker 未启动则 SECS 列留空并提示。
    - 自然度 NatScore：本地重建的 natscore_local 包（tools/natscore_local），
      读取已下载的 models/natscore/final.pt + 已缓存的 openai/whisper-small 编码器，
      完全离线、不依赖被墙的 git/pip 安装。
    - 能量/时长：soundfile + numpy，抓电音/崩坏/语速异常。

用法：
    # worker 8001 已启动（与离线变声/级联页同一服务）
    python tools/clone_eval.py --ref media/voicebank/kangaroo/reference.wav \
        --candidates outputs/ab_test --out outputs/ab_test/score.csv

    # 多个文件
    python tools/clone_eval.py --ref ref.wav --candidates a.wav b.wav c.wav --out score.csv

    # 只评自然度/能量（不依赖 worker，纯离线）
    python tools/clone_eval.py --no-secs --candidates outs/ --out s.csv

权重默认相似度/自然度各 0.5；夸张音色（如袋鼠）可调低 --w-sim（它本就不是"同一个人"目标）。
"""
import argparse
import csv
import json
import math
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKER = "http://127.0.0.1:8001"
LOCAL_FINAL_PT = ROOT / "models" / "natscore" / "final.pt"
LOCAL_WHISPER = ROOT / "models" / "whisper-small"

sys.path.insert(0, str(ROOT / "tools"))  # 让 natscore_local 可被 import


def _post_json(url: str, payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get_emb(path: Path) -> list:
    d = _post_json(WORKER + "/emb", {"path": str(path)}, timeout=300)
    if d.get("error") or not d.get("emb"):
        raise RuntimeError(d.get("error") or "empty emb")
    return d["emb"]


def _cosine(a, b) -> float:
    import numpy as np
    a, b = np.asarray(a, float), np.asarray(b, float)
    n = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / n) if n > 0 else 0.0


def _to_16k_mono(src: Path, dst: Path) -> Path:
    """用 ffmpeg 统一转 16k 单声道（NatScore 的 whisper 编码器要求 16k）。
    避免依赖 librosa；ffmpeg 已在项目中用于同类预处理。"""
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ar", "16000", "-ac", "1", str(dst)],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg 16k 转换失败: {r.stderr.strip()[:200]}")
    return dst


def _wav_stats(path: Path):
    import numpy as np
    import soundfile as sf

    d, sr = sf.read(str(path))
    if d.ndim > 1:
        d = d.mean(-1)
    dur = len(d) / sr
    rms = float(np.sqrt(np.mean(d ** 2))) if len(d) else 0.0
    peak = float(np.abs(d).max()) if len(d) else 0.0
    dbfs = 20.0 * math.log10(peak) if peak > 0 else -120.0
    return dur, rms, dbfs


def _norm01(x, lo, hi) -> float:
    try:
        return max(0.0, min(1.0, (float(x) - lo) / (hi - lo)))
    except Exception:
        return 0.0


def main():
    p = argparse.ArgumentParser(description="克隆音色客观评测（SECS+NatScore+能量）")
    p.add_argument("--ref", help="原声/目标音色参考 wav（--no-secs 时可省略）")
    p.add_argument("--candidates", required=True, nargs="+",
                   help="候选 wav 文件或目录（目录则递归取 *.wav）")
    p.add_argument("--out", default="clone_score.csv")
    p.add_argument("--w-sim", type=float, default=0.5, help="相似度权重")
    p.add_argument("--w-nat", type=float, default=0.5, help="自然度权重")
    p.add_argument("--no-secs", action="store_true", help="跳过 SECS（不依赖 worker）")
    args = p.parse_args()

    if not args.no_secs and not args.ref:
        p.error("--ref 在启用 SECS 时为必填（或加 --no-secs 仅评自然度）")

    # 收集候选
    cands: list[Path] = []
    for c in args.candidates:
        cp = Path(c)
        if cp.is_dir():
            cands += sorted(cp.rglob("*.wav"))
        else:
            cands.append(cp)
    cands = [c for c in cands if c.suffix.lower() == ".wav"]
    if not cands:
        print("没有候选 wav，退出")
        sys.exit(1)

    # NatScore scorer（本地 final.pt + 缓存的 whisper-small 编码器，离线）
    print("[nat] loading NatScore（本地 final.pt + whisper-small 编码器）…", flush=True)
    from natscore_local import load_local
    if not LOCAL_FINAL_PT.exists():
        print(f"[nat] 缺少 {LOCAL_FINAL_PT}，请先下载 NatScore 权重", flush=True)
        sys.exit(1)
    try:
        scorer = load_local(str(LOCAL_FINAL_PT), encoder_model_name=str(LOCAL_WHISPER))
    except Exception as e:
        print(f"[nat] NatScore 加载失败: {e}", flush=True)
        sys.exit(1)

    tmp = Path(tempfile.mkdtemp(prefix="clone_eval_"))

    def nat_of(path: Path) -> float:
        w16 = tmp / (path.stem + ".16k.wav")
        _to_16k_mono(path, w16)
        return float(scorer.score(str(w16)))

    # SECS：原声嵌入
    secs_ref = None
    if not args.no_secs:
        try:
            secs_ref = _get_emb(Path(args.ref))
            print("[secs] 已取原声嵌入（worker 8001 / CAM++）", flush=True)
        except Exception as e:
            print(f"[secs] 警告：原声嵌入失败（worker 未启动？）：{e}", flush=True)
            secs_ref = None

    rows = []
    for i, c in enumerate(cands, 1):
        row = {"file": c.name, "secs": "", "natscore": "",
               "rms": "", "dbfs": "", "dur_s": "", "score": "", "rank": ""}
        try:
            dur, rms, dbfs = _wav_stats(c)
            row["dur_s"] = round(dur, 3)
            row["rms"] = round(rms, 4)
            row["dbfs"] = round(dbfs, 1)
        except Exception as e:
            print(f"  [{c.name}] 读取失败: {e}", flush=True)
        if secs_ref is not None:
            try:
                row["secs"] = round(_cosine(secs_ref, _get_emb(c)), 4)
            except Exception as e:
                print(f"  [{c.name}] SECS 失败: {e}", flush=True)
        try:
            row["natscore"] = round(nat_of(c), 3)
        except Exception as e:
            print(f"  [{c.name}] NatScore 失败: {e}", flush=True)
        rows.append(row)
        print(f"  {i}/{len(cands)} {c.name}  secs={row['secs']}  nat={row['natscore']}  "
              f"rms={row['rms']}  dbfs={row['dbfs']}  dur={row['dur_s']}s", flush=True)

    # 加权总分 + 排名：用批内 min-max 归一化（不假设量程，仅用于排序）。
    # SECS 是余弦相似度(越高越像原声)，NatScore 是 Bradley-Terry logit(越高越自然)。
    secs_vals = [r["secs"] for r in rows if r["secs"] != ""]
    nat_vals = [r["natscore"] for r in rows if r["natscore"] != ""]

    def _minmax(v, vals):
        if not vals:
            return 0.0
        lo, hi = min(vals), max(vals)
        return 0.0 if hi == lo else (v - lo) / (hi - lo)

    for r in rows:
        s, parts = 0.0, 0
        if r["secs"] != "":
            s += args.w_sim * _minmax(r["secs"], secs_vals); parts += 1
        if r["natscore"] != "":
            s += args.w_nat * _minmax(r["natscore"], nat_vals); parts += 1
        r["score"] = round(s, 4) if parts else ""
    ranked = sorted([r for r in rows if r["score"] != ""], key=lambda r: -r["score"])
    for i, r in enumerate(ranked, 1):
        r["rank"] = i

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cols = ["rank", "file", "score", "secs", "natscore", "rms", "dbfs", "dur_s"]
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r["rank"] or 999)):
            w.writerow({k: r.get(k, "") for k in cols})
    print(f"\n完成：{out}（{len(rows)} 条，加权 w_sim={args.w_sim}/w_nat={args.w_nat}）")


if __name__ == "__main__":
    main()
