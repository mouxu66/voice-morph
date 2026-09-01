"""M1 流水线：raw_videos 里的所有视频 → 提音轨 → 去BGM → 切成3~10秒片段

用法：
    python m1_workshop/pipeline.py
"""
import subprocess
import sys
import time
from pathlib import Path

from pydub import AudioSegment
from pydub.silence import detect_nonsilent

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "media" / "raw_videos"
VOCALS = ROOT / "media" / "vocals"
CLIPS = ROOT / "media" / "clips"
DEMUCS_OUT = ROOT / "media" / "demucs_out"   # demucs 的输出目录
for d in (RAW, VOCALS, CLIPS, DEMUCS_OUT):
    d.mkdir(parents=True, exist_ok=True)


class PipelineCancelled(Exception):
    """流水线被用户取消"""


def _run_or_cancel(cmd, cancel_event=None, desc=""):
    """运行子进程，支持中途取消（终止并等待退出）"""
    proc = subprocess.Popen(cmd)
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            raise PipelineCancelled("流水线已取消")
        time.sleep(0.2)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=desc.encode() if desc else None)

# 可调参数
SILENCE_THRESH = -42    # 静音阈值(dBFS)，切片全是噪音时调到 -38
MIN_SILENCE_MS = 350    # 判断"间隔"的最短静音
CLIP_MIN_MS = 2500      # 短于此时长的片段丢弃
CLIP_MAX_MS = 10000     # 片段最长 10 秒
DEMUCS_MODEL = "htdemucs"  # 想要更好分离质量换 "htdemucs_ft"（更慢）


def step1_extract(video: Path, cancel_event=None) -> Path:
    """视频 → 44.1k 单声道 wav 音轨"""
    out = VOCALS / f"{video.stem}.wav"
    _run_or_cancel(
        ["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "44100", str(out)],
        cancel_event=cancel_event, desc=f"ffmpeg {video.name}",
    )
    print(f"[提取] {video.name} ✓ -> {out.name}")
    return out


def step2_separate(wav: Path, cancel_event=None) -> Path:
    """去 BGM。demucs 第一次运行会自动下载模型(~300MB)，请耐心等待。"""
    _run_or_cancel(
        [
            sys.executable, "-m", "demucs", "-n", DEMUCS_MODEL,
            "--two-stems", "vocals", "-o", str(DEMUCS_OUT), str(wav),
        ],
        cancel_event=cancel_event, desc=f"demucs {wav.name}",
    )
    result = DEMUCS_OUT / DEMUCS_MODEL / wav.stem / "vocals.wav"
    if not result.exists():
        raise FileNotFoundError(f"demucs 未生成预期文件: {result}")
    print(f"[分离] 人声已输出 -> {result}")
    return result


def step3_slice(vocal: Path, prefix: str, cancel_event=None):
    """静音检测 → 合并成 3~10 秒片段"""
    audio = AudioSegment.from_wav(vocal).set_channels(1).set_frame_rate(22050)
    # 响度归一化：保证低响度素材也能被静音检测识别（真实素材普遍响度不一）
    audio = audio.normalize(headroom=6.0)
    if audio.max_dBFS < -50:
        print(f"[切分] {vocal.name} 响度过低(max {audio.max_dBFS:.1f}dBFS)，可能是纯 BGM 或静音，跳过")
        return 0
    spans = detect_nonsilent(
        audio, min_silence_len=MIN_SILENCE_MS, silence_thresh=SILENCE_THRESH
    )
    if not spans:
        print(f"[切分] {vocal.name} 没检测到人声，跳过")
        return 0

    count = 0
    buf_start = spans[0][0]
    # 在末尾追加哨兵，保证最后一个缓冲被切出
    for s, e in spans + [(0, None)]:
        if cancel_event is not None and cancel_event.is_set():
            raise PipelineCancelled("流水线已取消")
        if e is None or (e - buf_start) >= CLIP_MIN_MS:
            seg_end = e if e is None else min(e, buf_start + CLIP_MAX_MS)
            clip = audio[buf_start:seg_end]
            if len(clip) >= CLIP_MIN_MS:
                count += 1
                name = CLIPS / f"{prefix}_{count:03d}.wav"
                clip.export(name, format="wav")
            if e is not None:
                buf_start = s if e - buf_start >= CLIP_MAX_MS else e
    print(f"[切分] {vocal.name} -> {count} 个片段在 media/clips/")
    return count


def main():
    # 视频与音频素材统一处理（音频文件 ffmpeg -vn 提轨同样有效）
    suffixes = (".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi",
                ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".wma")
    videos = [f for f in RAW.iterdir() if f.suffix.lower() in suffixes]
    if not videos:
        print("把视频/音频素材放进 media/raw_videos/ 再运行！")
        sys.exit(1)
    for v in videos:
        try:
            wav = step1_extract(v)
            vocal = step2_separate(wav)
            step3_slice(vocal, v.stem[:12])
        except subprocess.CalledProcessError as e:
            print(f"[错误] {v.name} 处理失败: {e}")
        except FileNotFoundError as e:
            print(f"[错误] {e}")
    print("\n下一步：去 media/clips/ 试听，挑出【纯美团老鼠音色】的片段，然后运行 build_reference.py")


if __name__ == "__main__":
    main()
