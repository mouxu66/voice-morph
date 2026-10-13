# -*- coding: utf-8 -*-
"""probe_natscore_domain 测试：钉死「递进劣化」探针自身的正确性。

为什么这些用例值钱（2026-10-14）：
这个探针是用来判**别的尺子能不能用**的，所以它自己必须先可信 ——
否则「自然度未测」这个结论就是拿一把没验过的探针说另一把尺子不行。
三处容易错：① SNR 配平（算错就变成"加了噪声反而更响"）；② 砖墙低通（
写错截止频率会把"丢高频"变成"丢了整个人声"）；③ 单调性判定（加容差就等于
往想要的结论上调）。全部纯 numpy，不 import torch（保持 FAST_TESTS 预算）。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import probe_natscore_domain as pnd  # noqa: E402


class TestRngNoise:
    def test_snr_is_actually_delivered(self):
        """要求 20dB，实测就得 ≈20dB（±0.5）。"""
        rng = np.random.default_rng(0)
        sig = (rng.standard_normal(48000) * 0.1).astype(np.float32)
        out = pnd.rng_noise(sig, 20.0)
        noise = out - sig * (np.max(np.abs(out)) / np.max(np.abs(sig)))
        # 归一化会整体缩放，所以按功率比算：噪声功率 / 信号功率
        p_n = float(np.mean(noise**2))
        p_s = float(np.mean((sig * (np.max(np.abs(out)) / np.max(np.abs(sig)))) ** 2))
        snr = 10 * np.log10(p_s / p_n)
        assert abs(snr - 20.0) < 0.5, f"实测 SNR={snr:.2f}dB，应≈20dB"

    def test_more_noise_means_lower_snr(self):
        """SNR 越小（噪声越多），输出与源的相关度越低 —— 单调。"""
        rng = np.random.default_rng(1)
        sig = (rng.standard_normal(24000) * 0.1).astype(np.float32)
        cors = []
        for snr in (30.0, 20.0, 10.0, 0.0):
            out = pnd.rng_noise(sig, snr)
            cors.append(float(np.corrcoef(sig, out)[0, 1]))
        assert cors == sorted(cors, reverse=True), f"相关度未随 SNR 下降单调：{cors}"

    def test_silent_input_does_not_blow_up(self):
        """全零输入按定义返回原样（不能除零）。"""
        z = np.zeros(1000, dtype=np.float32)
        assert np.array_equal(pnd.rng_noise(z, 10.0), z)

    def test_deterministic(self):
        """同 seed 必定逐字节相同（否则探针自身不可复现）。"""
        rng = np.random.default_rng(2)
        sig = (rng.standard_normal(8000) * 0.1).astype(np.float32)
        assert np.array_equal(pnd.rng_noise(sig, 15.0), pnd.rng_noise(sig, 15.0))

    def test_length_preserved(self):
        sig = np.ones(4321, dtype=np.float32) * 0.1
        assert len(pnd.rng_noise(sig, 10.0)) == len(sig)


class TestLowpass:
    def test_passband_kept_stopband_gone(self):
        """400/4000Hz 的低频必须留下，8/12kHz 的高频必须清掉。"""
        sr = 24000
        t = np.arange(sr) / sr
        lo = 0.3 * np.sin(2 * np.pi * 400 * t)
        hi = 0.3 * np.sin(2 * np.pi * 12000 * t)
        out = pnd.lowpass_brickwall((lo + hi).astype(np.float32), sr, 4000)
        spec = np.abs(np.fft.rfft(out))
        freqs = np.fft.rfftfreq(len(out), 1 / sr)

        def band_amp(f0):
            m = (freqs > f0 - 50) & (freqs < f0 + 50)
            return float(spec[m].max())

        assert band_amp(400) > 100 * band_amp(12000), "低频没留下或高频没清掉"

    def test_length_preserved(self):
        x = np.ones(3333, dtype=np.float32) * 0.1
        assert len(pnd.lowpass_brickwall(x, 16000, 4000)) == len(x)

    def test_cutoff_far_above_nyquist_is_noop(self):
        """截止频率高于有效带宽时不该改动信号（防止偏移/尺度 bug）。"""
        rng = np.random.default_rng(3)
        x = (rng.standard_normal(2048) * 0.1).astype(np.float32)
        out = pnd.lowpass_brickwall(x, 16000, 12000)
        assert float(np.max(np.abs(out - x))) < 1e-4


class TestMonotoneRule:
    def test_clean_sequence_passes(self):
        """干净退化：越劣化分越低 ⇒ 尺子有序。"""
        assert pnd.is_monotone_degradation([-1.5, -1.8, -2.5, -4.3, -8.4])

    def test_small_inversion_fails(self):
        """小幅劣化反而更高 ⇒ 必须判失败（正是袋鼠素材实测的形态）。"""
        assert not pnd.is_monotone_degradation([-1.105, -0.973, -1.088, -2.453, -8.386])

    def test_sine_not_lowest_fails(self):
        """连纯正弦都不是最低 ⇒ 失败（尺子完全没在量自然度）。"""
        assert not pnd.is_monotone_degradation([-1.5, -1.8, -2.5, -4.3, 0.0])

    def test_equal_values_pass(self):
        """相等算通过（平台不算退化）。"""
        assert pnd.is_monotone_degradation([-2.0, -2.0, -2.0, -2.0, -8.0])
