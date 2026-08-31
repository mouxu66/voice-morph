# -*- coding: utf-8 -*-
"""QLoRA 训练数据准备：raw_videos → 人声分离 → 静音切分 → media/clips。

与 pipeline_clean.py 同一套质检参数（-42dB 静音阈值、2.5~10s 切片、归一化），
但目录参数化，直接服务微调数据管线：
    media/raw_videos  --demucs-->  media/clips/*.wav
之后接 m1_workshop/transcribe_for_ft.py 生成训练 JSONL。

用法（在 .venv 里跑，需要 demucs / pydub / ffmpeg）：
    D:/变声/.venv/Scripts/python.exe m1_workshop/prep_ft_data.py
    D:/变声/.venv/Scripts/python.exe m1_workshop/prep_ft_data.py --raw_dir media/raw_videos --clips_dir media/clips
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

from pydub import AudioSegment
from pydub.silence import detect_nonsilent

ROOT = Path(__file__).resolve().parent.parent

SILENCE_THRESH = -42
MIN_SILENCE_MS = 350
CLIP_MIN_MS = 2500
CLIP_MAX_MS = 10000
DEMUCS_MODEL = "htdemucs"


def _run(cmd, desc=""):
    proc = subprocess.Popen(cmd)
    while proc.poll() is None:
        time.sleep(0.2)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def step1_extract(video: Path, vocals_dir: Path) -> Path:
    out = vocals_dir / f"{video.stem}.wav"
    if out.exists():
        print(f"[提取] 已存在，跳过: {out.name}")
        return out
    _run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "44100", str(out)],
         desc=f"ffmpeg {video.name}")
    print(f"[提取] {video.name} -> {out.name}")
    return out


def step2_separate(wav: Path, demucs_out: Path) -> Path:
    result = demucs_out / DEMUCS_MODEL / wav.stem / "vocals.wav"
    if result.exists():
        print(f"[分离] 已存在，跳过: {result.name}")
        return result
    _run([sys.executable, "-m", "demucs", "-n", DEMUCS_MODEL, "--two-stems", "vocals",
          "-o", str(demucs_out), str(wav)], desc=f"demucs {wav.name}")
    if not result.exists():
        print(f"[分离] 未找到 {result}，回退用提取音轨")
        return wav
    print(f"[分离] 人声 -> {result.name}")
    return result


def step3_slice(vocal: Path, prefix: str, clips_dir: Path) -> int:
    audio = AudioSegment.from_wav(vocal).set_channels(1).set_frame_rate(22050)
    audio = audio.normalize(headroom=6.0)
    if audio.max_dBFS < -50:
        print(f"[切分] {vocal.name} 响度过低，跳过")
        return 0
    spans = detect_nonsilent(audio, min_silence_len=MIN_SILENCE_MS, silence_thresh=SILENCE_THRESH)
    if not spans:
        print(f"[切分] {vocal.name} 无语音，跳过")
        return 0
    count = 0
    buf_start = spans[0][0]
    for s, e in spans + [(0, None)]:
        if e is None or (e - buf_start) >= CLIP_MIN_MS:
            seg_end = e if e is None else min(e, buf_start + CLIP_MAX_MS)
            clip = audio[buf_start:seg_end]
            if len(clip) >= CLIP_MIN_MS:
                count += 1
                clip.export(clips_dir / f"{prefix}_{count:03d}.wav", format="wav")
            if e is not None:
                buf_start = s if e - buf_start >= CLIP_MAX_MS else e
    print(f"[切分] {vocal.name} -> {count} 段")
    return count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw_dir", default="media/raw_videos")
    ap.add_argument("--clips_dir", default="media/clips")
    ap.add_argument("--work_dir", default="media/ft_work",
                    help="中间产物（提取音轨/demucs 输出）存放目录")
    args = ap.parse_args()

    raw = (ROOT / args.raw_dir) if not Path(args.raw_dir).is_absolute() else Path(args.raw_dir)
    clips = (ROOT / args.clips_dir) if not Path(args.clips_dir).is_absolute() else Path(args.clips_dir)
    work = (ROOT / args.work_dir) if not Path(args.work_dir).is_absolute() else Path(args.work_dir)
    vocals = work / "vocals"
    demucs_out = work / "demucs_out"
    for d in (clips, vocals, demucs_out):
        d.mkdir(parents=True, exist_ok=True)

    videos = sorted(f for f in raw.iterdir()
                    if f.suffix.lower() in (".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi"))
    if not videos:
        print(f"{raw} 里没有视频！")
        sys.exit(1)

    total = 0
    for v in videos:
        print(f"\n===== {v.name} =====")
        try:
            wav = step1_extract(v, vocals)
            vocal = step2_separate(wav, demucs_out)
            total += step3_slice(vocal, v.stem, clips)
        except subprocess.CalledProcessError as e:
            print(f"[错误] {v.name}: {e}")

    print(f"\n完成：共 {total} 段切片 -> {clips}")


if __name__ == "__main__":
    main()
