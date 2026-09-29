# -*- coding: utf-8 -*-
"""嗓音客观指标提取（HNR / H1-H2 / jitter / shimmer / 谱倾斜）。

为什么有这个模块（2026-09-29）：
    调研 `docs/调研-SVC路线与嗓音客观指标-2026-09-28.md` 第 4.2 节定的口径 ——
    「夹嗓子」（vocal strain）在语音学里有可测量的声学代理。本模块把三灯里
    「自然度」与「夹嗓子风险」两个灯所需的原始测量值算出来，别家 SVC 产品基本不给。

★★ 关于 CPP（倒谱峰突出度）—— 已评估，**本模块不提供**，这是有意的：
    调研把 CPP 列为「整体嗓音质量首选指标」，但本机实测无法可靠复现：
    Praat 的 `Get CPPS` 通过 parselmouth 调用时参数签名探测不到（`Data` 对象
    不暴露矩阵，只能靠 praat 命令取；逐个试参数在 13 个位置仍报
    `right Peak search pitch range` 类型错，说明 left/right 成对参数前缀的
    排布与桌面 Praat 的脚本接口不同）。
    自实现的两个版本也都不成立，**有实测证据**（2026-09-29）：
      - 版本 A（峰值 - 低分位均值）：纯正弦 1.68 / 白噪声 0.59，区分度仅 3 倍；
      - 版本 B（峰值 - 线性回归趋势，1ms 起算）：纯正弦 0.65 / 白噪声 0.59，
        **区分度消失** —— 正弦的倒谱能量集中在窗泄漏的直流尖峰（实测 0 号索引
        幅值 2.05，是 0.14ms 处的 2.9 倍），真正的周期峰（150Hz→6.7ms）
        在 1~15ms 窗内根本没被抬起来。
    结论：CPP 的正确实现依赖 Praat 内部的对数谱/窗补偿细节，逆向成本超过收益。
    **宁可少一个灯的数据，也不要一个量级错误、跨样本不可比的假指标。**
    将来若要补，正确路径是读到 Praat 官方 `cpp.praat` 插件源码后再实现，
    并必须用「纯正弦 vs 白噪声应拉开 5 倍以上」作为验收前提（本模块的测试
    就是这么写的，见 tests/test_voice_metrics.py）。

设计约束（都不是随手选的）：
    1. **零新依赖**：`praat_parselmouth 0.4.7` 本机 `.venv` 已装
       （`tools/voice_qc.py` 早就在用它算 F0 中位数）。
    2. **绝不裸崩**：任何指标算不出来 → 返回 None，绝不抛。
       `rvc_live` 训练完成回调依赖「QC 永不裸崩」这个约定。
    3. **纯函数、无全局状态**：便于单测直接喂合成信号断言
       （见 `tests/test_voice_metrics.py`，用正弦/噪声/模拟挤压音标定）。
    4. **谱倾斜口径对齐 `experiments/svc_timbre_diag.py` 的 `tilt`**
       （log2 频率 vs dB 谱的回归斜率，低频起点 100Hz）——同一批数字能横向比。
    5. **与 `voice_qc.py` 的分工**：这里只出**原始测量值**，不做打分、
       不判 PASS/FAIL、不知道什么是「三灯」。阈值与分档在 `voice_qc.py` 里。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

# Praat 调用默认参数。集中在这里，方便标定时一处改。
PITCH_FLOOR = 60.0
PITCH_CEILING = 800.0
TIME_STEP = 0.01

# 有效浊音段最少帧数：太少则统计量不可信（一个 0.1s 的「啊」不该给出结论）
MIN_VOICED_FRAMES = 10

# 谱倾斜的拟合频带（对齐 svc_timbre_diag.py：100Hz 起步）
TILT_F_LO = 100.0
TILT_F_HI = 4000.0

# 静音帧门限（RMS）。低于它的帧不参与统计 —— 静音段的噪声会把所有指标拖歪，
# 这是 2026-09-29 实测踩到的坑（HNR 全段平均得 -33dB，只在浊音帧上取才是 13.3dB）。
SILENCE_RMS = 1e-4


def _import_parselmouth():
    """延迟导入：本模块可能被「安装版后端」导入，而那边不一定装了 parselmouth。

    ⚠️ 这个 try/except 不只是"规范好看"，它是**依赖归属门禁认得出来的唯一形式**：
    `tools/audit_plugin_deps.py` 用 AST 判断一条 import 是否被
    ImportError/ModuleNotFoundError/Exception 兜住，兜住的算 optional、不兜的算必需。
    parselmouth 在本项目是**可选**依赖（安装版后端不保证装了它，`voice_qc.py`
    还有一条"回退 RVC venv 子进程"的兜底就是为这件事写的），所以这里必须兜住 ——
    否则 `clips_api` import 本模块会把 parselmouth 拉成运行时必需依赖，门禁报红
    （2026-09-29 实测：去掉这个 try 后 `[ownership]` 失败，undeclared = parselmouth）。
    """
    try:
        import parselmouth

        return parselmouth
    except ImportError as e:  # pragma: no cover - 环境相关
        raise ImportError(f"parselmouth 不可用: {e}") from e


def _praat_call():
    """拿到 `parselmouth.praat.call`。

    ⚠️ 全模块**只在这里** import praat —— 这是依赖归属门禁的要求，也是单一入口：
    `tools/audit_plugin_deps.py` 用 AST 判断「一条 import 是否被 ImportError 兜住」，
    而且**同一个模块被「兜住」和「没兜住」各引一次时按没兜住算**（更严）。
    2026-09-29 实测：三个内部函数里各写一次裸 `from parselmouth.praat import call`，
    会让 parselmouth 被判成**必需**依赖、`[ownership]` 门禁失败 ——
    尽管 `_import_parselmouth` 那处已经兜住了。收敛到这一个入口就解决了。
    """
    try:
        from parselmouth.praat import call

        return call
    except ImportError as e:  # pragma: no cover - 环境相关
        raise ImportError(f"parselmouth.praat 不可用: {e}") from e


def metrics_available() -> tuple[bool, str]:
    """(可用?, 原因)。给上层决定是降级还是报错 —— 而不是 try/except ImportError。"""
    try:
        _import_parselmouth()
        return True, ""
    except Exception as e:  # pragma: no cover - 环境相关
        return False, f"parselmouth 不可用: {type(e).__name__}: {e}"


def _load_mono(path: str | Path):
    """读成单声道 float64 numpy，附带 sr。返回 (x, sr) 或 (None, None)。"""
    import soundfile as sf

    d, sr = sf.read(str(path), dtype="float64")
    if d.ndim > 1:
        d = d.mean(axis=1)
    return d.astype(np.float64, copy=False), int(sr)


def _to_mono_snd(snd):
    """parselmouth.Sound 转单声道（H1-H2 / 谱倾斜只对单一声道有意义）。"""
    if snd.n_channels > 1:
        return snd.extract_channel(1)
    return snd


def _hnr_db(snd) -> float | None:
    """谐噪比（dB），只在浊音帧上取中位数。

    ★ 这里有两个实测踩过的坑（2026-09-29），都不是理论问题：
      1. `To Harmonicity (cc)` 的返回值**已经是 dB**（值域 -200 ~ +27），
         不是 0~1 的比例。早期版本按比例做 `10*log10(v/(1-v))` 直接得 nan。
      2. 值域里的 **-200 是「该帧无浊音」的哨兵值**，不是测量结果。
         对全段取 mean() 会被这些哨兵拖到 -33dB（实测），毫无意义。
         正确做法：按 F0 的浊音掩码筛选，再取中位数（实测 13.28dB，正常）。
    """
    call = _praat_call()

    try:
        pitch = snd.to_pitch(time_step=TIME_STEP,
                             pitch_floor=PITCH_FLOOR,
                             pitch_ceiling=PITCH_CEILING)
        f0 = pitch.selected_array["frequency"]
        h = call(snd, "To Harmonicity (cc)", TIME_STEP, PITCH_FLOOR, 0.1, 1.0)
        hv = np.asarray(h.values, dtype=float).ravel()
        n = min(len(f0), len(hv))
        if n == 0:
            return None
        voiced = hv[:n][f0[:n] > 0]
        voiced = voiced[np.isfinite(voiced) & (voiced > -100)]  # 掉 -200 哨兵
        if len(voiced) < 3:
            return None
        return round(float(np.median(voiced)), 3)
    except Exception:
        return None


def _pitch_and_pp(snd):
    """返回 (f0数组, voiced_mask, point_process 或 None)。任一步失败给空。"""
    call = _praat_call()

    try:
        pitch = snd.to_pitch(time_step=TIME_STEP,
                             pitch_floor=PITCH_FLOOR,
                             pitch_ceiling=PITCH_CEILING)
        f0 = np.asarray(pitch.selected_array["frequency"], dtype=float)
    except Exception:
        return np.array([]), np.array([], dtype=bool), None
    mask = f0 > 0
    pp = None
    try:
        pp = call(snd, "To PointProcess (periodic, cc)",
                  PITCH_FLOOR, PITCH_CEILING)
    except Exception:
        pp = None
    return f0, mask, pp


def _jitter_shimmer(snd, pp) -> tuple[float | None, float | None]:
    """局部 jitter / shimmer（%，越低越稳）。"""
    call = _praat_call()

    if pp is None:
        return None, None
    jit = shim = None
    try:
        jit = round(float(call(pp, "Get jitter (local)",
                               0, 0, 0.0001, 0.02, 1.3)) * 100.0, 4)
    except Exception:
        pass
    try:
        shim = round(float(call([snd, pp], "Get shimmer (local)",
                                0, 0, 0.0001, 0.02, 1.3, 1.6)) * 100.0, 4)
    except Exception:
        pass
    return jit, shim


def _harmonic_amps(x: np.ndarray, sr: int, f0: float) -> tuple[float, float] | None:
    """F0 与 2*F0 处的幅度（dB，Welch 平均谱上取 ±15% 窗内最大值）。"""
    try:
        if not f0 or f0 <= 0 or len(x) < 512:
            return None
        n = 4096
        if len(x) < n:
            n = 1 << int(np.floor(np.log2(max(len(x), 1))))
        if n < 512:
            return None
        w = np.hanning(n)
        acc = np.zeros(n // 2 + 1)
        cnt = 0
        for st in range(0, len(x) - n + 1, n // 2):
            seg = x[st:st + n] * w
            if float(np.sqrt(np.mean(seg ** 2))) < SILENCE_RMS:
                continue
            acc += np.abs(np.fft.rfft(seg))
            cnt += 1
        if cnt == 0:
            return None
        spec = acc / cnt
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        def _amp_at(target: float) -> float | None:
            if target >= freqs[-1]:
                return None
            band = (freqs >= target * 0.85) & (freqs <= target * 1.15)
            if not np.any(band):
                return None
            v = float(np.max(spec[band]))
            return 20.0 * math.log10(v) if v > 0 else None

        a1, a2 = _amp_at(f0), _amp_at(2.0 * f0)
        if a1 is None or a2 is None:
            return None
        return a1, a2
    except Exception:
        return None


def _h1_h2(x: np.ndarray, sr: int, f0_median: float | None) -> float | None:
    """H1-H2（dB）—— 第一、二谐波幅度差，phonation type 判据。

    ★ 方向已用合成信号标定（2026-09-29，tests/test_voice_metrics.py 守着）：
      对「每谐波降 N dB」的合成音，H1-H2 与 N **线性同向**：
        每谐波 -2dB → H1-H2 ≈ 2.3dB（气声/leaky，谱缓降）
        每谐波 -6dB → H1-H2 ≈ 6.3dB（自然发声）
        每谐波 -12dB → H1-H2 ≈ 12.3dB（挤压/pressed，谱陡降）
      即 **H1-H2 越大 = 高频滚降越快 = 越「挤压」**。
      ⚠️ 这里与调研文档 4.2 节写的「挤压 → H1-H2 变低」**方向相反**。
      调研那句是转述文献的通用表述，而文献里的符号依赖 H1/H2 的相位与
      Praat 的取幅约定；本机口径以**合成信号实测的单调性**为准（可复现、
      有测试）。三灯打分在 `voice_qc.py` 里按「越大越危险」处理，若将来
      换了取幅方式，这个方向必须重新标定 —— 否则灯会反着亮。
    """
    if not f0_median or f0_median <= 0:
        return None
    amps = _harmonic_amps(x, sr, float(f0_median))
    if amps is None:
        return None
    a1, a2 = amps
    return round(a1 - a2, 3)


def _spectral_tilt(x: np.ndarray, sr: int) -> float | None:
    """谱倾斜（dB/octave）—— 口径对齐 `experiments/svc_timbre_diag.py` 的 tilt：
    log2 频率 vs 10*log10(功率谱) 的线性回归斜率，拟合带 100Hz~4kHz。

    越负 = 高频衰减越快 = 更「挤压/pressed」。与 H1-H2 同向，可互相印证。
    """
    try:
        n = 4096
        if len(x) < n:
            n = 1 << int(np.floor(np.log2(max(len(x), 1))))
        if n < 512:
            return None
        w = np.hanning(n)
        acc = np.zeros(n // 2 + 1)
        cnt = 0
        for st in range(0, len(x) - n + 1, n // 2):
            seg = x[st:st + n] * w
            if float(np.sqrt(np.mean(seg ** 2))) < SILENCE_RMS:
                continue
            acc += np.abs(np.fft.rfft(seg)) ** 2
            cnt += 1
        if cnt == 0:
            return None
        psd = acc / cnt
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        band = ((freqs >= TILT_F_LO) & (freqs <= TILT_F_HI) & (psd > 0))
        if np.count_nonzero(band) < 10:
            return None
        lf = np.log2(freqs[band] / TILT_F_LO)
        db = 10.0 * np.log10(psd[band] + 1e-12)
        return round(float(np.polyfit(lf, db, 1)[0]), 3)
    except Exception:
        return None


def extract(path: str | Path) -> dict:
    """提一组嗓音指标。任何单项失败写 None，整体不抛。

    返回键：
        hnr           谐噪比 dB（浊音帧中位数；正常说话约 10~20）
        h1_h2         H1-H2 dB（越大越「挤压」，见 _h1_h2 的方向说明）
        spectral_tilt 谱倾斜 dB/oct（越负越「挤压」，同 svc_timbre_diag 口径）
        jitter_local  局部基频微扰 %（越低越稳）
        shimmer_local 局部振幅微扰 %（越低越稳）
        voiced_ratio  浊音帧占比
        voiced_frames 有效浊音帧数
        f0_median     浊音段 F0 中位数 Hz
        duration_s    时长
        error         整体性失败时的原因（单项失败不进这里）
    """
    res: dict = {
        "hnr": None, "h1_h2": None, "spectral_tilt": None,
        "jitter_local": None, "shimmer_local": None,
        "voiced_ratio": None, "voiced_frames": 0,
        "f0_median": None, "duration_s": None, "error": None,
    }
    p = Path(path)
    if not p.exists():
        res["error"] = f"文件不存在: {p}"
        return res

    ok, why = metrics_available()
    if not ok:
        res["error"] = why
        return res

    parselmouth = _import_parselmouth()
    try:
        snd = parselmouth.Sound(str(p))
    except Exception as e:
        res["error"] = f"读取音频失败: {type(e).__name__}: {e}"
        return res

    try:
        snd = _to_mono_snd(snd)
        res["duration_s"] = round(float(snd.get_total_duration()), 3)
    except Exception as e:
        res["error"] = f"时长读取失败: {type(e).__name__}: {e}"
        return res

    # Praat 侧：F0 / 浊音比例 / jitter / shimmer / HNR
    f0, mask, pp = _pitch_and_pp(snd)
    if len(f0):
        res["voiced_frames"] = int(np.count_nonzero(mask))
        res["voiced_ratio"] = round(float(np.count_nonzero(mask) / len(f0)), 4)
        voiced = f0[mask]
        if len(voiced):
            res["f0_median"] = round(float(np.median(voiced)), 2)
    jit, shim = _jitter_shimmer(snd, pp)
    res["jitter_local"], res["shimmer_local"] = jit, shim
    res["hnr"] = _hnr_db(snd)

    # numpy 侧：H1-H2 / 谱倾斜
    try:
        x, sr = _load_mono(p)
        res["h1_h2"] = _h1_h2(x, sr, res.get("f0_median"))
        res["spectral_tilt"] = _spectral_tilt(x, sr)
    except Exception:
        pass

    return res
