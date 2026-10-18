# -*- coding: utf-8 -*-
"""`experiments/cover_emotion_chain.py`（表现力加工 A/B）的纯函数守卫。

这批加工是给**耳朵**裁决的，脚本自己不能悄悄犯四类错：

1. **对照臂必须零加工** —— A 臂一旦被加上空间/推拉，单变量纪律当场破功，
   用户听到的“差别”就不再是这几个变量。
2. **段落推拉不能变成音节内推拉** —— 8s 窗口若退化成 50ms，会把源里已有的
   动态再乘一遍（平方），听感是“抽气”，而且会把“情感”做成“抖动”。
3. **混响必须真的是卷积空间，不是离散回声** —— `aecho` 在慢歌上会听成
   山谷重复；IR 必须是衰减尾巴、左右去相关、单位能量（湿声电平只由 wet 决定）。
4. **宽化必须保持时长/有限值，且只在 F 臂** —— 延迟叠错位会变成合唱/相位洞；
   `mix_stems` 的防削波只能在超顶时等比缩，不能偷偷压动态。

测试只依赖 numpy/scipy，不跑 ffmpeg / librosa / GPU。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_EXP = _ROOT / "experiments"


def _load():
    spec = importlib.util.spec_from_file_location("cover_emotion_chain", _EXP / "cover_emotion_chain.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cover_emotion_chain"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cec():
    return _load()


# ---------------------------------------------------------------- 臂清单

def test_control_arm_is_truly_unprocessed(cec):
    """① 必须没有任何加工 —— 否则它就不是“你已确认的颜色”基线。"""
    by_key = {k: opts for k, _t, _s, opts in cec.ARMS}
    assert by_key["A_control"] == {}
    assert set(by_key) == {"A_control", "B_space", "C_breath", "D_space_auto", "E_density", "F_wide"}


def test_arms_are_cumulative_and_single_variable(cec):
    by_key = {k: opts for k, _t, _s, opts in cec.ARMS}
    assert by_key["B_space"] == {"reverb": True}
    assert by_key["C_breath"] == {"automation": True}
    assert by_key["D_space_auto"] == {"reverb": True, "automation": True}
    # E/F 是“在 D 上加一个”，不是另起炉灶
    assert by_key["E_density"] == {"reverb": True, "automation": True, "density": True}
    assert by_key["F_wide"] == {"reverb": True, "automation": True, "density": True, "wide": True}


# ---------------------------------------------------------------- IR / 混响

def test_ir_is_stereo_unit_energy_and_decorrelated(cec):
    ir = cec.make_ir(sr=8000, rt60=1.0, length_s=1.2)
    assert ir.shape == (9600, 2)
    for ch in range(2):
        assert abs(float(np.sqrt((ir[:, ch] ** 2).sum())) - 1.0) < 1e-4
    corr = float(np.corrcoef(ir[:, 0], ir[:, 1])[0, 1])
    assert corr < 0.5, f"左右通道太像（corr={corr:.2f}）→ 没有立体声空间"


def test_ir_tail_decays(cec):
    """尾巴必须衰减：前 100ms 能量应远大于最后 100ms。"""
    ir = cec.make_ir(sr=8000, rt60=0.8, length_s=1.5)
    head = float((ir[:800] ** 2).sum())
    tail = float((ir[-800:] ** 2).sum())
    assert head > tail * 20


def test_reverb_keeps_dry_and_adds_tail(cec):
    sr = 8000
    x = np.zeros(4000, dtype=np.float32)
    x[500:1500] = 0.3
    ir = cec.make_ir(sr=sr, rt60=1.0, length_s=1.0)
    out = cec.reverb(x, ir, wet=0.2)
    assert out.shape == (4000, 2)
    assert np.isfinite(out).all()
    # 干声段仍在（左右都含原信号）
    assert np.allclose(out[600:900, 0], x[600:900], atol=0.2)
    # 停止后 200–400ms 应有余量
    tail = float(np.abs(out[1700:1900]).mean())
    assert tail > 1e-3, "混响没有尾巴"
    # 立体声：左右不应逐样本相同（去相关 IR 生效）
    assert not np.allclose(out[:, 0], out[:, 1])


# ---------------------------------------------------------------- 段落推拉

def test_automation_gain_is_bounded_and_length_matched(cec):
    sr = 8000
    src = np.random.RandomState(0).randn(3 * sr).astype(np.float32) * 0.1
    g = cec.automation_gain(src, sr, window_s=1.0, max_db=2.0)
    assert g.shape == src.shape
    assert np.isfinite(g).all()
    lo, hi = 10 ** (-2.0 / 20), 10 ** (2.0 / 20)
    assert g.min() >= lo - 1e-5 and g.max() <= hi + 1e-5


def test_automation_follows_sections_not_syllables(cec):
    """前 1s 轻、后 1s 响：推拉曲线应在两段间有差，但段内近乎平。"""
    sr = 4000
    quiet = 0.02 * np.sin(2 * np.pi * 200 * np.arange(sr) / sr)
    loud = 0.20 * np.sin(2 * np.pi * 200 * np.arange(sr) / sr)
    src = np.concatenate([quiet, loud]).astype(np.float32)
    g = cec.automation_gain(src, sr, window_s=0.5, max_db=2.0)
    assert g[: sr // 2].mean() < g[sr // 2:].mean(), "响的段落应被推起来"
    # 8s/0.5s 窗平滑后，段内相邻采样差应极小（不是逐音节抖动）
    assert np.abs(np.diff(g)).max() < 0.01


def test_automation_empty_input_is_safe(cec):
    assert cec.automation_gain(np.zeros(0, dtype=np.float32), 8000).shape == (0,)


# ---------------------------------------------------------------- 压缩 / 密度

def test_compressor_reduces_dynamic_range(cec):
    sr = 8000
    t = np.arange(sr) / sr
    quiet = 0.02 * np.sin(2 * np.pi * 200 * t)
    loud = 0.50 * np.sin(2 * np.pi * 200 * t)
    x = np.concatenate([quiet, loud]).astype(np.float32)
    y = cec.compressor(x, sr, threshold_db=-24, ratio=4, makeup_db=0)
    assert y.shape == x.shape and np.isfinite(y).all()
    before = 20 * np.log10(np.sqrt((loud ** 2).mean()) / np.sqrt((quiet ** 2).mean()))
    after = 20 * np.log10(np.sqrt((y[sr:] ** 2).mean()) / np.sqrt((y[:sr] ** 2).mean()))
    assert after < before - 3, f"动态没被压下来：{before:.1f} → {after:.1f} dB"


def test_parallel_density_preserves_length_and_never_clips(cec):
    sr = 8000
    x = (0.6 * np.sin(2 * np.pi * 180 * np.arange(sr) / sr)).astype(np.float32)
    y = cec.parallel_density(x, sr, amount=0.2)
    assert y.shape == x.shape and np.isfinite(y).all()
    assert float(np.abs(y).max()) <= cec.MIX_CEILING + 1e-5


# ---------------------------------------------------------------- 宽化 / 混音

def test_widen_keeps_length_and_creates_side_energy(cec):
    sr = 8000
    x = np.stack([np.sin(2 * np.pi * 200 * np.arange(sr) / sr)] * 2, axis=1).astype(np.float32)
    y = cec.widen(x, sr, delays_ms=(5.0, 7.0), amount=0.2)
    assert y.shape == x.shape and np.isfinite(y).all()
    mid = (y[:, 0] + y[:, 1]) / 2
    side = (y[:, 0] - y[:, 1]) / 2
    assert np.sqrt((side ** 2).mean()) > 0
    assert np.sqrt((side ** 2).mean()) < np.sqrt((mid ** 2).mean())


def test_mix_stems_is_exact_linear_sum_when_not_clipping(cec):
    v = np.full((100, 2), 0.1, dtype=np.float32)
    a = np.full((100, 2), 0.2, dtype=np.float32)
    out, scale = cec.mix_stems(v, a, vocal_gain=0.5, acc_gain=0.5)
    assert scale == 1.0
    assert np.allclose(out, 0.15, atol=1e-6)


def test_mix_stems_scales_only_when_over_ceiling(cec):
    v = np.full((100, 2), 0.8, dtype=np.float32)
    a = np.full((100, 2), 0.8, dtype=np.float32)
    out, scale = cec.mix_stems(v, a, vocal_gain=1.0, acc_gain=1.0, ceiling=0.98)
    assert scale < 1.0
    assert abs(float(np.abs(out).max()) - 0.98) < 1e-5
