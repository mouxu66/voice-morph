# -*- coding: utf-8 -*-
"""自动音高建议：分析输入音频的基频（f0），对照目标音色参考音高，
算出 RVC 变调（半音数）建议值，解决"怎么调都不像"的手动试错。

用法：
    from pitch_advice import analyze_f0, suggest_pitch, voice_ref_f0

设计：
    - f0 用 librosa.pyin（概率 YIN，对人声稳健），16k 单声道输入。
    - 输入音频先经 ffmpeg 统一转 16k 单声道 wav（与 offline_vc 预处理同一套路），
      避免直接解码 webm/m4a 等容器失败。
    - 音色参考音高取 voicebank/<id>/reference.wav，按 (mtime, size) 缓存，
      重复请求不重算。
    - 建议 = 12 * log2(ref_f0 / in_f0)，四舍五入并夹到 [-12, +12]
      （与前端变调滑块范围一致）。
    - 有效语音占比过低（< 8%）时认为 f0 不可靠，返回 voiced_ratio 让前端提示。
"""
import subprocess
import threading
import tempfile
from pathlib import Path

import numpy as np

import config as cfg
from common import find_ffmpeg

FMIN_HZ = 50.0
FMAX_HZ = 500.0
MIN_VOICED_RATIO = 0.08   # 低于此值认为 f0 不可靠
MAX_ANALYZE_S = 60        # 过长音频只取开头 60s 分析（pyin 较慢）

# 参考音高缓存：voice_id -> (mtime, size, f0)
_ref_cache: dict[str, tuple[float, int, float]] = {}
_cache_lock = threading.Lock()


def _to_wav16k(src: Path, dst: Path) -> None:
    cmd = [find_ffmpeg(), "-y", "-loglevel", "error", "-i", str(src),
           "-t", str(MAX_ANALYZE_S), "-af", "aresample=16000", "-ac", "1", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ffmpeg 解码失败: {r.stderr.strip()[:300]}")


def f0_curve(y: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """pyin 提取基频曲线。返回 (f0, voiced_flag)（帧级，可跨信号对比）。"""
    import librosa

    f0, voiced_flag, _ = librosa.pyin(
        np.asarray(y, dtype=np.float32), fmin=FMIN_HZ, fmax=FMAX_HZ,
        sr=sr, frame_length=1024,
    )
    return f0, np.asarray(voiced_flag, dtype=bool)


def analyze_f0(path: Path) -> dict:
    """分析音频基频。返回 {f0: float|None, voiced_ratio: float}。

    f0 为有效帧的中位数基频（Hz）；无效语音过多时为 None。
    """
    import soundfile as sf

    path = Path(path)
    with tempfile.TemporaryDirectory(dir=str(cfg.OUTPUTS_DIR)) as td:
        wav16k = Path(td) / "f0_in.wav"
        try:
            # 已经是 wav 的直接读，省一次 ffmpeg
            data, sr = sf.read(str(path), dtype="float32")
            if data.ndim > 1:
                data = data.mean(axis=1)
            if sr != 16000:
                raise ValueError("需重采样")
            y = data
        except Exception:
            _to_wav16k(path, wav16k)
            y, sr = sf.read(str(wav16k), dtype="float32")

    if len(y) < sr // 2:  # < 0.5s 不可分析
        return {"f0": None, "voiced_ratio": 0.0}

    f0, voiced_flag = f0_curve(y, sr)
    voiced_ratio = float(np.mean(voiced_flag)) if len(voiced_flag) else 0.0
    med = np.nanmedian(f0) if f0 is not None else np.nan
    return {
        "f0": round(float(med), 1) if np.isfinite(med) else None,
        "voiced_ratio": round(voiced_ratio, 3),
    }


def voice_ref_f0(voice_id: str) -> float | None:
    """目标音色参考音频的中位基频（带缓存）；无 reference.wav 返回 None。"""
    ref = cfg.MEDIA_DIR / "voicebank" / voice_id / "reference.wav"
    if not ref.exists():
        return None
    stat = ref.stat()
    key = (stat.st_mtime, stat.st_size)
    with _cache_lock:
        hit = _ref_cache.get(voice_id)
        if hit and (hit[0], hit[1]) == key:
            return hit[2]
    f0 = analyze_f0(ref)["f0"]
    if f0:
        with _cache_lock:
            _ref_cache[voice_id] = (stat.st_mtime, stat.st_size, f0)
    return f0


def suggest_pitch(in_f0: float | None, ref_f0: float | None) -> int | None:
    """按中位音高算建议变调（半音），夹到 [-12, +12]。任一 f0 缺失返回 None。"""
    if not in_f0 or not ref_f0:
        return None
    semis = 12.0 * float(np.log2(ref_f0 / in_f0))
    return int(max(-12, min(12, round(semis))))


def full_suggestion(path: Path, voice_id: str) -> dict:
    """一步到位：分析输入 + 查参考音高 + 算建议。供 /offlinevc/pitch_suggest 使用。"""
    in_res = analyze_f0(path)
    ref_f0 = voice_ref_f0(voice_id) if voice_id else None
    unreliable = in_res["f0"] is None or in_res["voiced_ratio"] < MIN_VOICED_RATIO
    suggested = None if unreliable else suggest_pitch(in_res["f0"], ref_f0)
    return {
        "input_f0": in_res["f0"],
        "voiced_ratio": in_res["voiced_ratio"],
        "ref_f0": ref_f0,
        "suggested_pitch": suggested,
        "reliable": not unreliable,
    }
