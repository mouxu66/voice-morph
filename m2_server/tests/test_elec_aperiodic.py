# -*- coding: utf-8 -*-
"""`experiments/elec_aperiodic.py` 的纯函数守卫。

两个断言最值钱：
1. **残差真的不是谐波** —— 220Hz 正弦 + 噪声过一遍，220Hz 处的能量必须掉一大截，
   而噪声带必须还在。否则"叠回残差"只是把同一段声音变响，剂量实验自我循环。
2. **剂量是单调的** —— −18 与 −6dB 的幅度比必须等于 10^(12/20)=3.98，
   不能因为防削波把整体缩了就把剂量关系也缩没（缩的是整体、不是相对关系）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_EXP = _ROOT / "experiments"

SR = 40000


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _EXP / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ap = _load("elec_aperiodic_under_test", "elec_aperiodic.py")


def _tone_with_noise(seconds=2.0, f0=220.0, snr_db=10.0, seed=0):
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    clean = 0.3 * np.sin(2 * np.pi * f0 * t)
    noise = rng.normal(0, 10 ** (-snr_db / 20.0) * float(np.sqrt((clean ** 2).mean())), len(clean))
    return (clean + noise).astype(np.float32)


def _amp_at(x, freq, sr=SR):
    n = len(x)
    spec = np.abs(np.fft.rfft(x * np.hanning(n)))
    freqs = np.fft.rfftfreq(n, 1 / sr)
    i = int(np.argmin(np.abs(freqs - freq)))
    return float(spec[max(0, i - 2):i + 3].max())


def test_residual_removes_harmonic_keeps_noise():
    x = _tone_with_noise()
    f0 = np.full(len(x) // 512 + 1, 220.0)
    r = ap.harmonic_residual(x, SR, f0=f0)

    assert len(r) == len(x)
    # 谐波必须掉 ≥15dB（正常实现实测约 −30dB）
    assert 20 * np.log10((_amp_at(r, 220.0) + 1e-12) / (_amp_at(x, 220.0) + 1e-12)) < -15.0
    # 残差 ≈ 原噪声的 0.5–0.8 倍：梳状陷波会连 220Hz 间隔的噪声 bin 一起挖掉
    # （每 11.3 个 bin 挖 7 个），但**必须还是同一量级** —— 若掉到 0.1 倍，
    # 说明残差大半是谐波旁瓣而不是噪声，叠回去的仍是"更响的谐波"。
    clean = 0.3 * np.sin(2 * np.pi * 220 * np.arange(len(x)) / SR)
    noise_rms = np.sqrt(((x - clean) ** 2).mean())
    ratio = np.sqrt((r ** 2).mean()) / max(noise_rms, 1e-12)
    assert 0.4 < ratio < 0.85, ratio


def test_residual_is_quiet_on_pure_sine():
    """纯正弦没有非周期成分 —— 残差应接近空，而不是把谐波换个相位还回来。"""
    t = np.arange(SR) / SR
    x = (0.5 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    r = ap.harmonic_residual(x, SR, f0=np.full(len(x) // 512 + 1, 220.0))
    assert np.sqrt((r ** 2).mean()) < 0.02 * np.sqrt((x ** 2).mean())


def test_mix_dose_ratio_is_linear():
    vocal = np.zeros(SR, dtype=np.float32)
    layer = np.full(SR, 0.01, dtype=np.float32)
    a = ap.mix_at_db(vocal, layer, -18.0, relative=False)
    b = ap.mix_at_db(vocal, layer, -6.0, relative=False)
    assert np.max(np.abs(b)) / np.max(np.abs(a)) == pytest.approx(10 ** (12 / 20), rel=0.02)


def test_relative_dose_anchors_to_vocal_rms():
    """★ 回归：层比人声低 20dB 时，绝对 −6dB 也等于没叠；相对模式必须先对齐 RMS。"""
    vocal = np.full(SR, 0.3, dtype=np.float32)
    layer = np.full(SR, 0.003, dtype=np.float32)  # 比人声低 40dB
    out = ap.mix_at_db(vocal, layer, -6.0, relative=True)
    # 层被抬到与人声同级后叠 −6dB ⇒ 总幅度≈0.3×(1+0.5)=0.45（未削波）
    assert np.max(np.abs(out)) == pytest.approx(0.45, rel=0.02)


def test_mix_truncates_to_shortest():
    assert len(ap.mix_at_db(np.zeros(100), np.zeros(40), -18.0)) == 100
    assert len(ap.mix_at_db(np.zeros(40), np.zeros(100), -18.0)) == 40


def test_mix_limits_peak_when_clipping():
    """超顶时整体缩到 0.98；此时剂量比**不再**等于纯线性比（整体缩了），
    只要求"更响的剂量仍然更响、且都不超顶" —— 别把缩峰误读成剂量失效。"""
    vocal = np.full(1000, 0.95, dtype=np.float32)
    layer = np.ones(1000, dtype=np.float32) * 0.2
    a = ap.mix_at_db(vocal, layer, -18.0, relative=False)
    b = ap.mix_at_db(vocal, layer, -6.0, relative=False)
    assert np.max(np.abs(a)) <= 0.98 + 1e-6
    assert np.max(np.abs(b)) <= 0.98 + 1e-6
    assert np.max(np.abs(b)) > np.max(np.abs(a))


def test_residual_short_input_is_safe():
    assert len(ap.harmonic_residual(np.zeros(100, dtype=np.float32), SR, f0=[220.0])) == 100


def test_envelope_mix_puts_dose_in_loud_frames():
    """★ 回归：整段 RMS 对齐会把剂量全落在静音段（响帧里等于没叠）。

    逐帧对齐后，在**响的帧**里 diff/vocal 必须≈10^(dB/20)，而不是远小于它。
    """
    rng = np.random.default_rng(2)
    n = SR * 2
    t = np.arange(n) / SR
    env = 0.05 + 0.95 * (np.sin(2 * np.pi * 0.5 * t) > 0)
    vocal = (0.3 * np.sin(2 * np.pi * 220 * t) * env).astype(np.float32)
    layer = rng.normal(0, 0.01, n).astype(np.float32)
    layer[: n // 4] = 0.0  # 前 1/4 没有残差（模拟静音段）
    out = ap.mix_at_db_envelope(vocal, layer, -6.0, SR)
    diff = out - vocal
    # 1.2–1.8s：人声在响、且离 1.0s 的包络切换足够远（避开平滑过渡段）
    mid = slice(int(1.2 * SR), int(1.8 * SR))
    ratio = np.sqrt((diff[mid] ** 2).mean()) / np.sqrt((vocal[mid] ** 2).mean())
    assert ratio == pytest.approx(10 ** (-6 / 20), rel=0.35)


def test_envelope_mix_silent_vocal_gets_no_layer():
    vocal = np.zeros(SR, dtype=np.float32)
    layer = np.ones(SR, dtype=np.float32) * 0.5
    assert np.max(np.abs(ap.mix_at_db_envelope(vocal, layer, -6.0, SR))) == 0.0
