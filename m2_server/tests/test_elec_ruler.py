# -*- coding: utf-8 -*-
"""`experiments/elec_ruler.py` 的定标守卫。

这把尺子是本轮「电音归因」的全部证据来源，所以必须先证明它**在已知答案上单调**：
纯正弦 → 1；白噪声 → 接近 0；正弦 + 噪声的 SNR 越低 → 读数越低。
它不过关，后面所有"高音电音"的结论都不算数（同族：犯错指南 #171/#178）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_EXP = _ROOT / "experiments"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _EXP / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ruler = _load("elec_ruler_under_test", "elec_ruler.py")

SR = 40000


def _sine(f0=220.0, n=2048, amp=0.5):
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * f0 * t)).astype(np.float32)


def test_pure_sine_reads_near_one():
    v = ruler.peak_at_f0(_sine(), SR, 220.0)
    assert v is not None and v > 0.99


def test_pitch_does_not_change_the_ceiling():
    """★ 回归（2026-10-19）：旧版 `ac/ac[0]` 归一让上限 = 1 − lag/n，
    同一个纯正弦在 108Hz 只能读 0.82、325Hz 读 0.94 —— "高音更周期"的假象。"""
    lows = ruler.peak_at_f0(_sine(108.0, n=4096), SR, 108.0)
    highs = ruler.peak_at_f0(_sine(325.0, n=4096), SR, 325.0)
    assert lows > 0.99 and highs > 0.99
    assert abs(lows - highs) < 0.01


def test_white_noise_reads_near_zero():
    rng = np.random.default_rng(0)
    v = ruler.peak_at_f0(rng.normal(0, 0.1, 2048), SR, 220.0)
    assert v is not None and v < 0.15


def test_reads_decrease_with_noise_floor():
    """SNR 单调：+30dB → 0dB → −10dB 必须一路下降（尺子的分辨率）。"""
    rng = np.random.default_rng(1)
    clean = _sine()
    reads = []
    for snr_db in (30.0, 10.0, 0.0, -10.0):
        noise_amp = 10 ** (-snr_db / 20.0) * float(np.sqrt((clean ** 2).mean()))
        reads.append(ruler.peak_at_f0(clean + rng.normal(0, noise_amp, len(clean)), SR, 220.0))
    assert all(v is not None for v in reads)
    assert reads[0] > reads[1] > reads[2] > reads[3]
    assert reads[1] > 0.80 and reads[3] < 0.6


def test_wrong_f0_is_not_a_perfect_peak():
    """F0 写错时必须掉下来 —— 否则尺子只是"lag→0 恒为 1"的假读数。"""
    ok = ruler.peak_at_f0(_sine(220.0), SR, 220.0)
    # 150Hz 的搜索窗（±15% ⇒ lag 227–307）里没有 220Hz 的整周期（181.8×2=363）
    bad = ruler.peak_at_f0(_sine(220.0), SR, 150.0)
    assert ok > 0.99 and bad < 0.9


def test_silence_returns_none_or_zero():
    assert ruler.peak_at_f0(np.zeros(2048, dtype=np.float32), SR, 220.0) is None


def test_summarise_bins_and_percentiles():
    rows = [(110.0, 0.9)] * 20 + [(300.0, 0.4)] * 20 + [(500.0, 0.2)] * 20
    s = ruler.summarise(rows)
    assert s["frames"] == 60
    assert s["periodicity_p50"] == pytest.approx(0.4)
    assert "0-130" in s["by_f0"] and s["by_f0"]["0-130"]["p50"] == pytest.approx(0.9)
    # 每箱至少 15 帧才出统计（少于 15 帧的箱不参与结论）
    tiny = ruler.summarise([(110.0, 0.9)] * 5 + [(300.0, 0.4)] * 20)
    assert "0-130" not in tiny["by_f0"]


def test_summarise_empty():
    assert ruler.summarise([]) == {"frames": 0}
