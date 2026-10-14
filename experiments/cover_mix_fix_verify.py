# -*- coding: utf-8 -*-
"""端到端核验：产品链路 `cover_api.mix_back` 防削波（#174 修复）。

为什么要有这个脚本（不只是单测）：单测用的是替身，证明的是"argv 里有没有 pcm_f32le"。
而 #174 的教训恰恰是**合成信号上的结论 ≠ 真实产物上的事实** —— 所以这里拿
**真实素材**（demucs 分离出的 vocals / no_vocals）跑**产品自己的那个函数**，
把修前/修后的成品都量出来。

★ 判据（三条，全部要过）：
  1. 修后的成品 **一个削波点都没有**（≥0.99 的采样数 = 0），峰值 ≤ MIX_CEILING+容差；
  2. **修前的行为确实会削波** —— 用同一份素材按"不缩放"跑一遍（前置对照），
     否则"修好了"可能只是因为这份素材本来就不过顶（§ 前置断言纪律）；
  3. 两侧增益**配比保持**（等比缩放），不是"随便压小"。

用法（主 venv）：
    .venv/Scripts/python.exe experiments/cover_mix_fix_verify.py \
        --song media/songs/lorezo_feed_the_one.mp3 --out media/covers/mixfix
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

import cover_api  # noqa: E402
from common import find_ffmpeg  # noqa: E402


def measure(path: Path) -> dict:
    d, sr = sf.read(str(path), dtype="float64")
    a = np.abs(d)
    return {
        "peak": round(float(a.max()), 4),
        "clipped": int((a >= 0.99).sum()),
        "samples": int(d.shape[0]),
        "sr": int(sr),
        "dur_s": round(d.shape[0] / sr, 1),
    }


def to_wav(src: Path, dst: Path) -> None:
    cmd = [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
           "-ar", "44100", "-ac", "2", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError("解码失败：" + (r.stderr or "")[:400])


def mix_unscaled(vocal: Path, accomp: Path, gains: tuple[float, float], dst: Path) -> None:
    """**修前的行为**：一遍混音、直接写 pcm_s16（不量峰、不缩放）。

    复刻的是 `cover_api.mix_back` 改动前那一版 —— 用来做前置对照：
    它必须真的削波，否则这份素材证明不了任何事。
    """
    vg, ag = gains
    cmd = [
        find_ffmpeg(), "-y", "-loglevel", "error",
        "-i", str(vocal), "-i", str(accomp),
        "-filter_complex",
        f"[0:a]volume={vg}[v];[1:a]volume={ag}[a];"
        f"[v][a]amix=inputs=2:duration=longest:normalize=0",
        "-ar", "44100", "-ac", "2", str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError("对照混音失败：" + (r.stderr or "")[:400])


def main() -> int:
    ap = argparse.ArgumentParser(description="核验产品 mix_back 的防削波")
    ap.add_argument("--song", required=True, type=Path)
    ap.add_argument("--voice", default="myvoice_ppt12_full", help="RVC 实验名")
    ap.add_argument("--pitch", type=int, default=-12)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--skip-convert", action="store_true",
                    help="不重跑 RVC，直接用 --reuse-vocal")
    ap.add_argument("--reuse-vocal", type=Path, default=None)
    args = ap.parse_args()

    song = args.song if args.song.is_absolute() else ROOT / args.song
    out = args.out if args.out.is_absolute() else ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"song": str(song), "voice": args.voice, "pitch": args.pitch}

    # ① 解码 + ② 分离（用**产品自己的** separate_song，产物落 out 下的会话样式目录）
    wav = out / "_song_44k.wav"
    t = time.time()
    to_wav(song, wav)
    vocals, accomp = cover_api.separate_song(wav, int(time.time() * 1000))
    report["separate_s"] = round(time.time() - t, 1)
    report["stems"] = {"vocals": str(vocals), "accomp": str(accomp)}

    # ③ 换声（或用现成的）
    if args.skip_convert and args.reuse_vocal:
        rvc_out = args.reuse_vocal if args.reuse_vocal.is_absolute() else ROOT / args.reuse_vocal
    else:
        import rvc_convert
        t = time.time()
        rvc_out = rvc_convert.rvc_convert(vocals, args.voice, pitch=args.pitch, index_rate=0.5)
        report["convert_s"] = round(time.time() - t, 1)
    report["rvc_out"] = str(rvc_out)

    # ④ 自动配平（产品口径）—— 刻意让增益顶到夹紧上限附近，复现 #174 的现场
    vocal_db, accomp_db = cover_api.rms_db(rvc_out), cover_api.rms_db(accomp)
    gains = (cover_api.auto_vocal_gain(vocal_db, accomp_db), 1.0)
    report["rms_db"] = {"vocal": round(vocal_db, 2) if vocal_db else None,
                        "accomp": round(accomp_db, 2) if accomp_db else None}
    report["auto_gain"] = list(gains)

    # ⑤ 前置对照：**修前的行为**（一遍 s16、不缩放）
    t = time.time()
    before = out / "before_unscaled.wav"
    mix_unscaled(rvc_out, accomp, gains, before)
    report["before"] = measure(before)
    report["before_mix_s"] = round(time.time() - t, 1)

    # ⑥ 产品修复后的行为
    t = time.time()
    after = out / "after_product.wav"
    _, applied = cover_api.mix_back(rvc_out, accomp, after, gains=gains)
    report["after"] = measure(after)
    report["after_mix_s"] = round(time.time() - t, 1)
    report["gains_applied"] = list(applied)
    report["probe_cleaned"] = not any("probe" in p.name for p in out.iterdir())

    # ⑦ 判据
    ok = True
    b, a = report["before"], report["after"]
    report["assertions"] = []

    def check(name: str, cond: bool, detail: str) -> None:
        nonlocal ok
        report["assertions"].append({"name": name, "pass": bool(cond), "detail": detail})
        if not cond:
            ok = False

    check("前置：不缩放确实削波", b["clipped"] > 0,
          f"削波点 {b['clipped']} / {b['samples']}（{b['clipped']/max(b['samples'],1)*100:.3f}%），峰值 {b['peak']}")
    check("修复后零削波", a["clipped"] == 0, f"削波点 {a['clipped']}，峰值 {a['peak']}")
    check("修复后峰值不超上限", a["peak"] <= cover_api.MIX_CEILING + 5e-3,
          f"峰值 {a['peak']} ≤ {cover_api.MIX_CEILING}")
    check("配比保持（等比缩放）",
          abs(applied[0] / applied[1] - gains[0] / gains[1]) < 1e-3,
          f"{gains} → {applied}")
    check("探测文件已清理", report["probe_cleaned"], "会话目录里没有 .probe.f32.wav")

    report["ok"] = ok
    (out / "verify.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n=== 结论 ===")
    for it in report["assertions"]:
        print(f"  [{'PASS' if it['pass'] else 'FAIL'}] {it['name']} —— {it['detail']}")
    print("全部通过" if ok else "有未通过项")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
