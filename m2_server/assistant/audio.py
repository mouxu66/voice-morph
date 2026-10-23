"""音频预处理：把搜到的歌转成微信语音条能吃的 wav，并按上限截一段。

为什么必须重采样到单声道 PCM
----------------------------
`wechat_voice._wav_duration` 用标准库 `wave` 读时长，只认 PCM。B 站取回来的是
m4a/webm，直接喂进去会 `wave.Error: file does not start with RIFF id` —— 而那个
错误发生在**已经开始录音之后**，等于白录一遍。所以库存在这里就统一成 pcm_s16le。

为什么默认截 54 秒而不是整首
----------------------------
微信语音条 60 秒是硬上限（`wechat_voice.MAX_MSG_S`），而实际可承载的音频预算还要
扣掉播放前导、尾部静音、UIA 向上取整等余量。`MAX_CHUNK_S` 是那条链路算好的数，
这里直接用它，**不自己再定一个常量** —— 两处各定一个数就会漂（本仓吃过这个亏）。

从哪一刻开始截
--------------
默认从 30% 处起。流行歌的副歌通常落在 1/3~2/3，从头截往往只有前奏；从 30% 起
命中副歌的概率最高。这只是默认值，用户可以在提案里给 `start_s` 覆盖。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

try:
    from common import find_ffmpeg
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from common import find_ffmpeg


class AudioError(RuntimeError):
    """转码失败。消息给人看。"""


def probe_duration(path: Path) -> float | None:
    """用 ffprobe 读时长；读不到返回 None（**不要**返回 0，那会和"零秒"混淆）。"""
    ff = Path(find_ffmpeg())
    ffprobe = ff.with_name("ffprobe.exe" if ff.suffix.lower() == ".exe" else "ffprobe")
    if not ffprobe.exists():
        return None
    try:
        out = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return float((out.stdout or "").strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def max_chunk_s() -> float:
    """这条链路能发多长。**从 wechat_voice 取，不另定常量。**"""
    try:
        import wechat_voice

        return float(wechat_voice.MAX_CHUNK_S)
    except Exception:  # noqa: BLE001 —— 插件关着时退到保守值，宁可截短
        return 50.0


def to_wav(
    src: Path,
    dst: Path,
    *,
    start_s: float | None = None,
    clip_s: float | None = None,
    sr: int = 24000,
) -> Path:
    """转成单声道 16bit PCM wav，可选截段。

    `clip_s=None` 表示整首转（用于"我就想听完整版"的离线场景）；传了值就截。
    `start_s=None` 时若有 clip 需求，按总时长 30% 起（见模块注释）。
    """
    src, dst = Path(src), Path(dst)
    if not src.exists():
        raise AudioError(f"源文件不存在：{src.name}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    if clip_s is not None:
        clip_s = max(1.0, float(clip_s))
        if start_s is None:
            total = probe_duration(src) or 0.0
            # 总长没读出来就从头截 —— 比"截了个空文件"强
            start_s = max(0.0, total * 0.3) if total > clip_s else 0.0
        start_s = max(0.0, float(start_s))

    cmd = [str(find_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y"]
    if start_s is not None and start_s > 0:
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-i", str(src)]
    if clip_s is not None:
        cmd += ["-t", f"{clip_s:.3f}"]
    cmd += ["-ac", "1", "-ar", str(int(sr)), "-c:a", "pcm_s16le", str(dst)]

    try:
        res = subprocess.run(
            cmd, capture_output=True, timeout=300, encoding="utf-8", errors="replace", check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise AudioError("转码超时（300s），这个文件可能坏了") from exc
    except OSError as exc:
        raise AudioError(f"ffmpeg 起不来：{exc}") from exc

    if res.returncode != 0 or not dst.exists() or dst.stat().st_size < 1024:
        tail = (res.stderr or "").strip().splitlines()
        raise AudioError(f"转码失败：{tail[-1] if tail else res.returncode}")
    return dst


def clip_for_wechat(
    src: Path, dst: Path, *, start_s: float | None = None, margin_s: float = 1.0
) -> Path:
    """按微信上限截一段。留 `margin_s` 余量，避免刚好卡在判定边界上。"""
    budget = max(5.0, max_chunk_s() - margin_s)
    return to_wav(src, dst, start_s=start_s, clip_s=budget)
