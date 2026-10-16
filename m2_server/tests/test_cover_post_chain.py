# -*- coding: utf-8 -*-
"""`experiments/cover_post_chain.py`（《唯一》高音段"再优化"实验）的守卫。

这批加工的产物是要给**耳朵**裁决的，所以脚本本身不能悄悄犯这三类错：

1. **"不加工"必须是真·空串** —— `apply_filters` 用 `filters == ""` 判断"直接复制"，
   若关掉所有档位还拼出一个 `","` 之类的脏串，ffmpeg 会报错或做出意外处理，
   于是"对照臂"其实被加工过 —— 单变量纪律当场破功。
2. **空气感与 De-Ess 必须成对、且顺序固定** —— 只抬 9k 不压齿音会把 s/z 抬尖，
   这正是本实验要避免的失败模式（RVC 输出 8–12k 已比输入低，抬它是有意的，
   但必须紧跟着把齿音收回去）。
3. **气口轨只能含"非浊音"成分、且不得引入底噪** —— 它是要叠回人声的：
   浊音段漏进去会与原唱打架（瞬间变成双声部，正是上一轮电音的成因）；
   `floor_db` 没生效则会把安静房间的底噪一起叠上去，越优化越脏。

纯函数测试：不依赖 ffmpeg / librosa / GPU。
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
    spec = importlib.util.spec_from_file_location("cover_post_chain", _EXP / "cover_post_chain.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cover_post_chain"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cpc():
    return _load()


# ---------------------------------------------------------------- 滤镜串

def test_filter_chain_has_air_deess_comp_in_order(cpc):
    s = cpc.post_filters()
    assert s, "默认档位不该是空串"
    for frag in ("treble=", "deesser=", "acompressor="):
        assert frag in s
    assert s.index("treble=") < s.index("deesser=") < s.index("acompressor="), \
        "空气感必须先于 De-Ess（先抬 9k 再把齿音压回去），压缩放最后"


def test_reverb_is_off_by_default_and_opt_in(cpc):
    assert "aecho" not in cpc.post_filters(), "混响是混音决策，默认必须关（会掩盖干声毛病）"
    assert "aecho" in cpc.post_filters(reverb=True)


def test_all_stages_off_yields_empty_string(cpc):
    """关掉每一档 ⇒ 真·空串 ⇒ 调用方走'直接复制'分支（对照臂必须零加工）。"""
    assert cpc.post_filters(air_db=0, deess_i=0, comp_ratio=0) == ""


def test_numbers_are_formatted_without_scientific_notation(cpc):
    """ffmpeg 滤镜值不接受 1e-05 这种写法（会当场报错）。"""
    s = cpc.post_filters(air_db=0.0000001, air_hz=9000.0, comp_ratio=2.5)
    assert "e-" not in s.lower() and "e+" not in s.lower()


# ---------------------------------------------------------------- 浊音权重 / 气口轨

def test_sample_weight_is_all_one_for_fully_voiced(cpc):
    w = cpc.frame_to_sample_weight(np.ones(20, dtype=bool), 4000, 40000, smooth_ms=60.0)
    assert w.shape == (4000,)
    assert np.allclose(w, 1.0, atol=1e-6)


def test_sample_weight_is_all_zero_for_fully_unvoiced(cpc):
    w = cpc.frame_to_sample_weight(np.zeros(20, dtype=bool), 4000, 40000, smooth_ms=60.0)
    assert np.allclose(w, 0.0, atol=1e-6)


def test_sample_weight_length_always_matches_audio(cpc):
    """帧数与采样数不成整数倍（pyin 的常态）时也必须精确等于音频长度，否则叠回会错位。"""
    for n in (3999, 4000, 4001, 12345):
        w = cpc.frame_to_sample_weight(np.ones(7, dtype=bool), n, 40000)
        assert w.shape == (n,)
    w0 = cpc.frame_to_sample_weight(np.array([], dtype=bool), 999, 40000)
    assert w0.shape == (999,) and not w0.any()


def test_weight_smoothing_creates_ramp_not_step(cpc):
    """浊音↔非浊音的边界必须是斜坡：硬切会切出咔哒声。"""
    voiced = np.zeros(40, dtype=bool)
    voiced[20:] = True
    w = cpc.frame_to_sample_weight(voiced, 40000, 40000, smooth_ms=60.0)
    mid = w[19000:21000]
    assert 0.05 < mid.min() and mid.max() < 0.95, "边界处应出现中间值（平滑窗口生效）"


def test_breath_track_keeps_only_unvoiced_part(cpc):
    """合成'浊音正弦 + 非浊音噪声'：气口轨只应出现在非浊音那一段。"""
    sr, seg = 40000, 40000
    t = np.arange(seg) / sr
    y = np.concatenate([0.5 * np.sin(2 * np.pi * 200 * t), 0.05 * np.random.RandomState(0).randn(seg)])
    y = y.astype(np.float32)
    voiced = np.concatenate([np.ones(20, dtype=bool), np.zeros(20, dtype=bool)])
    b = cpc.breath_track(y, voiced, sr, smooth_ms=20.0, floor_db=-80.0)
    assert b.shape == y.shape
    loud, quiet = float(np.abs(b[:18000]).max()), float(np.abs(b[22000:]).max())
    assert quiet > 0.005, "非浊音段的气口成分被丢掉了"
    assert loud < quiet / 5, "浊音段漏进了气口轨（叠回会变成双声部）"


def test_breath_track_floor_kills_room_noise(cpc):
    """低于 floor 的样本必须清零 —— 否则叠回去的是底噪，越优化越脏。"""
    sr, seg = 40000, 8000
    y = (1e-4 * np.random.RandomState(1).randn(seg)).astype(np.float32)  # 约 −80dBFS
    b = cpc.breath_track(y, np.zeros(10, dtype=bool), sr, floor_db=-60.0)
    assert not b.any()


# ---------------------------------------------------------------- 叠回

def test_overlay_breath_adds_exactly_at_given_gain(cpc):
    vocal = np.full(8000, 0.10, dtype=np.float32)
    breath = np.full(8000, 0.20, dtype=np.float32)
    out = cpc.overlay_breath(vocal, breath, gain_db=-20.0)
    add = 0.20 * (10.0 ** (-20.0 / 20.0))
    assert out.shape == vocal.shape
    assert np.allclose(out, 0.10 + add, atol=1e-6)


def test_overlay_breath_never_touches_voiced_samples(cpc):
    """气口为零的位置必须逐样本不变（不做整体增益/归一化）。"""
    rng = np.random.RandomState(2)
    vocal = (0.3 * rng.randn(5000)).astype(np.float32)
    breath = np.zeros(5000, dtype=np.float32)
    breath[1000:2000] = 0.1
    out = cpc.overlay_breath(vocal, breath, gain_db=-18.0)
    mask = np.ones(5000, dtype=bool)
    mask[1000:2000] = False
    assert np.array_equal(out[mask], vocal[mask])


def test_overlay_breath_tolerates_length_mismatch(cpc):
    """源与输出长度不等（重采样/裁边）时按短的来，不得抛异常或错位叠加。"""
    out = cpc.overlay_breath(np.zeros(1000, dtype=np.float32), np.ones(400, dtype=np.float32), gain_db=0.0)
    assert out.shape == (1000,)
    assert np.allclose(out[:400], 1.0) and np.allclose(out[400:], 0.0)


def test_peak_normalize_only_scales_when_clipping(cpc):
    ok = np.array([0.5, -0.5], dtype=np.float32)
    assert np.array_equal(cpc._peak_normalize(ok), ok), "没超顶就不该动它"
    hot = np.array([1.5, -0.5], dtype=np.float32)
    assert abs(float(np.abs(cpc._peak_normalize(hot)).max()) - 0.98) < 1e-5
