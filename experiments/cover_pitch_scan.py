# -*- coding: utf-8 -*-
"""**音域扫描**：把同一段歌声喂给模型时，变调到多高就开始坏？（在 RVC venv 里跑）

要回答用户的问题（2026-10-14）：
    「他们那种调唱得很高，那种普通人根本唱不来的那种」—— 我们能不能也这样？

★ 前半段的答案已经由 `cover_f0_transfer.py` 给出：**SVC 逐帧复制原唱的 F0 曲线**
（log_f0 相关 0.71、逐帧移位 p10–p90 只有 2.4 半音）⇒ **"唱得上去"不是目标音色的能力，
是原唱唱上去的**，我们只是把音色换掉。

那剩下的真问题是：**换成"我的音色"之后，这个音高还能不能保持住我的音色？**
模型是在 12 分钟**说话**素材（中位 ~105 Hz）上训的，而歌里有一半帧在它一个八度以上。
所以本脚本做一个**单变量扫描**：同一段歌声，只改 `pitch`（−12 … +12），量

    ① 对"你的参考音"的相似度（越高 = 越像你）
    ② 对"原唱这一段"的相似度（越高 = 越是泄漏原唱）
    ③ 谱质心与 4–8k（音色是否随变调变形）

⇒ 会得到一条**"像你 vs 撞原唱"的权衡曲线**，以及**模型在哪一档开始失守**。
这正是"我能不能用我的音色唱高音"的可复算答案。

跑法（必须 RVC venv）：
    D:/RVC/.venv/Scripts/python.exe experiments/cover_pitch_scan.py \
        --source media/covers/lorezo_feed_the_one/01_original_vocal.wav \
        --voice myvoice_ppt12_full --start 78 --seconds 20 --out media/covers/pitchscan
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

#: 变调档位（半音）。0 = 保持原调（用户听过的"混杂"那版）；负 = 往下走。
#: 正数那一侧刻意也扫 —— 用户想要"唱得高"，那就得知道**往上走会发生什么**。
PITCHES = [-12, -8, -5, 0, 4, 8, 12]


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()[:12]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="音域扫描：变调到多高模型开始失守")
    ap.add_argument("--source", required=True, type=Path, help="源歌声 wav（demucs vocals）")
    ap.add_argument("--voice", default="myvoice_ppt12_full")
    ap.add_argument("--index-rate", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--start", type=float, default=78.0)
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--pitches", default=",".join(str(p) for p in PITCHES))
    args = ap.parse_args()

    # ★ 先钉绝对路径：setup_env() 会 chdir 到 RVC 根（踩过两次）
    src = args.source.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    import numpy as np
    import soundfile as sf

    import offline_vc_infer as ov

    ov.setup_env()
    from infer.audio import load_audio

    sys.path.insert(0, str(ROOT / "experiments"))
    from cover_ckpt_sweep import resolve_pth          # 复用同一套权重解析

    pth = resolve_pth(args.voice)
    index = None
    rvc_root = Path(__import__("os").environ.get("VM_RVC_ROOT", r"D:\RVC"))
    log_dir = rvc_root / "logs" / args.voice
    if log_dir.exists():
        index = next(iter(sorted(log_dir.glob("added_*.index"))), None)

    print(f"[音域] 权重 {pth.name} · index={'有' if index else '无'} · "
          f"片段 {args.start:.0f}–{args.start + args.seconds:.0f}s")
    engine = ov.load_vc(str(pth), str(index) if index else "")
    print(f"[音域] 目标采样率 {engine.tgt_sr}Hz")

    full = load_audio(str(src), 16000)
    a, b = int(args.start * 16000), int((args.start + args.seconds) * 16000)
    clip = np.asarray(full[a:b], dtype=np.float32)

    # 原唱同段留 40k 参考（也是"撞原唱"那把尺子的参照）
    from scipy.signal import resample_poly

    ref40 = resample_poly(np.asarray(full[a:b], dtype=np.float64), 5, 2)
    sf.write(str(out / "00_source_40k.wav"), (ref40 * 0.7).astype(np.float32), 40000)

    # 量输入段的中位基频，用来标注"这一档落在目标音色的哪个音域位置"
    import librosa

    f0_in, _v, _p = librosa.pyin(np.asarray(full[a:b], dtype=np.float32), fmin=65, fmax=1000, sr=16000)
    med_in = float(np.nanmedian(f0_in)) if np.any(~np.isnan(f0_in)) else 0.0

    rows = []
    for p in [int(x) for x in args.pitches.split(",") if x.strip()]:
        t = time.time()
        y = ov.convert_audio(engine, clip.copy(), pitch=p,
                             index_rate=args.index_rate, seed=args.seed)
        dst = out / f"p{p:+03d}.wav"
        sf.write(str(dst), y, engine.tgt_sr)
        # 输出的中位 F0 = 输入中位 × 2^(p/12)（应当如此；量出来验证没跑偏）
        out_f0 = med_in * (2 ** (p / 12.0)) if med_in else 0.0
        rows.append({
            "pitch": p, "md5": _md5(dst), "wav": str(dst),
            "median_f0_hz_expected": round(out_f0, 1),
            "convert_s": round(time.time() - t, 1),
        })
        print(f"[音域] pitch {p:+3d} → 预期中位 {out_f0:6.1f}Hz · "
              f"{rows[-1]['convert_s']:5.1f}s · md5={rows[-1]['md5']}")

    report = {
        "source": str(src), "voice": args.voice, "seed": args.seed,
        "index_rate": args.index_rate, "start_s": args.start, "seconds": args.seconds,
        "source_median_f0_hz": round(med_in, 1),
        "tgt_sr": int(engine.tgt_sr),
        "rows": rows,
        "note": ("每档只改 pitch，其余全部相同；用 cover_metrics.py 打 CAM++（对你 / 对原唱）"
                 "与频段，得到『像你 vs 撞原唱』的权衡曲线"),
    }
    (out / "pitch_scan.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[音域] 报告 → {out / 'pitch_scan.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
