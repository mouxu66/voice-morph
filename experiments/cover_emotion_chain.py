# -*- coding: utf-8 -*-
"""《好久不见》supp2026 成品 · 「表现力 / 情感」加工 A/B（2026-10-18）。

### 为什么做这一页（用户原话）

> “现在我挺满意的，这颜色，但是它还没有情感那些方面的，我们可以加一点吗？
>   就是丰富一下它的那种，表现力感染力那种，可以学习一下吗”

先量了「换声有没有把表演压平」（`_probe_emotion.py`，全曲、同一时间轴）：

| 指标 | 源（陈奕迅） | 换声干声 | 读法 |
|---|---|---|---|
| 50ms RMS 动态 std | 13.9 dB | 15.9 dB | 没压平，甚至更大 |
| 有声帧非浊音能量占比 | 0.092 | 0.093 | 换气/气声没丢 |
| 4–8Hz 颤音深度 p90 | 0.52 半音 | 0.55 半音 | 颤音没丢 |
| 帧级音高差中位 | — | 0.000 半音 | 音高忠实跟随 |

⇒ **“缺情感”不是 RVC 把动态/换气/颤音压平了**。干声该有的表演都在，
缺的是两件事：① 成品里没有空间与段落推拉（一直是同一个音量、同一个干声）；
② 目标音色没有“情绪状态”的样本（训练集是平的说话 + 低音唱歌）。
本页只做①（今天就能听）；② 要录情绪素材重训，见
`docs/产品研究/m1m_情感表现力路线_2026-10-18.md` 与 `docs/情感录制稿-2026-10.md`。

### 臂（单变量叠加，全部基于用户已确认的颜色）

基线 = `natural/supp_post_breath.wav`（+6dB@7k + De-Ess + 轻压缩 + 气口 −18dB），
混音增益用**旧成品反解出来的同一组**（最小二乘 corr 0.999998，max|diff| 0.0016）：

    旧 mix = 0.95450 · vocal + 0.70710 · accomp

| 臂 | 加工 | 想验证什么 |
|---|---|---|
| A | 对照：旧成品同款 | 你的“颜色”基线 |
| B | 空间：2.0s 板式混响 | 情感是不是缺“空间/余韵” |
| C | 呼吸：按源的 8s 包络做段落推拉 ±2dB | 情感是不是缺“推/收” |
| D | B + C | 空间与推拉是否叠加 |
| E | D + 密度（并行压缩/饱和） | 情感是不是缺“贴耳/颗粒” |
| F | E + 宽化（双轨延迟） | 情感是不是缺“宽/大”；探过界 |

★ 纪律：不改产品链路、不重训、不动用户已确认的颜色（8–12k 与谱平坦度只做
**漂移监测**，不作为“更好”的依据）。全部只写 `media/covers/lowsong_20261018/emotion/`
（gitignore），最终由耳朵裁决。

### 已知边界（写进页面，防止误读）

- 混响/宽化会**降低“像你”**（CAM++ 是本项目第三次证明会被加工骗的尺子），
  所以本页不拿 CAM++ 排序，只报“漂移”。
- 并行压缩会改变 4–8k 与 Crest，可能让“颜色”变；E/F 是探边界，不是产品值。
- 段落推拉只做 8s 平滑，**不动音节内动态**（干声已有，重复推会变抽气）。

用法：
    python experiments/cover_emotion_chain.py run    # 6 臂 + 指标
    python experiments/cover_emotion_chain.py page   # 试听页
    python experiments/cover_emotion_chain.py all
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))
BASE = ROOT / "media" / "covers" / "lowsong_20261018"
OUT = BASE / "emotion"
SR = 44100

#: 基线输入：用户已确认颜色的成品干声（+6dB@7k + 气口）
VOCAL = BASE / "natural" / "supp_post_breath.wav"
ACC = BASE / "natural" / "acc.wav"
#: 旧成品（用于反解混音增益 + 作为 A 臂逐字节/相关性自检）
OLD_MIX = BASE / "natural" / "mix_supp_air6_breath.wav"

#: ★ 由旧成品最小二乘反解（2026-10-18，corr 0.999998 / max|diff| 0.0016）。
#: 不自己另拍一组：换一组增益 = 换了“颜色”，A 臂就不再是对照。
VOCAL_GAIN = 0.95450371
ACC_GAIN = 0.70710461

#: 混响：1.8s RT60、2.2s IR、湿声 −16dB。保守档：应该听出“有空间”，
#: 但不该听出“加了混响”。
REVERB_RT60 = 2.0
REVERB_LENGTH = 2.4
#: −11dB 湿声（0.28）。0.16 在实测里几乎听不出尾巴：RVC 干声自己就带 −7dB 的尾，
#: 再叠一条 −16dB 的混响等于没加。慢歌的“厅堂感”需要 20–30% 才成立，但仍低于
#: “明显加了混响”的 40%。
REVERB_WET = 0.28

#: 段落推拉：8s 平滑、±2dB。只动“主歌收/副歌推”，不动音节内。
AUTO_WINDOW_S = 8.0
AUTO_MAX_DB = 2.0

#: 并行压缩：4:1、−24dB 阈值、+3dB makeup；并行副本再软饱和，按 −18dB 混入。
DENSITY_AMOUNT = 0.13

#: 双轨宽化：左右各 13/19ms、−18dB。超过就变成合唱效果。
WIDE_DELAY_MS = (13.0, 19.0)
WIDE_AMOUNT = 0.12

MIX_CEILING = 0.98


# ---------------------------------------------------------------- 纯函数（可单测）

def make_ir(
    sr: int = SR,
    rt60: float = REVERB_RT60,
    length_s: float = REVERB_LENGTH,
    seed: int = 1234,
    pre_delay_ms: float = 0.0,
) -> np.ndarray:
    """生成一条立体声“板式”混响 IR（指数衰减噪声 + 早期反射）。

    为什么不用 ffmpeg 的 `aecho`：`aecho` 是**离散回声**，在慢歌上会听成
    “山谷里的重复”，不是空间。这里用真卷积：早期反射给方向感、指数尾巴给余韵。

    `pre_delay_ms` 把**整条 IR 往后推**（前面补零），总时长仍是 `length_s`：
    干声到达耳朵后先静一段、空间才开始。这是 SOS 那组 plate 案例的核心参数
    （60–80ms），而本函数默认值仍是 0（= 早期反射第一拍在 11ms，`m1l` 原档）——
    默认路径逐字节不变，老结论可复算。⚠️ 2026-10-18 之前把它当“混响类型”一维漏了，
    实际 2.0s/11ms 更接近中厅而不是流行 plate（`m1m` §4.2）。
    """
    n = max(1, int(round(sr * length_s)))
    rng = np.random.RandomState(seed)
    t = np.arange(n, dtype=np.float64) / sr
    pre_s = max(0.0, float(pre_delay_ms)) / 1000.0
    onset = min(n - 1, int(round(pre_s * sr)))
    # −60dB 的时间 = rt60 ⇒ 幅度衰减 e^{-6.9078 t / rt60}（t 从首达时刻算起）
    dt = t - pre_s
    late = rng.standard_normal((n, 2)) * np.exp(-6.9078 * np.clip(dt, 0.0, None) / max(1e-6, rt60))[:, None]
    late[dt < 0.0] = 0.0
    # 10ms 淡入，避免 IR 起点“啪”一声
    fade = min(n - onset, max(1, int(0.010 * sr)))
    late[onset:onset + fade] *= np.linspace(0.0, 1.0, fade)[:, None]

    # 早期反射：左右去相关（同一组时间、不同符号/微差），给立体声宽度
    ir = late.astype(np.float64)
    taps_ms = (11.0, 17.0, 23.0, 31.0, 41.0, 53.0, 67.0)
    gains = (0.60, 0.45, 0.35, 0.28, 0.20, 0.15, 0.10)
    for ch in range(2):
        for k, (ms, g) in enumerate(zip(taps_ms, gains, strict=False)):
            idx = int(round((ms + pre_delay_ms + ch * 0.7) * sr / 1000.0))
            if idx < n:
                sign = 1.0 if (k + ch) % 2 == 0 else -1.0
                ir[idx, ch] += sign * g * 0.5

    # 高切 6.5k / 低切 180Hz：慢歌的人声空间不该带“嘶”或“轰”
    ir = _one_pole_lowpass(ir, sr, 6500.0)
    ir = ir - _one_pole_lowpass(ir, sr, 180.0)  # 一阶差分高通
    # 每条通道单位能量：湿声电平只由 wet 决定，换 IR 不会偷偷改响度
    for ch in range(2):
        e = float(np.sqrt((ir[:, ch] ** 2).sum()))
        if e > 1e-12:
            ir[:, ch] /= e
    return ir.astype(np.float32)


def _one_pole_lowpass(x: np.ndarray, sr: int, cutoff_hz: float) -> np.ndarray:
    """一阶低通（对每条通道独立）。IR 只有 2 秒，用 lfilter 足够快。"""
    from scipy.signal import lfilter

    if cutoff_hz <= 0 or cutoff_hz >= sr / 2:
        return np.asarray(x, dtype=np.float64)
    a = math.exp(-2.0 * math.pi * cutoff_hz / sr)
    return lfilter([1.0 - a], [1.0, -a], np.asarray(x, dtype=np.float64), axis=0)


def automation_gain(
    src_mono: np.ndarray,
    sr: int = SR,
    *,
    window_s: float = AUTO_WINDOW_S,
    max_db: float = AUTO_MAX_DB,
) -> np.ndarray:
    """按源人声的**段落级**包络生成增益曲线（线性，长度 = 输入）。

    只做 8s 平滑：源里的音节内动态（颤音、渐强）已经原样在换声输出里了，
    再按 50ms 包络推一遍 = 把动态**平方**，听感会“抽气”。这里要的是
    主歌收一点、副歌推一点，所以窗口必须远大于乐句。
    """
    x = np.asarray(src_mono, dtype=np.float64)
    if x.size == 0:
        return np.ones(0, dtype=np.float32)
    from scipy.ndimage import uniform_filter1d

    win = max(1, int(round(window_s * sr)))
    env = uniform_filter1d(x ** 2, size=min(win, x.size), mode="nearest")
    db = 10.0 * np.log10(env + 1e-12)
    active = env > np.percentile(env, 20)
    ref = float(np.median(db[active])) if active.any() else float(np.median(db))
    db = np.clip(db - ref, -max_db, max_db)
    gain = (10.0 ** (db / 20.0)).astype(np.float32)
    # 曲线本身已经 8s 平滑；再做 0.5s 平滑只为消除 float 抖动/边界台阶
    return uniform_filter1d(gain, size=max(1, int(0.5 * sr)), mode="nearest").astype(np.float32)


def compressor(
    x: np.ndarray,
    sr: int = SR,
    *,
    threshold_db: float = -24.0,
    ratio: float = 4.0,
    attack_ms: float = 8.0,
    release_ms: float = 120.0,
    makeup_db: float = 3.0,
    frame_ms: float = 10.0,
) -> np.ndarray:
    """帧级前馈压缩（10ms 帧 + attack/release 平滑 + 线性插值到采样）。

    纯 numpy，11M 采样也只循环约 25k 帧。用途是**并行副本**：
    压扁的那一轨提供密度，原轨保留动态，所以这里不需要侧链/lookahead。
    """
    y = np.asarray(x, dtype=np.float64)
    if y.size == 0:
        return y.astype(np.float32)
    hop = max(1, int(round(sr * frame_ms / 1000.0)))
    n = max(1, y.size // hop)
    fr = y[: n * hop].reshape(n, hop)
    rms = np.sqrt((fr ** 2).mean(axis=1))
    db = 20.0 * np.log10(rms + 1e-12)
    over = np.maximum(0.0, db - threshold_db)
    target_db = -over * (1.0 - 1.0 / max(1e-6, ratio)) + makeup_db

    a_att = math.exp(-frame_ms / max(1e-6, attack_ms))
    a_rel = math.exp(-frame_ms / max(1e-6, release_ms))
    smooth = np.empty(n, dtype=np.float64)
    cur = 0.0
    for i in range(n):
        coeff = a_att if target_db[i] < cur else a_rel
        cur = coeff * cur + (1.0 - coeff) * target_db[i]
        smooth[i] = cur
    gain = 10.0 ** (smooth / 20.0)
    # 帧中心对齐后插值到每个采样；尾部用最后一个增益
    centers = np.arange(n) * hop + hop / 2.0
    g = np.interp(np.arange(y.size), centers, gain, left=gain[0], right=gain[-1])
    return (y * g).astype(np.float32)


def parallel_density(x: np.ndarray, sr: int = SR, amount: float = DENSITY_AMOUNT) -> np.ndarray:
    """并行压缩 + 软饱和：原轨 + amount × 压扁/饱和副本。

    `tanh` 只给偶次/奇次谐波里的“颗粒”，不是失真墙；amount 0.13 约 −18dB。
    """
    y = np.asarray(x, dtype=np.float32)
    if y.size == 0:
        return y
    c = compressor(y, sr)
    sat = np.tanh(c * 1.8) / math.tanh(1.8)
    out = y.astype(np.float64) + amount * sat.astype(np.float64)
    peak = float(np.abs(out).max()) if out.size else 0.0
    if peak > MIX_CEILING:
        out *= MIX_CEILING / peak
    return out.astype(np.float32)


def reverb(vocal_mono: np.ndarray, ir: np.ndarray, wet: float = REVERB_WET) -> np.ndarray:
    """干声 + 湿声卷积，返回立体声 (n, 2)。干声保持在正中。"""
    from scipy.signal import fftconvolve

    v = np.asarray(vocal_mono, dtype=np.float32)
    if v.size == 0:
        return np.zeros((0, 2), dtype=np.float32)
    out = np.empty((v.size, 2), dtype=np.float32)
    for ch in range(2):
        ir_ch = np.asarray(ir[:, ch], dtype=np.float32)
        w = fftconvolve(v, ir_ch)[: v.size]
        out[:, ch] = v + wet * np.asarray(w, dtype=np.float32)
    return out


def widen(stereo: np.ndarray, sr: int = SR, *, delays_ms=WIDE_DELAY_MS,
          amount: float = WIDE_AMOUNT) -> np.ndarray:
    """Haas 双轨宽化：左右各叠一条短延迟的对侧信号。

    ★ 这是“探边界”的加工：单声道兼容性会变差（手机外放/蓝牙音箱可能变薄），
    所以只放在 F 臂，不默认进产品。
    """
    x = np.asarray(stereo, dtype=np.float32)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    if x.size == 0:
        return x
    left, right = x[:, 0].copy(), x[:, 1].copy()
    for src_ch, dst, ms in ((right, left, delays_ms[0]), (left, right, delays_ms[1])):
        d = int(round(sr * ms / 1000.0))
        if 0 < d < len(src_ch):
            dst[d:] += amount * src_ch[:-d]
    return np.stack([left, right], axis=1)


def mix_stems(vocal_stereo: np.ndarray, acc_stereo: np.ndarray,
              vocal_gain: float = VOCAL_GAIN, acc_gain: float = ACC_GAIN,
              ceiling: float = MIX_CEILING) -> tuple[np.ndarray, float]:
    """按反解的增益相加；只在超顶时整体等比缩（与 `cover_api.mix_back` 同策略）。"""
    n = min(len(vocal_stereo), len(acc_stereo))
    v = np.asarray(vocal_stereo[:n], dtype=np.float64)
    a = np.asarray(acc_stereo[:n], dtype=np.float64)
    out = vocal_gain * v + acc_gain * a
    peak = float(np.abs(out).max()) if out.size else 0.0
    scale = 1.0
    if peak > ceiling:
        scale = ceiling / peak
        out *= scale
    return out.astype(np.float32), float(scale)


# ---------------------------------------------------------------- I/O

def _ffmpeg() -> str:
    from common import find_ffmpeg

    return find_ffmpeg()


def read_audio(path: Path, sr: int = SR, *, stereo: bool = False) -> np.ndarray:
    from math import gcd

    import soundfile as sf
    from scipy.signal import resample_poly

    d, src_sr = sf.read(str(path), dtype="float32", always_2d=True)
    if d.shape[1] == 1 and stereo:
        d = np.repeat(d, 2, axis=1)
    if d.shape[1] > 1 and not stereo:
        d = d.mean(axis=1, keepdims=True)
    if src_sr != sr:
        g = gcd(sr, src_sr)
        d = resample_poly(d, sr // g, src_sr // g, axis=0).astype(np.float32)
    return d if stereo else d[:, 0]


def write_wav(path: Path, data: np.ndarray, sr: int = SR) -> None:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    x = np.asarray(data, dtype=np.float32)
    peak = float(np.abs(x).max()) if x.size else 0.0
    if peak > MIX_CEILING:
        x = x * (MIX_CEILING / peak)
    sf.write(str(path), x, sr, subtype="PCM_16")


# ---------------------------------------------------------------- 指标

def _rms_env_db(y: np.ndarray, sr: int, win_s: float = 3.0, hop_s: float = 0.5) -> np.ndarray:
    win = max(1, int(win_s * sr))
    hop = max(1, int(hop_s * sr))
    if len(y) < win:
        return np.array([10 * np.log10(float(np.mean(y ** 2)) + 1e-12)])
    frames = np.lib.stride_tricks.sliding_window_view(y, win)[::hop]
    return 10 * np.log10((frames ** 2).mean(axis=1) + 1e-12)


def analyze(vocal_stem: np.ndarray, mix: np.ndarray, acc: np.ndarray,
            src_vocal: np.ndarray, wet_db: float | None = None, sr: int = SR) -> dict:
    """一条臂的客观读数。全部是“漂移监测”，不用于宣布谁更好。"""
    vmono = vocal_stem.mean(axis=1) if vocal_stem.ndim > 1 else vocal_stem
    mmono = mix.mean(axis=1) if mix.ndim > 1 else mix
    amono = acc.mean(axis=1) if acc.ndim > 1 else acc

    ev = _rms_env_db(vmono, sr)
    em = _rms_env_db(mmono, sr)
    ea = _rms_env_db(amono, sr)
    es = _rms_env_db(src_vocal, sr)
    n = min(len(ev), len(ea), len(em), len(es))
    ev, ea, em, es = ev[:n], ea[:n], em[:n], es[:n]
    active = es > (np.percentile(es, 20) + 3.0)
    v2a = ev - ea
    out = {
        "lra": round(float(np.percentile(em, 95) - np.percentile(em, 10)), 1),
        "dyn_std": round(float(em.std()), 1),
        "v2a_std": round(float(v2a[active].std()), 2) if active.any() else None,
        "corr_src": round(float(np.corrcoef(es[active], em[active])[0, 1]), 3) if active.sum() > 5 else None,
        # 混响发送：湿声 RMS / 干声 RMS（−11dB = 0.28 线性）。这是构造值，
        # 用来证明“空间臂真的加了空间”；换声干声自己的尾巴不适合当尺子
        # （pyin 的 voiced→unvoiced 多发生在字内辅音，不是乐句尾，2026-10-18 实测）。
        "wet": wet_db,
        "clip": round(float(np.mean(np.abs(mix) >= 0.999) * 100), 3),
        "crest": round(float(20 * np.log10((np.abs(mmono).max() + 1e-12) / (np.sqrt(np.mean(mmono ** 2)) + 1e-12))), 1),
    }
    # ★ 必须传 SR：`_band_and_sfm_path` 的第二个参数是**输入采样率**，内部才重采样。
    #   2026-10-18 首跑传了 22050，把 44.1k 的数组当 22.05k 算 → 8–12k 被读成
    #   16–24k，A 臂的 band_8k 从旧页的 −45.9 变成 −53.0（假红 7dB）。
    b8, sfm, cen = _band_and_sfm_path(mmono, SR)
    out.update({"band_8k": b8, "sfm": sfm, "centroid": cen})
    if mix.ndim > 1:
        mid = (mix[:, 0] + mix[:, 1]) / 2.0
        side = (mix[:, 0] - mix[:, 1]) / 2.0
        ratio = float(20 * np.log10((np.sqrt(np.mean(side ** 2)) + 1e-12)
                                    / (np.sqrt(np.mean(mid ** 2)) + 1e-12)))
        # 源/伴奏本身是单声道（旧成品左右逐样本相同）→ 报 None，页面写“单声道”，
        # 不要把 −220dB 这种“数值上正确、读起来像 bug”的东西印给用户。
        out["side_mid"] = None if ratio < -60 else round(ratio, 1)
    return out


def _band_and_sfm_path(y: np.ndarray, sr: int) -> tuple[float, float, float]:
    """在内存数组上算 8–12k / 谱平坦度 / 质心（与既有页同口径，sr 固定 22050）。"""
    import librosa

    if sr != 22050:
        y = librosa.resample(np.asarray(y, dtype=np.float32), orig_sr=sr, target_sr=22050)
    y = np.asarray(y, dtype=np.float32)
    win, hop = 2048, 1024
    frames = np.lib.stride_tricks.sliding_window_view(y, win)[::hop]
    energy = (frames ** 2).sum(axis=1)
    frames = frames[energy > np.percentile(energy, 30)]
    spec = np.abs(np.fft.rfft(frames * np.hanning(win), axis=1)).mean(axis=0)
    freqs = np.fft.rfftfreq(win, 1 / 22050)
    ref = (spec[(freqs >= 20) & (freqs < 4000)] ** 2).sum() + 1e-12
    b8 = 10 * np.log10(((spec[(freqs >= 8000) & (freqs < 12000)] ** 2).sum() + 1e-12) / ref)
    sfm = float(np.exp(np.mean(np.log(spec + 1e-12))) / (np.mean(spec) + 1e-12))
    centroid = float((spec * freqs).sum() / (spec.sum() + 1e-12))
    return round(float(b8), 1), round(sfm, 4), round(centroid, 0)


# ---------------------------------------------------------------- 臂

ARMS = [
    ("A_control", "① 现役成品（对照）", "不改任何东西：你的「颜色」基线", {}),
    ("B_space", "② 空间 · 2.0s 板式混响", "给干声一个厅堂尾巴；情感常缺的是「余韵」",
     {"reverb": True}),
    ("C_breath", "③ 呼吸 · 段落推拉 ±2dB", "按源的 8s 包络：主歌收、副歌推；不动音节内",
     {"automation": True}),
    ("D_space_auto", "④ 空间 + 呼吸", "两条一起上：有空间、有推收",
     {"reverb": True, "automation": True}),
    ("E_density", "⑤ ④ + 密度", "并行压缩/软饱和：更贴耳、有颗粒",
     {"reverb": True, "automation": True, "density": True}),
    ("F_wide", "⑥ ⑤ + 宽化", "双轨延迟：更宽更大；探过界（单声道会变薄）",
     {"reverb": True, "automation": True, "density": True, "wide": True}),
]


def run_arm(vocal_mono: np.ndarray, acc: np.ndarray, ir: np.ndarray,
            src: np.ndarray, opts: dict) -> tuple[np.ndarray, np.ndarray, float | None]:
    """返回 (人声 stem 立体声, 混音立体声, 混响发送 dB)。

    处理顺序：推拉 → 密度 → 空间 → 宽化。`wet_db` 是湿声相对**进混响前**干声的
    电平（−11dB = 0.28 线性），与 `REVERB_WET` 对账用。
    """
    v = np.asarray(vocal_mono, dtype=np.float32).copy()
    if opts.get("automation"):
        v *= automation_gain(src, SR)
    if opts.get("density"):
        v = parallel_density(v, SR)
    wet_db = None
    if opts.get("reverb"):
        from scipy.signal import fftconvolve

        wet_parts = []
        for ch in range(2):
            wet_parts.append(fftconvolve(v, np.asarray(ir[:, ch], dtype=np.float32))[: len(v)])
        wet = np.stack(wet_parts, axis=1).astype(np.float32)
        dry_rms = float(np.sqrt(np.mean(v ** 2) + 1e-12))
        wet_rms = float(np.sqrt(np.mean(wet ** 2) + 1e-12))
        wet_db = round(20 * np.log10((REVERB_WET * wet_rms + 1e-12) / (dry_rms + 1e-12)), 1)
        v_st = np.stack([v, v], axis=1) + REVERB_WET * wet
    else:
        v_st = np.stack([v, v], axis=1)
    if opts.get("wide"):
        v_st = widen(v_st, SR)
    mix, _scale = mix_stems(v_st, acc)
    return v_st, mix, wet_db


def run_all() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    vocal_mono = read_audio(VOCAL, SR, stereo=False)
    acc = read_audio(ACC, SR, stereo=True)
    src = read_audio(BASE / "原曲_人声.wav", SR, stereo=False)
    n = min(len(vocal_mono), len(acc), len(src))
    vocal_mono, acc, src = vocal_mono[:n], acc[:n], src[:n]
    print(f"[输入] 干声 {len(vocal_mono)/SR:.1f}s · 伴奏 {len(acc)/SR:.1f}s · 源 {len(src)/SR:.1f}s")

    ir = make_ir()
    arms: dict[str, str] = {}
    metrics: dict[str, dict] = {}
    for key, title, _sub, opts in ARMS:
        v_st, mix, wet_db = run_arm(vocal_mono, acc, ir, src, opts)
        vp = OUT / f"{key}_vocal.wav"
        mp = OUT / f"{key}_mix.wav"
        write_wav(vp, v_st)
        write_wav(mp, mix)
        arms[key] = str(mp)
        metrics[key] = analyze(v_st, mix, acc, src, wet_db)
        print(f"[{key}] {title} · " + json.dumps(metrics[key], ensure_ascii=False))
    (OUT / "arms.json").write_text(json.dumps(arms, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
    return arms


# ---------------------------------------------------------------- 页面

def build_page(arms: dict[str, str], metrics: dict[str, dict]) -> Path:
    ff = _ffmpeg()
    parts: list[str] = []
    for key, title, sub, _opts in ARMS:
        p = Path(arms[key])
        if not p.exists():
            parts.append(f'<div class="card"><h2>{title}</h2><p class="meta">{sub}</p><span class="bad">（缺文件）</span></div>')
            continue
        tmp = OUT / f".{key}.page.mp3"
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(p), "-b:a", "128k", str(tmp)],
                       check=True, timeout=300)
        b64 = "data:audio/mpeg;base64," + base64.b64encode(tmp.read_bytes()).decode()
        tmp.unlink(missing_ok=True)
        m = metrics.get(key, {})
        side = m.get("side_mid")
        side_txt = "单声道" if side is None else f"{side}dB"
        wet = m.get("wet")
        wet_txt = "—" if wet is None else f"{wet}dB"
        note = (f"LRA {m.get('lra')}dB · 动态std {m.get('dyn_std')}dB · "
                f"与源动态相关 {m.get('corr_src')} · 混响发送 {wet_txt} · 8–12k {m.get('band_8k')}dB · "
                f"SFM {m.get('sfm')} · 质心 {m.get('centroid')}Hz · 声场 {side_txt} · 削波 {m.get('clip')}%")
        parts.append(f'<div class="card"><h2>{title}</h2><p class="meta">{sub}<br>{note}</p>'
                     f'<audio controls preload="metadata" src="{b64}"></audio></div>')

    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>《好久不见》· supp2026 表现力对照（人声+伴奏）</title>
<style>
:root{--bg:#0e1014;--card:#161a21;--line:#242a35;--fg:#e8ecf2;--dim:#98a2b3;--acc:#6aa9ff;--ok:#48d18a;--warn:#ffb454}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:34px 20px 72px}
h1{font-size:24px;margin:0 0 6px}p.sub{color:var(--dim);margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:14px 0}
.card h2{font-size:16px;margin:0 0 3px;font-weight:600}
.meta{color:var(--dim);font-size:13px;margin:0 0 10px}
audio{width:100%;height:40px}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-top:6px}
th,td{text-align:right;padding:7px 8px;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
.note{color:var(--dim);font-size:13px;margin:10px 0 0}
.bad{color:#ff7b72;font-size:13px}.warn{color:var(--warn)}.ok{color:var(--ok)}
</style></head><body><div class="wrap">
<h1>《好久不见》· supp2026 表现力对照（都是成品：人声+伴奏）</h1>
<p class="sub">你的裁决已生效：<b>supp2026 更像你、更不 AI；t03 弃用</b>。颜色不变，这一页只加「情感/表现力」。</p>
<p class="sub">先量了换声干声：<b>动态 15.9dB（源 13.9）、气声占比 0.093（源 0.092）、颤音 0.55 半音（源 0.52）</b>
—— 表演没有被压平，所以本页不动模型、不重训，只加成品端的四件事：<b>空间、段落推拉、密度、宽化</b>。</p>
<p class="sub">听的时候只问一句：<b>哪一版更像「一个人在对你唱」</b>，而不是「哪一版更亮」。②③④ 是保守档，⑤⑥ 探边界。</p>
<p class="sub warn">⚠️ 已知边界：混响/宽化会降低「像你」（CAM++ 在本项目已被证明会被加工骗），所以本页不排序、只报漂移；⑤⑥ 若听出「糊/薄/合唱感」就退回 ④。</p>
__BODY__
<footer class="note">2026-10-18 · experiments/cover_emotion_chain.py · 基线 supp2026 +6dB@7k + 气口 · 混音增益由旧成品反解（0.9545/0.7071）· 全曲 250s</footer>
</div></body></html>"""
    dst = OUT / "listen_表现力.html"
    dst.write_text(html.replace("__BODY__", "\n".join(parts)), encoding="utf-8")
    print(f"[页] {dst}（{dst.stat().st_size / 1e6:.1f} MB）")
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["run", "page", "all"], nargs="?", default="all")
    a = ap.parse_args()
    import contextlib

    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    arms: dict[str, str] = {}
    metrics: dict[str, dict] = {}
    if a.step in ("run", "all"):
        arms = run_all()
        metrics = json.loads((OUT / "metrics.json").read_text(encoding="utf-8"))
    if a.step in ("page", "all"):
        if not arms:
            arms = json.loads((OUT / "arms.json").read_text(encoding="utf-8"))
            metrics = json.loads((OUT / "metrics.json").read_text(encoding="utf-8"))
        build_page(arms, metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
