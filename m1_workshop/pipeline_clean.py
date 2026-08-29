"""干净流水线：只处理 media/raw_videos_clean/ 里的视频，输出到 media/clips_clean/
与 pipeline.py 区别：
  - 用完整视频 stem 做切片前缀，避免 video_260828_105338 / video_260828_110637 编号重叠
  - 独立输出目录，绝不混入其他视频
"""
import subprocess, sys, time
from pathlib import Path
from pydub import AudioSegment
from pydub.silence import detect_nonsilent

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "media" / "raw_videos_clean"
VOCALS = ROOT / "media" / "vocals_clean"
CLIPS = ROOT / "media" / "clips_clean"
DEMUCS_OUT = ROOT / "media" / "demucs_out_clean"
for d in (RAW, VOCALS, CLIPS, DEMUCS_OUT):
    d.mkdir(parents=True, exist_ok=True)

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
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=desc.encode() if desc else None)

def step1_extract(video):
    out = VOCALS / f"{video.stem}.wav"
    _run(["ffmpeg","-y","-i",str(video),"-vn","-ac","1","-ar","44100",str(out)], desc=f"ffmpeg {video.name}")
    print(f"[提取] {video.name} -> {out.name}")
    return out

def step2_separate(wav):
    _run([sys.executable,"-m","demucs","-n",DEMUCS_MODEL,"--two-stems","vocals","-o",str(DEMUCS_OUT),str(wav)], desc=f"demucs {wav.name}")
    result = DEMUCS_OUT / DEMUCS_MODEL / wav.stem / "vocals.wav"
    if not result.exists():
        # 无BGM时 demucs 也输出原声，若失败回退直接用提取wav
        print(f"[分离] 未找到 {result}，回退用提取音轨")
        return wav
    print(f"[分离] 人声 -> {result}")
    return result

def step3_slice(vocal, prefix):
    audio = AudioSegment.from_wav(vocal).set_channels(1).set_frame_rate(22050)
    audio = audio.normalize(headroom=6.0)
    if audio.max_dBFS < -50:
        print(f"[切分] {vocal.name} 响度过低，跳过"); return 0
    spans = detect_nonsilent(audio, min_silence_len=MIN_SILENCE_MS, silence_thresh=SILENCE_THRESH)
    if not spans:
        print(f"[切分] {vocal.name} 无语音，跳过"); return 0
    count = 0
    buf_start = spans[0][0]
    for s, e in spans + [(0, None)]:
        if e is None or (e - buf_start) >= CLIP_MIN_MS:
            seg_end = e if e is None else min(e, buf_start + CLIP_MAX_MS)
            clip = audio[buf_start:seg_end]
            if len(clip) >= CLIP_MIN_MS:
                count += 1
                clip.export(CLIPS / f"{prefix}_{count:03d}.wav", format="wav")
            if e is not None:
                buf_start = s if e - buf_start >= CLIP_MAX_MS else e
    print(f"[切分] {vocal.name} -> {count} 段")
    return count

def main():
    videos = [f for f in RAW.iterdir() if f.suffix.lower() in (".mp4",".mkv",".mov",".flv",".webm",".avi")]
    if not videos:
        print("media/raw_videos_clean/ 没有视频！"); sys.exit(1)
    for v in sorted(videos):
        try:
            wav = step1_extract(v)
            vocal = step2_separate(wav)
            step3_slice(vocal, v.stem)   # 完整 stem 做前缀，唯一
        except subprocess.CalledProcessError as e:
            print(f"[错误] {v.name}: {e}")

if __name__ == "__main__":
    main()
