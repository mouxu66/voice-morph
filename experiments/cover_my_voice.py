# -*- coding: utf-8 -*-
"""把一首歌的「歌声」换成某个 RVC 音色（默认：我自己的声音）—— 出可听的 A/B 三件套。

为什么是 experiments/ 而不是 m2_server/：
    产品里的翻唱链路已经存在（`cover_api`：分离 → 换声 → 合回，前端 `/cover`）。
    这个脚本不改那条链路，只做一件事：**把同一条链路搬到命令行、并把中间产物留下来**
    —— 产品链路跑完即删（会话目录），而"我要听听像不像、韵律对不对"需要能反复播放的文件。

复用而不是复写：
    · 分离  `cover_api.separate_song`（demucs `htdemucs --two-stems vocals`）
    · 换声  `rvc_convert.rvc_convert`（常驻 worker，模型缓存；跑在 `D:/RVC/.venv` 子进程）
    · 配平  `cover_api.auto_vocal_gain` / `rms_db`（人声电平普遍低于伴奏，实测配平）
    · 合回  `cover_api.mix_back`
    · 量基频 `cover_api._median_f0`（中位而非均值：副歌会把均值拉高）

★ 变调是这一环唯一的"命门"：RVC 只换音色，**F0 曲线照抄源音频**。
  所以源歌与目标音色的音域差多少，必须显式补上，否则成品是"整首高八度/低八度"。
  本脚本按两边中位基频算建议值，并把测量结果**打印出来**（不藏数字、不猜）。

产出（默认落在 media/covers/<歌名>/）：
    01_original_vocal.wav   原唱人声（分离产物）—— A/B 的"原"
    02_my_voice_vocal.wav   换成目标音色的人声（无伴奏）—— 听音色/韵律最清楚
    03_my_voice_full.wav    人声 + 伴奏（实测配平后）—— 整首翻唱
    *.mp3                   上述三条的试听片段（默认 40s，给"点开就能听"用）

用法：
    .venv/Scripts/python.exe experiments/cover_my_voice.py \
        --song media/songs/lorezo_feed_the_one.mp3 \
        --voice myvoice_ppt12_full \
        --ref media/ft_work/ppt12/ref_24k.wav \
        --out media/covers/lorezo_feed_the_one
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "m2_server"))

import cover_api  # noqa: E402
import rvc_convert  # noqa: E402
from common import find_ffmpeg  # noqa: E402


def ffmpeg_to_wav(src: Path, dst: Path) -> None:
    """统一解码成 44.1k 立体声 wav（demucs 只吃音频，mp3/m4a/flac 都得先过这一刀）。"""
    cmd = [
        find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
        "-ar", "44100", "-ac", "2", str(dst),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"解码失败：{(r.stderr or '').strip()[:400]}")


def median_f0(path: Path) -> float | None:
    """中位基频（Hz）。借 `cover_api._median_f0` 的同一把尺子，避免两处口径不同。"""
    import librosa
    import numpy as np

    return cover_api._median_f0(path, np, librosa)


def suggest_pitch(f0_song: float | None, f0_voice: float | None, max_shift: int = 12) -> int:
    """把人声的中位基频对齐到目标音色的中位基频，返回整数半音（夹在 ±max_shift）。

    为什么夹 ±12：超过一个八度的"对齐"在听感上已经不是同一个人在同一首歌上的音域差，
    多半是测量被污染（哼唱段 / 纯气声 / 伴奏漏进人声）。这时**宁可少调**，
    调错一个八度是整首报废，调少几个半音只是"音域不太贴"。
    """
    import math

    if not f0_song or not f0_voice:
        return 0
    semitones = 12 * math.log2(f0_voice / f0_song)
    return int(max(-max_shift, min(max_shift, round(semitones))))


#: 混音后允许的最高峰值。0.98 而不是 1.0：给 pcm_s16 的量化与下游重采样留一点余量。
MIX_CEILING = 0.98


def _mix_once(vocal: Path, accomp: Path, gains: tuple[float, float], dst: Path, *, float_out: bool = False) -> None:
    """一遍混音。`float_out=True` 时写 float32 —— **探测那遍必须用它**。

    为什么探测不能写默认的 pcm_s16：s16 落盘时**超过满刻度的和会被硬削到 1.000**
    ——"先量一下峰再决定缩多少"就永远量到 1.000，缩放永不生效（本轮实测踩过：
    第二次混音仍然 47608 个削波采样点，因为第一次量到的峰已经是削平后的）。
    float32 保留真实幅度（可以 >1.0），才能算出正确的缩放系数。
    """
    vg, ag = gains
    cmd = [
        find_ffmpeg(), "-y", "-loglevel", "error",
        "-i", str(vocal), "-i", str(accomp),
        "-filter_complex",
        f"[0:a]volume={vg}[v];[1:a]volume={ag}[a];"
        f"[v][a]amix=inputs=2:duration=longest:normalize=0",
        "-ar", "44100", "-ac", "2",
    ] + (["-c:a", "pcm_f32le"] if float_out else []) + [str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError("混音失败：" + " | ".join((r.stderr or "").strip().splitlines()[-3:])[:400])


def _peak(path: Path) -> tuple[float, int]:
    import numpy as np
    import soundfile as sf

    a = np.abs(np.asarray(sf.read(str(path), dtype="float64")[0]))
    return float(a.max()), int((a >= 0.99).sum())


def mix_peak_safe(vocal: Path, accomp: Path, out: Path, gains: tuple[float, float]) -> tuple[float, int, tuple[float, float]]:
    """合回伴奏，**并防削波** —— 返回 (峰值, ≥0.99 的采样数, 实际使用的增益)。

    为什么不能直接用 `cover_api.mix_back`：它写的是 pcm_s16，而 `amix normalize=0`
    是**相加**。本项目实测（2026-10-14，Lorezo-Feed the One）：换声输出比伴奏安静得多，
    自动配平顶到夹紧上限 ×3.00，相加后 **0.35% 的采样点顶到 1.000**（硬削波，听感是
    副歌/鼓点上偶发的刺耳感）。而链路里没有任何环节会报错 —— 正是本项目最贵的那类失效。

    两种防法的取舍（都试过了，这版留的是第二种）：
      · 限幅器 `alimiter`：一遍完事，但实测把峰值压到 **0.726** —— 鼓点密集处
        增益衰减几乎不间断，整轨被压扁（动态损失听得出来）；
      · **两遍等比缩放**（本实现）：先按原配比混一遍量峰，超顶就**整体等比缩**再混。
        配比与动态完全保留，代价只是整体轻一点（听者把音量调大即可）；
        多跑一遍 ffmpeg，实测 3 分钟的歌只要 ~0.6s。
    """
    probe = out.with_name(f".{out.stem}.probe.f32.wav")
    _mix_once(vocal, accomp, gains, probe, float_out=True)  # ← float 探测，见 _mix_once 的注释
    true_peak, _ = _peak(probe)
    probe.unlink(missing_ok=True)
    applied = gains
    if true_peak > MIX_CEILING:
        k = MIX_CEILING / true_peak
        applied = (round(gains[0] * k, 4), round(gains[1] * k, 4))
    _mix_once(vocal, accomp, applied, out)  # 线性缩放 ⇒ 一遍就到位（峰值也线性跟随）
    peak, clipped = _peak(out)
    return peak, clipped, applied


def clip_and_encode(src: Path, dst_mp3: Path, start_s: float, seconds: float) -> None:
    """截一段并转 mp3 —— 试听页用 data-URI 内嵌，wav 太大（一分钟 ~10MB）。"""
    cmd = [
        find_ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{start_s:.2f}", "-t", f"{seconds:.2f}",
        "-i", str(src), "-ac", "2", "-ar", "44100", "-b:a", "160k", str(dst_mp3),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0 or not dst_mp3.exists():
        raise RuntimeError(f"试听片段编码失败：{(r.stderr or '').strip()[:400]}")


def main() -> int:
    ap = argparse.ArgumentParser(description="把歌声换成指定 RVC 音色（出 A/B 可听件）")
    ap.add_argument("--song", required=True, type=Path, help="整首歌（mp3/wav/m4a…）")
    ap.add_argument("--voice", default="myvoice_ppt12_full", help="RVC 实验名（logs/ 或 assets/weights/ 下）")
    ap.add_argument("--ref", type=Path, default=None, help="目标音色的参考音（用来量音域）")
    ap.add_argument("--out", required=True, type=Path, help="产物目录")
    ap.add_argument("--pitch", type=int, default=None, help="半音；不给则按中位基频自动建议")
    ap.add_argument("--index-rate", type=float, default=0.5)
    ap.add_argument("--clip-start", type=float, default=0.0, help="试听片段起点（秒）")
    ap.add_argument("--clip-seconds", type=float, default=40.0)
    args = ap.parse_args()

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    song = args.song if args.song.is_absolute() else _ROOT / args.song
    if not song.exists():
        print(f"[!] 找不到歌：{song}")
        return 2

    t_all = time.time()
    log: dict = {"song": str(song), "voice": args.voice, "steps": []}

    # ① 解码
    wav = out_dir / "_song_44k.wav"
    ffmpeg_to_wav(song, wav)
    log["steps"].append({"decode": round(time.time() - t_all, 1)})

    # ② 分离歌声 / 伴奏
    t = time.time()
    stamp = int(time.time() * 1000)
    vocals, accomp = cover_api.separate_song(wav, stamp)
    sep_s = round(time.time() - t, 1)
    orig_vocal = out_dir / "01_original_vocal.wav"
    shutil.copy2(vocals, orig_vocal)
    shutil.copy2(accomp, out_dir / "_accomp.wav")

    # ③ 变调决策（把两边的实测基频都打出来，不藏数字）
    f0_song = median_f0(orig_vocal)
    f0_voice = median_f0(args.ref) if args.ref and args.ref.exists() else None
    pitch = args.pitch if args.pitch is not None else suggest_pitch(f0_song, f0_voice)
    print(f"[音域] 歌声中位基频 {f0_song and round(f0_song, 1)} Hz · 目标音色 {f0_voice and round(f0_voice, 1)} Hz"
          f" ⇒ 变调 {pitch:+d} 半音")
    log.update(f0_song=f0_song, f0_voice=f0_voice, pitch=pitch, separate_s=sep_s)

    # ④ 换声（常驻 worker：第一次要加载模型，之后每条 ~几秒）
    t = time.time()
    rvc_out = rvc_convert.rvc_convert(vocals, args.voice, pitch=pitch, index_rate=args.index_rate)
    conv_s = round(time.time() - t, 1)
    my_vocal = out_dir / "02_my_voice_vocal.wav"
    shutil.copy2(rvc_out, my_vocal)

    # ⑤ 合回伴奏（实测 RMS 配平：换声输出电平普遍低于 demucs 伴奏）
    t = time.time()
    vg = cover_api.auto_vocal_gain(cover_api.rms_db(my_vocal), cover_api.rms_db(out_dir / "_accomp.wav"))
    full = out_dir / "03_my_voice_full.wav"
    peak, clipped, applied = mix_peak_safe(my_vocal, out_dir / "_accomp.wav", full, gains=(vg, 1.0))
    mix_s = round(time.time() - t, 1)
    log.update(convert_s=conv_s, mix_s=mix_s, vocal_gain=vg, mix_peak=round(peak, 4),
               clipped_samples=clipped, gains_applied=applied)
    print(f"[换声] {conv_s}s · [合回] {mix_s}s（人声 ×{applied[0]:.2f}/{vg:.2f}，峰值 {peak:.3f}，削波采样点 {clipped}）")

    # ⑥ 试听片段（mp3，给"点开就能听"的页面用）
    clips = {}
    for src, key in ((orig_vocal, "01_original_vocal"), (my_vocal, "02_my_voice_vocal"), (full, "03_my_voice_full")):
        mp3 = out_dir / f"{key}.mp3"
        clip_and_encode(src, mp3, args.clip_start, args.clip_seconds)
        clips[key] = str(mp3.relative_to(_ROOT)) if mp3.is_relative_to(_ROOT) else str(mp3)
    log["clips"] = clips
    log["total_s"] = round(time.time() - t_all, 1)

    # ⑦ 清场：只留成品（分离的中间态与解码中间态没有复用价值）
    wav.unlink(missing_ok=True)
    (out_dir / "_accomp.wav").unlink(missing_ok=True)
    shutil.rmtree(cover_api._separate_dir() / f"sep_{stamp}", ignore_errors=True)
    for leftover in Path(rvc_out).parent.glob(f"{Path(vocals).stem}_*.wav"):
        leftover.unlink(missing_ok=True)

    (out_dir / "run.json").write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[完成] 产物在 {out_dir}（总耗时 {log['total_s']}s）")
    for p in sorted(out_dir.iterdir()):
        print(f"        {p.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
