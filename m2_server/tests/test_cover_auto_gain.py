"""翻唱人声/伴奏自动配平（cover_api.auto_vocal_gain / rms_db）单测。

背景：用户两轮真机听感都是"人声太小，听不出音色"—— RVC 换声输出电平
普遍低于 demucs 伴奏。2026-09-26 起翻唱链路默认按实测 RMS 自动配平
（POST /api/cover/run 新增 auto_gain，默认 True）。

这里只锁**纯函数**的数学与兜底行为；真音频路径不 mock 不起进程，
全链路另有真机验证（.workbuddy-ai/memory/2026-09-26.md）。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("numpy")

from cover_api import (  # noqa: E402
    AUTO_GAIN_LIMITS,
    AUTO_VOCAL_LEAD_DB,
    auto_vocal_gain,
    rms_db,
)


class TestAutoVocalGain:
    def test_already_louder_than_target_returns_below_one(self):
        """人声已比伴奏高 6dB（目标 2.5dB）→ 该压人声（增益 <1），不是只会放大。"""
        gain = auto_vocal_gain(vocal_db=-12.0, accomp_db=-18.0)
        # need = 2.5 - (−12−(−18)) = -3.5dB → 10^(-3.5/20) ≈ 0.668
        assert gain == pytest.approx(0.67, abs=0.01)

    def test_quieter_than_target_gets_boost(self):
        """人声比伴奏低 6dB → 需要补 8.5dB → ×2.66，正好落在夹紧范围内。"""
        gain = auto_vocal_gain(vocal_db=-24.0, accomp_db=-18.0)
        assert gain == pytest.approx(10 ** (8.5 / 20), abs=0.01)

    def test_exactly_at_target_returns_one(self):
        """人声恰好高目标 2.5dB → 不动。"""
        assert auto_vocal_gain(vocal_db=-15.5, accomp_db=-18.0) == 1.0

    def test_extreme_gap_clamped_high(self):
        """人声低 40dB（近静音轨）→ 理论要 ×100，必须被夹到上限。"""
        gain = auto_vocal_gain(vocal_db=-58.0, accomp_db=-18.0)
        assert gain == AUTO_GAIN_LIMITS[1]

    def test_extreme_gap_clamped_low(self):
        """人声高 40dB → 理论要压到 0.01，必须被夹到下限。"""
        gain = auto_vocal_gain(vocal_db=22.0, accomp_db=-18.0)
        assert gain == AUTO_GAIN_LIMITS[0]

    def test_unmeasurable_vocal_returns_neutral(self):
        """人声轨测不出（None）→ 返回 1.0 不乱动，而不是拍一个大概值。"""
        assert auto_vocal_gain(None, -18.0) == 1.0

    def test_unmeasurable_accomp_returns_neutral(self):
        assert auto_vocal_gain(-12.0, None) == 1.0

    def test_both_unmeasurable_returns_neutral(self):
        assert auto_vocal_gain(None, None) == 1.0

    def test_result_rounded_to_two_decimals(self):
        """ffmpeg volume= 的值保留两位小数就够，便于日志对账。"""
        gain = auto_vocal_gain(vocal_db=-23.333, accomp_db=-18.0)
        assert round(gain, 2) == gain

    def test_custom_target_respected(self):
        """目标提前量可调：0dB（齐平）时,同差应为 1.0 而不是默认的放大。"""
        assert auto_vocal_gain(-18.0, -18.0, target_lead_db=0.0) == 1.0


class TestRmsDb:
    def test_sine_amplitude_matches_reference(self, tmp_path):
        """幅值 0.5 的正弦波 RMS = 0.5/√2 → -3.01dBFS，误差 <0.05dB。"""
        np = pytest.importorskip("numpy")
        sf = pytest.importorskip("soundfile")
        p = tmp_path / "tone.wav"
        sr = 8000
        t = np.arange(sr) / sr
        sf.write(str(p), 0.5 * np.sin(2 * np.pi * 440 * t), sr)
        val = rms_db(p)
        assert val is not None
        assert val == pytest.approx(20 * __import__("math").log10(0.5 / 2**0.5), abs=0.05)

    def test_silent_returns_none(self, tmp_path):
        """全静音轨（RMS≈0）→ None，调用方按"不调"处理。"""
        np = pytest.importorskip("numpy")
        sf = pytest.importorskip("soundfile")
        p = tmp_path / "silence.wav"
        sf.write(str(p), np.zeros(8000, dtype="float32"), 8000)
        assert rms_db(p) is None

    def test_missing_file_returns_none(self, tmp_path):
        """文件不存在 → None（不抛异常 —— 量电平失败不该炸翻唱链路）。"""
        assert rms_db(tmp_path / "nope.wav") is None


def test_defaults_documented_intent():
    """目标提前量与夹紧范围的取值是拍过板的（人声清楚但不压伴奏），锁住防漂。"""
    assert AUTO_VOCAL_LEAD_DB == 2.5
    assert AUTO_GAIN_LIMITS == (0.5, 3.0)
