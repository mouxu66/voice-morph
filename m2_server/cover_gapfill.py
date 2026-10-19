# -*- coding: utf-8 -*-
"""翻唱掉音补洞：源有声、换声输出却静音的帧，按低电平把源补回去。

为什么需要（2026-10-18 实测）
    《好久不见》整曲：原唱有声、翻唱无声的帧 **984/12706（7.7%）**，142 段，
    最长连续 0.59s，多在气口/词尾。听感是"断字"——不是音色问题，
    任何后期链都救不回来（后期只能改已有声音的形状，不能造出没有的声音）。

边界（必须诚实）
    · 这是**止血**不是修复：补回去的是**原唱**的残段（−14dB），不是你的音色。
      只在"模型相对静音"的地方用，且电平压到 −14dB，正常听感下应只补上"字尾"。
    · 阈值故意保守（源要够响、输出要近乎静音），宁可漏补也不要把原唱混进来。
    · 产品默认开（`VM_COVER_GAPFILL=0` 可关）；A/B 时建议两版都听。

纯函数（`frame_rms` / `drop_mask` / `fill`）不依赖 librosa/ffmpeg，守卫直接测。
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

#: 帧长/帧移（毫秒）。20ms 是"一个音节内核"的量级，足够分辨掉字。
FRAME_MS = 20.0
HOP_MS = 10.0

#: 源帧要高于「源有声帧中位数」的这个比例才算"本该有声"。
SRC_FLOOR_REL = 0.06

#: 输出帧低于源帧的这个比例才算"掉了"。0.30 ≈ −10.5dB。
#: 实测（《好久不见》翻唱干声）：0.15→275 帧(1.1%)、0.25→744(3.0%)、0.4→1489(5.9%)。
#: 取 0.30 覆盖到 10-18 记的"掉音"量级，同时不把正常弱唱当洞。
OUT_RATIO = 0.30

#: 补回去的电平（相对源）。−14dB 在正常混音里只补字尾，不会抢主唱。
FILL_GAIN_DB = -14.0


def frame_rms(x: np.ndarray, sr: int, frame_ms: float = FRAME_MS,
              hop_ms: float = HOP_MS) -> np.ndarray:
    """逐帧 RMS（帧长/帧移按毫秒），末尾不足一帧的丢弃。"""
    x = np.asarray(x, dtype=np.float64)
    n = int(sr * frame_ms / 1000.0)
    hop = max(1, int(sr * hop_ms / 1000.0))
    if len(x) < n or n <= 0:
        return np.zeros(0, dtype=np.float64)
    # 前缀和 O(n) 内存：250s/40k 用滑窗会物化 ~8000 万个数（640MB），
    # 而这个函数在每首歌上要跑两次
    cs = np.concatenate(([0.0], np.cumsum(x * x)))
    starts = np.arange(0, len(x) - n + 1, hop)
    return np.sqrt((cs[starts + n] - cs[starts]) / n)


def drop_mask(src_rms: np.ndarray, out_rms: np.ndarray, *,
              src_floor_rel: float = SRC_FLOOR_REL,
              out_ratio: float = OUT_RATIO) -> np.ndarray:
    """帧级「掉了」布尔掩码：源够响、输出却**相对**近乎静音。

    ★★ 必须先按整曲中位比对齐输出电平：RVC 输出普遍比源低 6–10dB，
    不归一就变成"所有帧都掉了"（实测 out_ratio=0.4 时误报 999 帧，
    归一到同一电平后只剩几十帧）。阈值问的是**动态凹陷**，不是绝对电平。
    """
    m = min(len(src_rms), len(out_rms))
    if m == 0:
        return np.zeros(0, dtype=bool)
    s, o = src_rms[:m], out_rms[:m]
    # ★ 用 >=：`>` 在大量相同值（PCM 量化/静音）上会取到空集，ref 归零后
    #   整个功能静默失效（单测实测：6 帧里 5 帧同值时 mask 全 False）。
    loud = s[s >= np.percentile(s, 60)] if np.any(s > 0) else s
    if loud.size == 0:
        loud = s
    ref = float(np.median(loud)) if loud.size else 0.0
    if ref <= 1e-9:
        return np.zeros(m, dtype=bool)
    # 整曲增益比：只用"源够响且输出非零"的帧估计，避免静音段把中位拉向 0
    idx = (s > ref * src_floor_rel) & (o > 1e-6)
    gain = float(np.median(o[idx] / s[idx])) if np.any(idx) else 1.0
    gain = max(gain, 1e-6)
    return (s > ref * src_floor_rel) & ((o / gain) < s * out_ratio)


def _smooth_bool(mask: np.ndarray, sr: int, hop: int,
                 fade_ms: float = 12.0) -> np.ndarray:
    """帧掩码 → 采样级软权重（0..1），带淡入淡出，避免补丁边缘"咔哒"。"""
    if mask.size == 0:
        return np.zeros(0, dtype=np.float32)
    w = np.repeat(mask.astype(np.float32), hop)
    k = max(3, int(sr * fade_ms / 1000.0))
    kernel = np.hanning(k).astype(np.float32)
    kernel /= max(kernel.sum(), 1e-9)
    return np.convolve(w, kernel, mode="same").astype(np.float32)


def fill(out: np.ndarray, src: np.ndarray, sr: int, *,
         gain_db: float = FILL_GAIN_DB) -> tuple[np.ndarray, int]:
    """把 `src` 在掉音帧按 `gain_db` 补进 `out`，返回 (结果, 掉音帧数)。"""
    o = np.asarray(out, dtype=np.float32)
    s = np.asarray(src, dtype=np.float32)
    n = min(len(o), len(s))
    if n == 0:
        return o, 0
    o, s = o[:n], s[:n]
    sr_rms = frame_rms(s, sr)
    out_rms = frame_rms(o, sr)
    mask = drop_mask(sr_rms, out_rms)
    if not mask.any():
        return o, 0
    hop = max(1, int(sr * HOP_MS / 1000.0))
    weight = _smooth_bool(mask, sr, hop)
    weight = weight[:n]
    if len(weight) < n:
        weight = np.pad(weight, (0, n - len(weight)))
    res = o + s * weight * (10.0 ** (gain_db / 20.0))
    peak = float(np.abs(res).max()) if res.size else 0.0
    if peak > 0.98:
        res = res * (0.98 / peak)
    return res.astype(np.float32), int(mask.sum())


def _read(path: Path) -> tuple[np.ndarray, int]:
    import soundfile as sf

    d, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return np.ascontiguousarray(d.mean(axis=1), dtype=np.float32), int(sr)


def _resample(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return np.asarray(x, dtype=np.float32)
    from math import gcd

    from scipy.signal import resample_poly

    g = gcd(src_sr, dst_sr)
    return resample_poly(x, dst_sr // g, src_sr // g).astype(np.float32)


def enabled() -> bool:
    """产品开关（默认开；`VM_COVER_GAPFILL=0` 关闭，供纯 A/B 用）。"""
    return os.environ.get("VM_COVER_GAPFILL", "1").strip() not in ("0", "false", "False")


def apply_in_place(out_path: Path, source_path: Path) -> int:
    """读 `out_path`、用 `source_path` 补洞、原子覆盖，返回补了几帧。

    失败不抛（沿用 cover 链路的"尽力而为"约定）：调用方只记日志，不让翻唱失败。
    """
    import soundfile as sf

    try:
        out, sr = _read(Path(out_path))
        src, src_sr = _read(Path(source_path))
        src = _resample(src, src_sr, sr)
        res, frames = fill(out, src, sr)
        if frames <= 0:
            return 0
        tmp = Path(out_path).with_suffix(".gapfill.tmp.wav")
        sf.write(str(tmp), res, sr, subtype="PCM_16")
        os.replace(tmp, out_path)
        return frames
    except Exception as e:
        print(f"[cover_gapfill] 跳过（不影响主流程）: {e}", flush=True)
        return 0
