"""素材自动打标（FRD F2）：语言 / 静音占比 / 响度 / 时长 / 可疑 BGM。

设计：
    - 纯音频分析（ffmpeg + soundfile + numpy），无 GPU 依赖，后台线程跑。
    - lang 复用 worker 转写（worker 在线才有效，离线回退 unknown），启发式判 CJK 占比。
    - has_bgm 为启发式：语音节奏占比极低但有持续能量 → 疑似纯 BGM/音乐。
    - 打标失败不阻断上传，写 tag_error 即可。
"""
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

# 静音判定阈值（RMS，线性 0~1）：低于此视为静音帧
_SILENCE_RMS = 0.008      # ≈ -42 dBFS
_FRAME_S = 0.02           # 20ms 分帧
_LOUDNESS_MIN_DBFS = -35.0


def _extract_audio(video: Path, out_wav: Path) -> bool:
    """ffmpeg 提取 16k 单声道 wav；失败返回 False。"""
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video),
             "-vn", "-ac", "1", "-ar", "16000", str(out_wav)],
            capture_output=True, text=True, timeout=300)
        return r.returncode == 0 and out_wav.exists()
    except Exception:
        return False


def _analyze(wav: Path) -> dict:
    """纯音频分析：时长 / 响度 / 语音占比 / 可疑 BGM。失败抛异常由调用方兜底。"""
    data, sr = sf.read(str(wav), dtype="float32")
    if data.ndim > 1:
        data = data[:, 0]
    n = len(data)
    if n == 0:
        raise ValueError("空音频")
    duration_s = n / sr
    # 响度（整体 RMS → dBFS，避免 log(0)）
    rms_all = float(np.sqrt(np.mean(data ** 2) + 1e-12))
    loudness_dbfs = round(float(20 * np.log10(rms_all + 1e-12)), 1)
    # 语音占比：分帧统计 RMS 超阈值的帧占比
    frame = max(1, int(sr * _FRAME_S))
    frames_n = n // frame
    if frames_n == 0:
        speech_ratio = 0.0
        rms = np.zeros(1, dtype=np.float32)
    else:
        seg = data[: frames_n * frame].reshape(frames_n, frame)
        rms = np.sqrt(np.mean(seg ** 2, axis=1) + 1e-12)
        speech_ratio = round(float(np.mean(rms > _SILENCE_RMS)), 3)
    # 疑似 BGM：几乎无停顿（speech_ratio 高）且帧响度过分均匀（变异系数小）。
    # 语音有呼吸/句间停顿，帧 RMS 起伏大；连续音乐/单音则异常平稳（允许误报）。
    cv = float(rms.std() / (rms.mean() + 1e-12))
    has_bgm = speech_ratio > 0.9 and cv < 0.5 and loudness_dbfs > _LOUDNESS_MIN_DBFS
    return {
        "duration_s": round(duration_s, 1),
        "loudness_dbfs": loudness_dbfs,
        "speech_ratio": speech_ratio,
        "has_bgm": bool(has_bgm),
    }


def _detect_lang(wav: Path) -> str:
    """用 worker 转写探测语言（按 CJK 占比判 zh/en）；worker 离线回退 unknown。"""
    try:
        from qwen3_tts import post
        import json as _json
        data = _json.loads(post("/transcribe",
                                {"path": str(wav).replace("\\", "/"),
                                 "vad_filter": False, "fast": True}, timeout=60))
        text = (data.get("text") or "").strip()
        if not text:
            return "unknown"
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        if cjk * 2 >= len(text):
            return "zh"
        return "en" if text.isascii() else "unknown"
    except Exception:
        return "unknown"


def tag_video(video_path: Path, tmp_dir: Path | None = None) -> dict:
    """对素材打标，返回 meta dict（调用方持久化）。"""
    wav = Path(tmp_dir or video_path.parent) / f"_tag_{video_path.stem}.wav"
    try:
        if not _extract_audio(video_path, wav):
            return {"tagging": False, "tag_error": "ffmpeg 提取音轨失败"}
        meta = _analyze(wav)
        meta["lang"] = _detect_lang(wav)
        meta["tagging"] = False
        return meta
    except Exception as e:
        return {"tagging": False, "tag_error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            wav.unlink(missing_ok=True)
        except Exception:
            pass
