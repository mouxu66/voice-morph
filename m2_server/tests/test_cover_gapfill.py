# -*- coding: utf-8 -*-
"""`cover_gapfill.py` 的纯函数守卫。

最贵的两类错：
1. **把正常唱段当掉音** → 原唱声音被混进来（−12dB 也听得出来），
   所以阈值必须有"源够响 + 输出近乎静音"两个条件。
2. **补丁边缘咔哒** → 掉音段被硬切出来，听感比原来的断字更糟。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import cover_gapfill as gf  # noqa: E402

SR = 40000


def _tone(seconds=1.0, freq=220.0, amp=0.3):
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_frame_rms_constant_signal():
    x = np.full(SR, 0.5, dtype=np.float32)
    r = gf.frame_rms(x, SR)
    assert len(r) == 99  # 1s / 10ms − 1
    assert np.allclose(r, 0.5, atol=1e-5)


def test_drop_mask_needs_both_loud_source_and_silent_output():
    src = np.array([0.5, 0.5, 0.5, 0.5, 0.001, 0.5])
    out = np.array([0.5, 0.0, 0.02, 0.001, 0.0, 0.5])
    m = gf.drop_mask(src, out)
    # 第 1 帧：源响、输出 0 → 掉音；第 2 帧：输出 0.02 < 0.5*0.15 → 也算；
    # 第 3 帧：输出 0.001 → 算；第 4 帧：源太轻 → 不算
    assert m.tolist() == [False, True, True, True, False, False]


def test_drop_mask_empty_and_silent():
    assert gf.drop_mask(np.array([]), np.array([])).size == 0
    assert not gf.drop_mask(np.zeros(10), np.zeros(10)).any()


def test_fill_only_touches_dropped_region():
    src = _tone(1.0)
    out = src.copy()
    out[SR // 2: SR // 2 + 4000] = 0.0  # 100ms 掉音
    res, n = gf.fill(out, src, SR)
    assert n >= 5
    mid = slice(SR // 2 + 1000, SR // 2 + 3000)
    # 补回来的是源 × −12dB（淡入淡出后可能略低，给宽容差）
    ratio = np.sqrt((res[mid] ** 2).mean()) / np.sqrt((src[mid] ** 2).mean())
    assert 0.10 < ratio < 0.30, ratio
    # 洞外必须逐样本不变
    assert np.array_equal(res[:SR // 2 - 2000], out[:SR // 2 - 2000])
    assert np.allclose(res[SR // 2 + 6000:], out[SR // 2 + 6000:], atol=1e-6)


def test_fill_noop_when_nothing_dropped():
    src = _tone(1.0)
    res, n = gf.fill(src, src, SR)
    assert n == 0
    assert np.array_equal(res, src)


def test_fill_limits_peak():
    src = np.full(2000, 0.95, dtype=np.float32)
    out = np.zeros(2000, dtype=np.float32)
    res, _ = gf.fill(out, src, SR, gain_db=0.0)
    assert np.max(np.abs(res)) <= 0.98 + 1e-6


def test_enabled_env_flag(monkeypatch):
    monkeypatch.delenv("VM_COVER_GAPFILL", raising=False)
    assert gf.enabled() is True
    monkeypatch.setenv("VM_COVER_GAPFILL", "0")
    assert gf.enabled() is False


def test_global_level_difference_is_not_a_dropout():
    """★ 回归：RVC 输出整体低 10dB 时，不能把每一帧都判成掉音。"""
    src = np.array([0.5, 0.4, 0.3, 0.5, 0.2])
    out = src * 0.32  # 整曲 −10dB，动态完全一致
    assert not gf.drop_mask(src, out).any()
