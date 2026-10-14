# -*- coding: utf-8 -*-
"""`experiments/cover_tilt_*.py`（训练集预加重治闷）的守卫。

守什么 —— 这条实验有两个**静默失效**的地方，两种都会给出"预加重没用"的假结论：

1. **预加重没生效**：斜率写反 / 抬错频段 / 被峰值缩放抹平。训练日志里完全看不出来，
   只会得到"三档都不行" ⇒ `cover_tilt_verify.py` 必须先证明 2kHz 以下不动、
   3.2–8k 单调抬升。这条把它钉住。
2. **只看相对频段 = 可能只是变响**：`cover_metrics` 的 `band_db` 以 0-4k 归一化，
   一个整体更响的产物也会"看起来更亮"。`cover_tilt_absbands.abs_bands` 用绝对 dB，
   并要求"扣掉总电平后的净变化"。这条钉住"绝对口径里也确实是高频涨了"。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "m2_server", _ROOT / "experiments"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tilt():
    return _load("cover_tilt_train_t", _ROOT / "experiments" / "cover_tilt_train.py")


@pytest.fixture(scope="module")
def absb():
    return _load("cover_tilt_absbands_t", _ROOT / "experiments" / "cover_tilt_absbands.py")


# ---------------------------------------------------------------- 预加重本身


def test_preemphasis_leaves_low_band_alone_and_lifts_high(tilt):
    """★ 核心：2kHz 是 pivot（以下不动），以上必须**单调**抬升。

    ★ 口径（2026-10-14 校准，第一版写错了两处）：
      · **不量单帧的频段占比** —— 预加重把 pivot 以上抬起来后，占比的分母也变大，
        于是"pivot 以下"的占比会**下降**（实测 300-800Hz 从 −15.60 掉到 −22.52），
        那是分母效应、不是"把低频改了"。
      · 正确做法：量**高频带 ÷ 低频带**的比值变化 —— pivot 以下不动时，
        这个比值应当严格按照预加重斜率上升。

    本条期望（实测）：5-8k/300-800 的比值在 +3 档约 +14 dB、+6 档约 +21 dB，
    且 +6 > +3（单调）。
    """
    sr = 40000
    t = np.arange(sr * 2) / sr
    rng = np.random.default_rng(0)
    x = (0.3 * rng.standard_normal(len(t))).astype(np.float32)

    def band_ratio(y, lo_hi, hi_hi):
        n = 2048
        freqs = np.fft.rfftfreq(n, 1 / sr)
        P = np.abs(np.fft.rfft(y[:n] * np.hanning(n))) ** 2
        lo = float(P[(freqs >= lo_hi[0]) & (freqs < lo_hi[1])].sum())
        hi = float(P[(freqs >= hi_hi[0]) & (freqs < hi_hi[1])].sum())
        return 10 * float(np.log10(hi / (lo + 1e-30) + 1e-30))

    y0 = tilt.preemphasize(x, sr, 0.0)
    assert np.allclose(y0, x), "0 dB/oct 必须原样返回（对照臂的定义）"

    y3 = tilt.preemphasize(x, sr, 3.0)
    y6 = tilt.preemphasize(x, sr, 6.0)

    lift3 = band_ratio(y3, (300, 800), (5000, 8000)) - band_ratio(x, (300, 800), (5000, 8000))
    lift6 = band_ratio(y6, (300, 800), (5000, 8000)) - band_ratio(x, (300, 800), (5000, 8000))
    assert lift3 > 5.0, f"+3dB/oct 的 5-8k/300-800 比值只升了 {lift3:+.2f} dB —— 没生效"
    assert lift6 > lift3, f"不单调：+6 档 {lift6:+.2f} ≤ +3 档 {lift3:+.2f}"

    # pivot 以下**内部**的比值不该被改（300-800 与 800-1600 都在 pivot 下）
    low3 = band_ratio(y3, (300, 400), (800, 1600)) - band_ratio(x, (300, 400), (800, 1600))
    assert abs(low3) < 0.6, f"pivot 以下的谱形被改了 {low3:+.2f} dB"


def test_preemphasis_does_not_clip_the_dataset_builder(tilt, tmp_path):
    """造数据集时若抬完过顶，必须**等比缩**而不是硬削（否则训练集被削平）。"""
    src = tmp_path / "src"
    src.mkdir()
    sr = 40000
    t = np.arange(sr) / sr
    loud = (0.98 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)
    sf.write(str(src / "a.wav"), loud, sr, subtype="PCM_16")

    dst = tmp_path / "dst"
    n = tilt.build_dataset(src, dst, 12.0)   # 猛抬，必过顶
    assert n == 1
    y, _ = sf.read(str(dst / "a.wav"), dtype="float64")
    assert np.abs(y).max() <= 0.995, f"抬完没做峰值安全化：峰值 {np.abs(y).max()}"
    # 且不许出现"被削平"的一长串等值样本
    assert int((np.abs(y) >= 0.999).sum()) == 0


def test_build_dataset_is_idempotent(tilt, tmp_path):
    """重复建同一个目录必须得到同样内容（幂等）—— 否则重跑会叠加上一次的结果。"""
    src = tmp_path / "src"
    src.mkdir()
    sr = 40000
    sf.write(str(src / "a.wav"), (0.3 * np.sin(2 * np.pi * 400 * np.arange(sr) / sr)).astype(np.float32),
             sr, subtype="PCM_16")
    dst = tmp_path / "dst"
    tilt.build_dataset(src, dst, 3.0)
    first, _ = sf.read(str(dst / "a.wav"))
    tilt.build_dataset(src, dst, 3.0)
    second, _ = sf.read(str(dst / "a.wav"))
    assert np.array_equal(first, second), "第二次建集结果不同 —— 不是幂等的"


# ---------------------------------------------------------------- 绝对频段口径


def test_abs_bands_detects_a_louder_copy_as_level_not_brightness(absb, tmp_path):
    """★★ 核心：**整体放大**不能被读成"更亮"。

    这正是 `cover_metrics.band_db`（相对 0-4k）单独用时的陷阱：一个 ×2 的副本
    会显得"高频更强"，其实什么都没变。绝对口径必须把这件事读成"总电平 +6 dB、
    各频段净变化 ≈ 0"。
    """
    sr = 40000
    t = np.arange(sr * 2) / sr
    rng = np.random.default_rng(1)
    x = (0.2 * rng.standard_normal(len(t))).astype(np.float32)
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(str(a), x, sr, subtype="FLOAT")
    sf.write(str(b), (x * 2.0).astype(np.float32), sr, subtype="FLOAT")

    ma, mb = absb.abs_bands(a), absb.abs_bands(b)
    assert mb["rms_dbfs"] - ma["rms_dbfs"] == pytest.approx(6.02, abs=0.3)
    # 各频段的**相对总能量**占比不变 ⇒ 净变化必须 ≈ 0
    for k in ("4-8k_dbfs", "8-11k_dbfs"):
        net = (mb[k] - ma[k]) - (mb["rms_dbfs"] - ma["rms_dbfs"])
        assert abs(net) < 0.5, f"{k} 的净变化 {net:+.2f} dB —— 把『更响』读成了『更亮』"


def test_abs_bands_detects_a_real_high_shelf(absb, tmp_path):
    """反向：真把高频抬了，绝对口径必须读出来（否则尺子没有正向区分度）。

    ★ 用 **8-11k** 判（2026-10-14 校准）：我第一版拿 4-8k 判，读数只有 −0.89 dB
    就红了 —— 但红的原因是**我的测试信号不对**：`x + 0.8*highpass(x)` 会在
    0-4k 里留下高通滤波的振铃，把总能量推高 3.8 dB，于是"扣总电平"时被稀释。
    8-11k 是高通真正加能量的地方（+0.81 dB 净），而 0-4k 本来就该掉（−5.24）。
    """
    sr = 40000
    t = np.arange(sr * 2) / sr
    rng = np.random.default_rng(2)
    x = (0.2 * rng.standard_normal(len(t))).astype(np.float32)
    from scipy.signal import butter, sosfilt
    sos = butter(4, 3000, fs=sr, btype="highpass", output="sos")
    y = (x + 0.8 * sosfilt(sos, x)).astype(np.float32)

    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    sf.write(str(a), x, sr, subtype="FLOAT")
    sf.write(str(b), y, sr, subtype="FLOAT")

    ma, mb = absb.abs_bands(a), absb.abs_bands(b)
    net_hi = (mb["8-11k_dbfs"] - ma["8-11k_dbfs"]) - (mb["rms_dbfs"] - ma["rms_dbfs"])
    assert net_hi > 0.3, f"真抬了高频，8-11k 净变化却只有 {net_hi:+.2f} dB —— 尺子没区分度"
    # 而 0-4k 相对总电平必须**下降**（能量被搬去高频了）—— 反向断言，防"什么都涨"
    net_lo = (mb["0-4k_dbfs"] - ma["0-4k_dbfs"]) - (mb["rms_dbfs"] - ma["rms_dbfs"])
    assert net_lo < -1.0, f"0-4k 的净变化 {net_lo:+.2f} dB 没降 —— 口径读不出'能量被搬走'"


# ---------------------------------------------------------------- 真产物回归


def test_real_tilt_arms_are_brighter_in_absolute_terms():
    """真产物回归：本机跑过的**三组**，t03 的 4-8k **净变化**都必须为正。

    三组分别是：同一首歌的两段时间（各配一个 seed）+ **换一首歌**（《唯一》）。
    ★ 换歌那一组是"结论只对这一首成立吗"的答案（2026-10-14 补跑）。

    产物在 `media/`（gitignore），没有就跳过 —— 但不能因为跳过就假装通过。
    """
    p = _ROOT / "media/covers/tilt_eval/absolute_bands.json"
    if not p.exists():
        pytest.skip("本机没有预加重产物（media/ 不进仓库）")
    rows = json.loads(p.read_text(encoding="utf-8"))
    by = {r["label"]: r for r in rows}
    pairs = [("t00 原样（对照）", "t03 预加重 +3dB/oct"),
             ("[段2] t00 原样", "[段2] t03 预加重")]
    checked = 0
    for ak, bk in pairs:
        if ak not in by or bk not in by:
            continue
        a, b = by[ak], by[bk]
        net = (b["4-8k_dbfs"] - a["4-8k_dbfs"]) - (b["rms_dbfs"] - a["rms_dbfs"])
        checked += 1
        assert net > 2.0, (
            f"{ak} → {bk} 的 4-8k 净变化只有 {net:+.2f} dB —— "
            "报告 m1j 的结论（高频被练进去了）不再成立，请复核后再改文档")
    assert checked >= 1, "一条对照都没跑到 —— 键名变了？"


def test_second_song_reproduces_the_tilt_gain():
    """★ 换歌也要复现 —— 这是"结论不是只对一首歌成立"的守卫。

    第三组用《唯一》（用户 2026-09-27 自己扒的那首，比 Lorezo 高得多：
    中位 275.5Hz vs 259、最高 **1187.8Hz**）。本机实测该组净变化：
    4-8k **+2.71 dB**、8-11k **+7.86 dB**，且「像你」升（0.5529→0.5754）。

    门槛取 1.5（该组实测 2.71，留余量但不至于宽松到放过"没效果"）。
    """
    import importlib.util

    pa = _ROOT / "media/covers/song2/tilt_eval/tilt__myvoice_tilt_t00.wav"
    pb = _ROOT / "media/covers/song2/tilt_eval/tilt__myvoice_tilt_t03.wav"
    if not (pa.exists() and pb.exists()):
        pytest.skip("本机没有第二首歌的预加重产物（media/ 不进仓库）")

    spec = importlib.util.spec_from_file_location(
        "tilt_absb_t", _ROOT / "experiments" / "cover_tilt_absbands.py")
    ab = importlib.util.module_from_spec(spec)
    sys.modules["tilt_absb_t"] = ab
    spec.loader.exec_module(ab)

    a, b = ab.abs_bands(pa), ab.abs_bands(pb)
    rms = b["rms_dbfs"] - a["rms_dbfs"]
    net4 = (b["4-8k_dbfs"] - a["4-8k_dbfs"]) - rms
    net8 = (b["8-11k_dbfs"] - a["8-11k_dbfs"]) - rms
    assert net4 > 1.5, f"第二首歌 4-8k 净变化只有 {net4:+.2f} dB —— 换歌没复现"
    assert net8 > 2.0, f"第二首歌 8-11k 净变化只有 {net8:+.2f} dB —— 换歌没复现"
    # 反向：总电平**不该**显著上涨（否则"变亮"可能只是"变响"）
    assert rms < 1.0, f"总电平涨了 {rms:+.2f} dB —— 得重新确认这不是响度效应"
