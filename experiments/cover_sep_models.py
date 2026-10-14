# -*- coding: utf-8 -*-
"""分离模型对照：`htdemucs` vs `htdemucs_ft`（同一条歌，同一段）。

为什么要测（2026-10-14，`m1g` 待办第三条）
    `m1g` 已把「闷/混杂」归到**素材**与**跨域**上，但还留了一个没排除的候选：
    **demucs 分离出来的人声里混着多少伴奏残留**。若人声轨本身带伴奏，
    那"混杂"就有一部分是分离器贡献的 —— 而这是**换模型就能改**的一条。

口径（三条，必须一起看）
    ① **伴奏残留**：人声轨在"原唱没唱"的时段里还有多少能量（真伴奏泄漏的直接证据）。
       取整首的**低能量帧**比较（人声安静处，理想的分离器应该几乎全静音）。
    ② **人声完整度**：分离前后在"人声占主导"的帧上，两者差多少 dB
       （掉太多 = 把该有的人声也削掉了，那是另一种坏）。
    ③ **听感对照**：两版人声都留 mp3，与换声结果放同一张试听页 —— 数字只做初筛。

跑法（主 venv，demucs 在这里；模型已在本地缓存，全程离线）：
    .venv/Scripts/python.exe experiments/cover_sep_models.py \
        --song media/songs/lorezo_feed_the_one.mp3 --out media/covers/sepmodels
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

from common import find_ffmpeg  # noqa: E402

#: 要对照的模型名（demucs 的 `-n` 参数）。`htdemucs_ft` 是官方微调版，
#: 官方自述"可能更好、慢约 4 倍" —— 慢多少也在报告里量出来。
MODELS = ["htdemucs", "htdemucs_ft"]


def decode_to_wav(src: Path, dst: Path) -> None:
    cmd = [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
           "-ar", "44100", "-ac", "2", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError("解码失败：" + (r.stderr or "")[:400])


def run_demucs(wav: Path, model: str, out_root: Path) -> tuple[Path, Path, float]:
    """跑一次分离，返回 (vocals, no_vocals, 耗时)。"""
    t = time.time()
    cmd = [sys.executable, "-m", "demucs", "-n", model, "--two-stems", "vocals",
           "-o", str(out_root), str(wav)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                       encoding="utf-8", errors="replace")
    took = time.time() - t
    stem = out_root / model / wav.stem
    vocals, accomp = stem / "vocals.wav", stem / "no_vocals.wav"
    if r.returncode != 0 or not vocals.exists() or not accomp.exists():
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise RuntimeError(f"{model} 分离失败：" + " | ".join(tail)[-400:])
    return vocals, accomp, took


def frame_db(path: Path, n: int = 1024, hop: int = 512) -> np.ndarray:
    """逐帧 RMS（dBFS）—— 用来找"人声安静处"与"人声主导处"。"""
    d, sr = sf.read(str(path), dtype="float64", always_2d=True)
    mono = d.mean(1)
    frames = [mono[i:i + n] for i in range(0, len(mono) - n, hop)]
    rms = np.array([np.sqrt(np.mean(f ** 2)) for f in frames])
    return 20 * np.log10(rms + 1e-12)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="demucs 分离模型对照（人声干净度）")
    ap.add_argument("--song", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--models", default=",".join(MODELS))
    args = ap.parse_args()

    song = (args.song if args.song.is_absolute() else ROOT / args.song).resolve()
    out = (args.out if args.out.is_absolute() else ROOT / args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    wav = out / "_song_44k.wav"
    decode_to_wav(song, wav)

    report: dict = {"song": str(song), "models": {}}
    vocals_by_model: dict[str, Path] = {}
    for m in models:
        print(f"[分离] 跑 {m} …")
        v, a, took = run_demucs(wav, m, out)
        vocals_by_model[m] = v
        vdb, adb = frame_db(v), frame_db(a)
        report["models"][m] = {
            "separate_s": round(took, 1),
            "vocals": str(v), "accomp": str(a),
            "vocals_rms_dbfs": round(float(vdb.mean()), 2),
            "accomp_rms_dbfs": round(float(adb.mean()), 2),
            "vocals_peak": round(float(np.abs(sf.read(str(v), dtype="float64")[0]).max()), 4),
        }
        print(f"[分离] {m}: {took:.1f}s · 人声 RMS {report['models'][m]['vocals_rms_dbfs']}dBFS")

    # ---- 判据①：伴奏残留（在人声最安静的那批帧上，人声轨还有多响）----
    # 用**最保守**的模型（htdemucs）的人声轨当"哪里没人唱"的参照系：
    # 它在那些帧上已经很安静，若另一个模型在同一批帧上更响，就是**相对更多的残留**。
    ref = vocals_by_model[models[0]]
    ref_db = frame_db(ref)
    quiet = ref_db <= np.percentile(ref_db, 15)   # 最安静的 15% 帧
    loud = ref_db >= np.percentile(ref_db, 70)    # 人声主导帧

    for m in models:
        db = frame_db(vocals_by_model[m])
        report["models"][m].update({
            "quiet_frame_dbfs": round(float(db[quiet].mean()), 2),
            "loud_frame_dbfs": round(float(db[loud].mean()), 2),
        })
    base = report["models"][models[0]]
    for m in models[1:]:
        cur = report["models"][m]
        cur["accomp_leak_vs_base_db"] = round(cur["quiet_frame_dbfs"] - base["quiet_frame_dbfs"], 2)
        cur["vocal_keep_vs_base_db"] = round(cur["loud_frame_dbfs"] - base["loud_frame_dbfs"], 2)
        cur["slowdown_x"] = round(cur["separate_s"] / base["separate_s"], 1)

    # ---- 出 mp3 供人耳对照（数字只做初筛）----
    for m, v in vocals_by_model.items():
        mp3 = out / f"vocals_{m}.mp3"
        subprocess.run([find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(v),
                        "-ac", "1", "-ar", "44100", "-b:a", "128k", str(mp3)], check=False)
        report["models"][m]["mp3"] = str(mp3) if mp3.exists() else None

    (out / "sep_models.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 结果 ===")
    print(f"{'模型':<16}{'耗时s':>8}{'残留(安静帧)':>14}{'人声(主导帧)':>14}{'慢':>6}")
    for m in models:
        r = report["models"][m]
        print(f"{m:<16}{r['separate_s']:>8.1f}{r['quiet_frame_dbfs']:>13.2f}dB"
              f"{r['loud_frame_dbfs']:>13.2f}dB"
              + (f"{r.get('slowdown_x', 1.0):>5.1f}x" if "slowdown_x" in r else "   1.0x"))
    print("\n★ 读法：『残留』越低 = 人声安静处越干净（伴奏漏得少）；"
          "『人声』越高 = 该有的人声保得越多。两条要一起看。")
    print(f"→ {out / 'sep_models.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
