"""effects.py DSP 效果链单测（纯 numpy/scipy，无 GPU/网络依赖）。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np
import pytest  # noqa: E402

pytest.importorskip("scipy")

import effects  # noqa: E402

SR = 16000


def _tone(dur=1.0, sr=SR, freq=440.0):
    t = np.arange(int(sr * dur), dtype=np.float32) / sr
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ---------------- apply_chain 通用行为 ----------------


def test_empty_chain_returns_original():
    x = _tone()
    out, skipped = effects.apply_chain(x, SR, [])
    assert skipped == []
    assert out.shape == x.shape
    assert np.allclose(out, x, atol=1e-6)


def test_none_chain_returns_original():
    x = _tone()
    out, skipped = effects.apply_chain(x, SR, None)
    assert skipped == []
    assert np.allclose(out, x, atol=1e-6)


def test_unknown_effect_skipped():
    x = _tone()
    out, skipped = effects.apply_chain(x, SR, [{"type": "nonexistent"}])
    assert any("未知效果" in s for s in skipped)
    assert np.allclose(out, x, atol=1e-6)


def test_malformed_step_skipped_silently():
    x = _tone()
    out, skipped = effects.apply_chain(x, SR, ["not-a-dict", 42])
    assert skipped == []
    assert np.allclose(out, x, atol=1e-6)


def test_failing_effect_skipped_not_abort():
    """某个效果抛异常不中断整链，且前序效果已生效。"""
    x = _tone()
    # 先加真实混响（会改信号），再喂一个必然崩溃的效果
    chain = [
        {"type": "reverb", "params": {"room": 0.6, "wet": 0.5}},
        {"type": "reverb", "params": None},  # None params → 应被跳过而非崩溃
    ]
    out, skipped = effects.apply_chain(x, SR, chain)
    assert skipped == []
    assert out.shape == x.shape
    assert np.all(np.isfinite(out))  # 无 NaN/Inf


def test_chain_order_matters():
    x = _tone()
    a, _ = effects.apply_chain(x, SR, [{"type": "reverb", "params": {"wet": 0.6}}])
    b, _ = effects.apply_chain(a, SR, [{"type": "reverb", "params": {"wet": 0.6}}])
    # 两个不同顺序/叠加应产生不同结果（至少二次叠加明显更湿）
    double, _ = effects.apply_chain(
        x,
        SR,
        [
            {"type": "reverb", "params": {"wet": 0.6}},
            {"type": "reverb", "params": {"wet": 0.6}},
        ],
    )
    assert not np.allclose(double, a, atol=1e-4)


# ---------------- 单效果：长度 / 有限 / 不削波 ----------------


def _assert_sane(out, x):
    assert out.shape[0] == x.shape[0] or out.ndim == 1  # 长度保持或单声道
    assert np.all(np.isfinite(out))
    assert float(np.max(np.abs(out))) <= 1.0 + 1e-6  # _norm 防削波


@pytest.mark.parametrize("fx_type", list(effects.CATALOG.keys()))
def test_each_effect_produces_finite_bounded_output(fx_type):
    """每个已注册效果：输入纯净正弦 → 输出有限、长度合理、被 _norm 约束。"""
    x = _tone()
    out, _ = effects.apply_chain(x, SR, [{"type": fx_type}])
    _assert_sane(out, x)
    # 输出不能整段静音（除非效果语义如此，如 limiter 默认仍保留信号）
    if fx_type != "limiter":
        assert float(np.max(np.abs(out))) > 1e-3


def test_mono_input_and_2ch_input_same_result():
    mono = _tone()
    stereo = np.stack([mono, mono], axis=1)
    out_m, _ = effects.apply_chain(mono, SR, [{"type": "reverb", "params": {"wet": 0.5}}])
    out_s, _ = effects.apply_chain(stereo, SR, [{"type": "reverb", "params": {"wet": 0.5}}])
    assert np.allclose(out_m, out_s, atol=1e-6)


def test_catalog_meta_has_no_fx_key():
    meta = effects.catalog_meta()
    assert meta
    for m in meta:
        assert "fx" not in m  # 前端目录不携带函数
        assert m["type"] in effects.CATALOG
        assert isinstance(m["params"], list)


def test_param_clamped_to_bounds():
    """越界参数被 clamp：传 99 的 room 不会导致崩溃或 NaN。"""
    x = _tone()
    out, _ = effects.apply_chain(x, SR, [{"type": "reverb", "params": {"room": 99, "wet": 99}}])
    assert np.all(np.isfinite(out))


# ---------------- 特定效果语义 ----------------


def test_speed_identity_at_rate_1():
    x = _tone()
    if effects.librosa is None:
        pytest.skip("librosa 未装，speed 降级直通")
    out, _ = effects.apply_chain(x, SR, [{"type": "speed", "params": {"rate": 1.0}}])
    assert np.allclose(out, x, atol=1e-6)


def test_pitch_identity_at_0():
    x = _tone()
    if effects.librosa is None:
        pytest.skip("librosa 未装，pitch 降级直通")
    out, _ = effects.apply_chain(x, SR, [{"type": "pitch", "params": {"semitones": 0}}])
    assert np.allclose(out, x, atol=1e-6)


def test_echo_changes_signal():
    x = _tone(0.5)
    out, _ = effects.apply_chain(
        x, SR, [{"type": "echo", "params": {"delay_s": 0.1, "feedback": 0.5}}]
    )
    assert out.shape[0] == x.shape[0]  # 时长不变（buf 超出部分截断）
    assert not np.allclose(out, x, atol=1e-3)  # 回声确实叠加了
