"""offline_vc_infer.postprocess_audio 回归测试。

历史坑：旧实现结尾 np.clip(y, -0.99, 0.99) 硬削波，RVC 瞬态 + IIR 低通过冲
后 ~0.17% 样本被削平，听感刺耳（NatScore 掉到负值区）。重构为「只衰减」的
峰值归一（-1.5 dBFS 目标）后，用例钉死：不削波、响度锚点保持、静音安全。
"""
import numpy as np
import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")

import offline_vc_infer  # noqa: E402

TARGET = 10 ** (-1.5 / 20)


def _speech_like(n: int = 48000, sr: int = 48000, sine_amp: float = 0.3,
                 n_impulse: int = 20, impulse_amp: float = 6.0) -> np.ndarray:
    """正弦 + 稀疏大幅瞬态：高波峰因子，模拟 RVC 输出的过冲尖峰。
    瞬态只放后半段，保证前半段是纯正弦（供波形形状断言用）。"""
    t = np.arange(n) / sr
    y = sine_amp * np.sin(2 * np.pi * 220 * t)
    rng = np.random.default_rng(7)
    idx = rng.integers(n // 2, n - 100, size=n_impulse)
    y[idx] += impulse_amp
    return y.astype(np.float32)


def test_loud_input_no_hard_clip_peak_at_target():
    y = _speech_like()
    out = offline_vc_infer.postprocess_audio(y, 48000)
    assert out.dtype == np.float32
    peak = float(np.abs(out).max())
    assert peak == pytest.approx(TARGET, rel=1e-3)   # 峰值衰减到 -1.5 dBFS
    assert peak < 0.99                               # 不再顶硬限幅沿
    # 无平顶削波：贴着目标的样本占比必须远低于旧 clip 实现
    assert (np.abs(out) >= TARGET * 0.999).mean() < 0.005


def test_no_flat_top_clipping():
    """等比缩放不产生平顶：贴峰的长连续段是硬削波特征，等比缩放没有。"""
    y = _speech_like()
    out = offline_vc_infer.postprocess_audio(y, 48000)
    peak = float(np.abs(out).max())
    at_peak = np.abs(np.abs(out) - peak) < 1e-4 * peak
    idx = np.where(at_peak)[0]
    assert len(idx) > 0
    segments = np.split(idx, np.where(np.diff(idx) > 1)[0] + 1)
    max_run = max(len(s) for s in segments)
    assert max_run <= 3  # 硬削波会形成远超 3 个样本的连续平顶


def test_normal_signal_rms_anchor():
    """峰值安全的正常信号：RMS 应锚定在 -18 dBFS，不被再放大。"""
    t = np.arange(48000) / 48000
    y = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    out = offline_vc_infer.postprocess_audio(y, 48000)
    rms = float(np.sqrt((out ** 2).mean()))
    assert rms == pytest.approx(10 ** (-18 / 20), rel=0.05)
    assert float(np.abs(out).max()) < TARGET  # 未触发峰值衰减


def test_low_sr_filter_guard():
    """22.05k 模型输出：低通截止自动让出 Nyquist（min(11k, 0.45·sr)），不报错。"""
    y = _speech_like(n=22050, sr=22050)
    out = offline_vc_infer.postprocess_audio(y, 22050)
    assert np.isfinite(out).all()
    assert float(np.abs(out).max()) <= TARGET + 1e-6


def test_silence_and_dc_safe():
    out = offline_vc_infer.postprocess_audio(np.zeros(4800, dtype=np.float32), 48000)
    assert out.shape == (4800,) and np.isfinite(out).all() and float(np.abs(out).max()) == 0.0
    out2 = offline_vc_infer.postprocess_audio(np.full(4800, 0.5, dtype=np.float32), 48000)
    assert np.isfinite(out2).all() and float(np.abs(out2).max()) <= TARGET + 1e-6
