# -*- coding: utf-8 -*-
"""翻唱用的 checkpoint 扫描：**同一段歌声、只换权重**（在 RVC venv 里跑）。

为什么还值得做（2026-10-13 已扫过一次 epoch，但那是**说话**路径）
    那次结论是「e10 就已到位，e10–e40 只涨 +0.0036 < seed 噪声」，用的是
    `tts_trial/ab_ft_vs_rvc_detail.py` 的 TTS→RVC 链路（素材是说话句）。
    ★ 而翻唱是**跨域**输入（歌声 → 说话模型），#175 已经证明"模型在域内忠实、
    跨域才掉高频"。域内结论**不能直接搬到跨域**：跨域时不同 epoch 的泛化差异
    有可能被放大（也可能同样分辨不出 —— 那也是一个值得钉住的结论）。

    另一个没扫过的维度：**训练素材时长**（4min / 8min / 12min，本机都已训好）。
    #175 的天花板测试指向"素材的频谱倾斜被封进模型"，那么"更短的素材是否更糟"
    是个可测的推论。

做法：不走产品链路（那要 demucs + 整首），只把**同一段歌声**喂给每个 checkpoint，
产物落盘后用 `experiments/cover_metrics.py` 打客观分（CAM++/频段/质心）。
这样单变量、可复算，且与 `cover_knobs.py` 同一套口径。

跑法（必须 RVC venv）：
    D:/RVC/.venv/Scripts/python.exe experiments/cover_ckpt_sweep.py \
        --source media/covers/lorezo_feed_the_one/01_original_vocal.wav \
        --pitch -12 --start 78 --seconds 20 --out media/covers/ckpt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 扫描表：**explicit 名单**而不是"扫 assets 下所有 myvoice*"。
#: 理由：资产目录里还有 4min/8min/last4min 等**别的数据集**训出来的模型，
#: 混在一起扫就变成"权重 × 数据集"两个变量同时动，结论没法归因。
#: 这里按「同一个训练集内的 epoch」与「同超参下的素材时长」分两组，各自单变量。
GROUPS: dict[str, list[str]] = {
    # A 组：12 分钟全集内的 epoch（单变量 = 训练轮数）
    "epochs_12min": [
        "myvoice_ppt12_full_e10_s990",
        "myvoice_ppt12_full_e20_s1980",
        "myvoice_ppt12_full_e30_s2970",
        "myvoice_ppt12_full_e40_s3960",
        "myvoice_ppt12_full",
    ],
    # B 组：同超参、不同素材时长（单变量 = 素材量）—— 每个都用它的 e40（最长训练档）
    "dur_4_8_12min": [
        "myvoice_probe_4min_e40_s1360",
        "myvoice_probe_8min_e40_s2560",
        "myvoice_ppt12_full_e40_s3960",
    ],
}


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()[:12]


def resolve_pth(voice: str) -> Path:
    """按产品口径找权重：`logs/<voice>/` 优先，其次 `assets/weights/`。

    ⚠️ **只找 .pth，不找 index**：本扫描要单变量。index 是检索特征库，
    不同实验的 index 来自不同数据集 —— 带上它就把"权重差异"与"检索库差异"
    混在一个数里了（且 #167 已证 index 对音色影响与 seed 噪声同量级）。
    所以这里**统一 index_rate=0**，只看模型本身。
    """
    import os

    rvc = Path(os.environ.get("VM_RVC_ROOT", r"D:\RVC"))
    for cand in (rvc / "logs" / voice / f"{voice}.pth",
                 rvc / "assets" / "weights" / f"{voice}.pth"):
        if cand.exists():
            return cand
    raise SystemExit(f"找不到权重 {voice}.pth")

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="翻唱 checkpoint 扫描（同一段歌声，只换权重）")
    ap.add_argument("--source", required=True, type=Path, help="源歌声 wav（demucs vocals）")
    ap.add_argument("--pitch", type=int, default=-12)
    ap.add_argument("--start", type=float, default=78.0)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--groups", default="epochs_12min,dur_4_8_12min",
                    help="要扫哪几组（逗号分隔，键见 GROUPS）")
    args = ap.parse_args()

    # ★ 先钉绝对路径：`setup_env()` 会 chdir 到 RVC 根（cover_knobs.py 踩过两次）
    src = args.source.resolve()
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    import numpy as np
    import soundfile as sf

    import offline_vc_infer as ov

    ov.setup_env()
    from infer.audio import load_audio

    full = load_audio(str(src), 16000)
    a, b = int(args.start * 16000), int((args.start + args.seconds) * 16000)
    clip = np.asarray(full[a:b], dtype=np.float32)

    # 原唱同段留一份 40k 参考（页面里当"原唱"听；客观分也要它当参照系）
    from scipy.signal import resample_poly

    ref40 = resample_poly(np.asarray(full[a:b], dtype=np.float64), 5, 2)
    sf.write(str(out_dir / "00_source_40k.wav"), (ref40 * 0.7).astype(np.float32), 40000)

    wanted = [g.strip() for g in args.groups.split(",") if g.strip()]
    rows: list[dict] = []
    print(f"[ckpt] 片段 {args.start:.0f}–{args.start + args.seconds:.0f}s · "
          f"变调 {args.pitch:+d} · index_rate=0（刻意不带 index，见 resolve_pth）")
    for group in wanted:
        names = GROUPS.get(group)
        if not names:
            print(f"[ckpt] 跳过未知组 {group}")
            continue
        for voice in names:
            try:
                pth = resolve_pth(voice)
            except SystemExit as e:
                print(f"[ckpt] 跳过 {voice}：{e}")
                continue
            t = time.time()
            engine = ov.load_vc(str(pth), "")   # ← index 传空串 = 不检索
            y = ov.convert_audio(engine, clip.copy(), pitch=args.pitch,
                                 index_rate=0.0, seed=args.seed)
            dst = out_dir / f"{group}__{voice}.wav"
            sf.write(str(dst), y, engine.tgt_sr)
            rows.append({
                "group": group, "voice": voice, "pth": str(pth),
                "tgt_sr": int(engine.tgt_sr), "md5": _md5(dst),
                "seconds": round(len(y) / engine.tgt_sr, 2),
                "convert_s": round(time.time() - t, 1), "wav": str(dst),
            })
            print(f"[ckpt] {group:14s} {voice:34s} {rows[-1]['convert_s']:5.1f}s "
                  f"md5={rows[-1]['md5']} → {dst.name}")

    uniq: dict[str, list[str]] = {}
    for r in rows:
        uniq.setdefault(r["md5"], []).append(r["voice"])
    same = {h: n for h, n in uniq.items() if len(n) > 1}

    report = {
        "source": str(src), "pitch": args.pitch, "seed": args.seed,
        "start_s": args.start, "seconds": args.seconds,
        "index_rate": 0.0,
        "note": "index_rate 刻意固定为 0：本扫描只让**权重**变，不让检索库跟着变",
        "rows": rows, "identical_groups": same,
    }
    (out_dir / "ckpt_sweep.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ckpt] 报告 → {out_dir / 'ckpt_sweep.json'}")
    if same:
        for h, names in same.items():
            print(f"[ckpt] ⚠️ 这几个权重输出**逐字节相同**：{names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
