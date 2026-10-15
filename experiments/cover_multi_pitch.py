# -*- coding: utf-8 -*-
"""把一首歌的人声**一次分离、多个变调档**换成我的音色 —— 出可直接听的成品。

为什么另写一个而不是改 `cover_my_voice.py`（2026-10-14）：
    · `cover_my_voice.py` 一次只出一个变调档，而"该用哪一档"正是这次要回答的问题；
      跑两遍会**各做一次 demucs 分离**，而分离本身带随机性（实测同模型两次产物
      不是逐字节相同）⇒ 两版之间就多了一个无关变量，A/B 不再单变量。
    · 本脚本：**分离一次**，之后每个变调档共用同一对人声/伴奏轨与同一个 seed ⇒
      两版之间**只有 pitch 不同**（同族纪律：A/B 包只许有一列不同）。

复用而不是复写：
    · 换声  `rvc_convert.rvc_convert`（常驻 worker，模型缓存）
    · 配平  `cover_api.auto_vocal_gain` / `rms_db`
    · 合回  `cover_api.mix_back`（内含防削波，见 #174）
    · 基频  `cover_api._median_f0`

用法：
    .venv/Scripts/python.exe experiments/cover_multi_pitch.py \
        --vocals media/covers/song2/01_vocals.wav \
        --accomp media/covers/song2/02_accomp.wav \
        --voice myvoice_tilt_t03 \
        --ref media/ft_work/ppt12/ref_24k.wav \
        --pitches -12,0 --out media/covers/song2/mypitch
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import numpy as np  # noqa: E402

import cover_api  # noqa: E402
import rvc_convert  # noqa: E402
from common import find_ffmpeg  # noqa: E402


def median_f0(path: Path) -> float | None:
    """中位基频（复用产品口径；算不出返回 None）。"""
    try:
        import librosa

        return float(cover_api._median_f0(path, np, librosa))
    except Exception as e:  # noqa: BLE001
        print(f"[!] 量基频失败 {path.name}: {e}")
        return None


def suggest_pitch(f0_song: float | None, f0_voice: float | None, max_shift: int = 12) -> int:
    """把歌声音域对齐到目标音色 —— 夹在 ±max_shift（同 `cover_my_voice` 的口径）。"""
    import math

    if not f0_song or not f0_voice:
        return 0
    st = 12 * math.log2(f0_voice / f0_song)
    return int(max(-max_shift, min(max_shift, round(st))))


def mp3(src: Path, dst: Path, start: float, seconds: float) -> bool:
    cmd = [find_ffmpeg(), "-y", "-loglevel", "error",
           "-ss", f"{start:.2f}", "-t", f"{seconds:.2f}", "-i", str(src),
           "-ac", "2", "-ar", "44100", "-b:a", "192k", str(dst)]
    r = __import__("subprocess").run(cmd, capture_output=True, text=True,
                                    timeout=900, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not dst.exists():
        print(f"[!] mp3 失败 {src.name}: {(r.stderr or '')[:200]}")
        return False
    return True


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="一次分离、多个变调档换声（单变量 A/B）")
    ap.add_argument("--vocals", required=True, type=Path, help="已分离的人声轨")
    ap.add_argument("--accomp", required=True, type=Path, help="已分离的伴奏轨")
    ap.add_argument("--voice", default="myvoice_tilt_t03", help="RVC 实验名")
    ap.add_argument("--ref", type=Path, default=None, help="目标音色参考音（算建议变调）")
    ap.add_argument("--pitches", default="-12,0", help="要出的变调档（逗号分隔）")
    ap.add_argument("--index-rate", type=float, default=0.5)
    ap.add_argument("--clip-start", type=float, default=0.0, help="试听片段起点（秒）")
    ap.add_argument("--clip-seconds", type=float, default=45.0)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    vocals = args.vocals if args.vocals.is_absolute() else ROOT / args.vocals
    accomp = args.accomp if args.accomp.is_absolute() else ROOT / args.accomp
    out = args.out if args.out.is_absolute() else ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    ref = (args.ref if args.ref and args.ref.is_absolute() else
           (ROOT / args.ref if args.ref else None))
    if not vocals.exists() or not accomp.exists():
        print(f"[!] 缺分离轨：{vocals} / {accomp}")
        return 2

    f0_song = median_f0(vocals)
    f0_voice = median_f0(ref) if ref and ref.exists() else None
    sug = suggest_pitch(f0_song, f0_voice)
    print(f"[音域] 歌声中位 {f0_song and round(f0_song, 1)} Hz · "
          f"我的参考音中位 {f0_voice and round(f0_voice, 1)} Hz ⇒ 建议变调 {sug:+d} 半音")

    pitches = [int(x) for x in args.pitches.split(",") if x.strip()]
    log: dict = {"vocals": str(vocals), "accomp": str(accomp), "voice": args.voice,
                 "f0_song": f0_song, "f0_voice": f0_voice, "suggested_pitch": sug,
                 "index_rate": args.index_rate, "variants": []}

    for p in pitches:
        t = time.time()
        rvc_out = Path(rvc_convert.rvc_convert(vocals, args.voice, pitch=p,
                                               index_rate=args.index_rate))
        conv_s = round(time.time() - t, 1)
        my_vocal = out / f"vocal_p{p:+03d}.wav"
        shutil.copy2(rvc_out, my_vocal)

        # 实测 RMS 配平 + 防削波合回（复用产品函数）
        vg = cover_api.auto_vocal_gain(cover_api.rms_db(my_vocal), cover_api.rms_db(accomp))
        full = out / f"full_p{p:+03d}.wav"
        _, applied = cover_api.mix_back(my_vocal, accomp, full, gains=(vg, 1.0))

        # 量成品实际音高（验证变调真的按预期落地 —— 别只信参数）
        f0_out = median_f0(full)
        row = {"pitch": p, "vocal": str(my_vocal), "full": str(full),
               "convert_s": conv_s, "vocal_gain": vg,
               "gains_applied": [round(x, 4) for x in applied],
               "f0_full_median_hz": f0_out and round(f0_out, 1)}
        mp3(my_vocal, out / f"vocal_p{p:+03d}.mp3", args.clip_start, args.clip_seconds)
        mp3(full, out / f"full_p{p:+03d}.mp3", args.clip_start, args.clip_seconds)
        log["variants"].append(row)
        print(f"[换声] pitch {p:+3d}: 变调 {conv_s}s · 人声 RMS 配平 ×{applied[0]:.2f}"
              f" · 成品中位 F0 {row['f0_full_median_hz']} Hz")

    # 原唱同段也出一份 mp3 当对照（A/B 的"原"）
    mp3(vocals, out / "orig_vocal.mp3", args.clip_start, args.clip_seconds)

    (out / "multi_pitch.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[完成] 产物在 {out}")
    for f in sorted(out.glob("*.mp3")):
        print(f"        {f.name}  {f.stat().st_size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
