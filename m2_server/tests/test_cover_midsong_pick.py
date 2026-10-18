"""守卫 `experiments/cover_midsong_pick.py`：选曲口径的纯函数。

★ 这个文件存在的主要理由是**防止旧错误复辟**：
  * 10-18 之前"模型天花板 ~272Hz"被当成常量写进判断，实际那是 `pitch=-12` 的产物；
  * 音域统计如果退回整轨 min/max，会立刻把《好久不见》读成 C2–C5（#183）。

所以下面有两类测试：
  ① 纯函数行为（音名、半音、窗中位、连续段）；
  ② **回归锚点**：用本机实测过的真实数字当断言，谁改坏了在这里断。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "experiments"))

from cover_midsong_pick import (  # noqa: E402
    DEFAULT_COLLAPSE_HZ,
    RangeSummary,
    collapse_stats,
    gate_threshold,
    high_run_stats,
    hz_to_note,
    longest_run,
    semitones_between,
    summarize,
    window_medians,
)

# ---------------------------------------------------------------- 音名 / 半音

@pytest.mark.parametrize(
    "hz,expect",
    [
        (440.0, "A4"),
        (261.63, "C4"),
        (146.83, "D3"),      # 《好久不见》中位
        (182.8, "F#3"),      # 《成都》中位（实测）
        (333.3, "E4"),       # 《唯一》高音段（实测）
        (108.1, "A2"),       # supp2026 训练集 p50
    ],
)
def test_hz_to_note_known_pitches(hz, expect):
    assert hz_to_note(hz) == expect


def test_hz_to_note_is_ascii_only():
    """★ Windows 控制台是 GBK：返回 `F♯3` 会让 print 直接抛 UnicodeEncodeError。

    本轮实测：6 首普查跑完、最后一行汇总时整脚本崩掉，前面的下载与分离全白跑。
    """
    for hz in (185.0, 233.1, 277.2, 311.1):
        note = hz_to_note(hz)
        assert note.isascii(), f"{hz}Hz → {note!r} 含非 ASCII 字符"


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_hz_to_note_handles_degenerate(bad):
    assert hz_to_note(bad) == "?"


def test_semitones_between_matches_known_gap():
    """《好久不见》145.9Hz → 《成都》182.8Hz 实测 3.9 个半音（页面就是这么写的）。"""
    assert semitones_between(145.9, 182.8) == pytest.approx(3.9, abs=0.05)
    # 八度整
    assert semitones_between(220.0, 440.0) == pytest.approx(12.0, abs=1e-9)
    # 同频
    assert semitones_between(200.0, 200.0) == pytest.approx(0.0, abs=1e-9)


def test_semitones_between_degenerate_returns_nan():
    assert np.isnan(semitones_between(0.0, 100.0))
    assert np.isnan(semitones_between(100.0, 0.0))


# ---------------------------------------------------------------- 静音门

def test_gate_threshold_is_p20_times_two():
    rms = np.linspace(0.0, 1.0, 1001)
    expected = np.percentile(rms, 20) * 2.0
    assert gate_threshold(rms) == pytest.approx(expected)


def test_gate_threshold_empty_is_zero():
    assert gate_threshold(np.array([])) == 0.0


def test_gate_removes_quiet_intro_noise():
    """#183 的核心：间奏/开头的低电平残留必须被门挡掉。

    构造：前面 1s 极轻（RMS 0.001）+ 后面 1s 正常电平（RMS 0.1）。
    ★ 必须传**正值包络**（真实 RMS 恒 ≥0）——本轮第一版拿有符号噪声当 RMS，
      p20 落在负数上，门变成负值，断言就变成在测"负数小于正数"这种废话。
    """
    rng = np.random.default_rng(0)
    quiet = 0.001 * (1.0 + 0.05 * rng.standard_normal(100))  # 贴在 0.001 附近、恒正
    loud = 0.1 * (1.0 + 0.05 * rng.standard_normal(100))
    rms = np.concatenate([quiet, loud])
    gate = gate_threshold(rms)
    assert 0.001 < gate < 0.1, f"门应落在两段之间，实测 {gate}"
    assert quiet.mean() < gate < loud.mean()


# ---------------------------------------------------------------- 窗中位

def test_window_medians_takes_median_not_mean():
    """颤音尖峰会把均值抬高 —— 所以口径是**中位**。"""
    hop_s = 0.01  # 10ms/帧
    n = 100        # 1s
    f0 = np.full(n, 200.0)
    f0[::10] = 800.0  # 每 10 帧一个尖峰
    rms = np.full(n, 0.5)
    meds = window_medians(f0, rms, hop_s=hop_s, window_s=0.5)
    assert meds.size == 2
    assert np.allclose(meds, 200.0), "尖峰不该把窗中位抬走"


def test_window_medians_skips_windows_below_voiced_fraction():
    hop_s = 0.01
    n = 200
    f0 = np.full(n, np.nan)
    # 第一个 0.5s 窗只有 1 帧有声（1% < 20%），第二个窗有 60%
    f0[3] = 200.0
    f0[100:130] = 210.0
    rms = np.full(n, 0.5)
    meds = window_medians(f0, rms, hop_s=hop_s, window_s=0.5, min_voiced_frac=0.2)
    assert meds.size == 1
    assert meds[0] == pytest.approx(210.0)


def test_window_medians_respects_gate():
    """200 帧 × 0.01s = 2s；0.5s 窗 ⇒ 4 个窗。前 1s 低于门 ⇒ 只留后 2 个。"""
    hop_s = 0.01
    n = 200
    f0 = np.full(n, 150.0)
    rms = np.full(n, 0.5)
    rms[:100] = 0.001  # 前半段低于门
    meds = window_medians(f0, rms, hop_s=hop_s, window_s=0.5, gate=0.01)
    assert meds.size == 2, "只有通过门的那两窗该被采信"


def test_window_medians_gate_is_compared_to_mean_rms():
    """门比的是**窗内 RMS 均值**，不是逐帧。整窗刚好过线 vs 刚好不过线。

    100 帧 × 0.01s = 1s；0.5s 窗 ⇒ 2 个窗，RMS 全为 0.02。
    """
    hop_s = 0.01
    n = 100
    f0 = np.full(n, 150.0)
    rms = np.full(n, 0.02)
    # 均值 0.02 < 门 0.021 → 两窗全被挡
    assert window_medians(f0, rms, hop_s=hop_s, window_s=0.5, gate=0.021).size == 0
    # 均值 0.02 > 门 0.019 → 两窗全保留
    assert window_medians(f0, rms, hop_s=hop_s, window_s=0.5, gate=0.019).size == 2


def test_window_medians_mismatched_lengths_are_truncated():
    hop_s = 0.01
    f0 = np.full(100, 200.0)
    rms = np.full(60, 0.5)  # 比 f0 短
    meds = window_medians(f0, rms, hop_s=hop_s, window_s=0.5)
    assert meds.size == 1, "按较短者截断，不该越界"


# ---------------------------------------------------------------- 音域摘要

def test_summarize_uses_p05_p95_not_extremes():
    """#183 的另一半：即便加了门，也不能用 min/max —— 用 p05/p95。"""
    arr = np.concatenate([
        np.full(50, 500.0),   # 少数极值
        np.random.default_rng(1).normal(180.0, 15.0, 900),
        np.full(50, 60.0),    # 少数极值
    ])
    s = summarize(arr)
    assert s.p05_hz > 100.0, "p05 不该被 60Hz 的极值拉走"
    assert s.p95_hz < 400.0, "p95 不该被 500Hz 的极值拉走"
    assert s.p50_hz == pytest.approx(180.0, abs=5.0)


def test_summarize_empty_is_nan_not_exception():
    s = summarize(np.array([]))
    assert s.n_win == 0
    assert np.isnan(s.p50_hz)
    assert np.isnan(s.span_semi)


def test_summarize_all_nan_input_is_empty():
    s = summarize(np.full(10, np.nan))
    assert s.n_win == 0


def test_summarize_span_matches_real_octave():
    arr = np.array([220.0] * 10 + [440.0] * 10)
    s = summarize(arr)
    assert s.span_semi == pytest.approx(12.0, abs=0.1)


def test_range_summary_notes_are_consistent_with_percentiles():
    s = RangeSummary(n_win=10, p05_hz=146.8, p50_hz=182.8, p95_hz=311.0, span_semi=13.0)
    notes = s.notes()
    assert notes == {"lo": "D3", "mid": "F#3", "hi": "D#4"}


# ---------------------------------------------------------------- 连续段

def test_longest_run_finds_longest_block():
    hop_s = 0.5
    mask = np.zeros(20, dtype=bool)
    mask[4:6] = True    # 1.0s @2.0s
    mask[10:16] = True  # 3.0s @5.0s
    dur, at = longest_run(mask, hop_s)
    assert dur == pytest.approx(3.0)
    assert at == pytest.approx(5.0)


def test_longest_run_all_false_and_empty():
    assert longest_run(np.zeros(10, dtype=bool), 0.5) == (0.0, 0.0)
    assert longest_run(np.array([], dtype=bool), 0.5) == (0.0, 0.0)


def test_longest_run_all_true():
    dur, at = longest_run(np.ones(6, dtype=bool), 0.5)
    assert dur == pytest.approx(3.0)
    assert at == pytest.approx(0.0)


# ---------------------------------------------------------------- 崩坏区

def test_collapse_stats_separates_continuous_from_spikes():
    """★ 这是本模块存在的理由：同样 30% 的占比，决策完全不同。

    A：整段坐在 350Hz（前 12s，200 帧 × 0.1s）
    B：每 0.2s 闪一下 350Hz（最长 0.1s）
    """
    hop_s = 0.1
    n = 200  # 20s

    a = np.full(n, 350.0)
    a[120:] = 180.0
    ok_a = np.ones(n, dtype=bool)
    sa = collapse_stats(a, ok_a, hop_s=hop_s, collapse_hz=300.0)

    b = np.full(n, 180.0)
    b[::2] = 350.0  # 每隔一帧闪一下
    ok_b = np.ones(n, dtype=bool)
    sb = collapse_stats(b, ok_b, hop_s=hop_s, collapse_hz=300.0)

    assert sa["ge_pct"] == pytest.approx(60.0, abs=0.5)
    assert sa["run_s"] == pytest.approx(12.0, abs=0.15), "A 应读到 120 帧 = 12s 连续"
    assert sa["run_at_s"] == pytest.approx(0.0)
    assert sb["ge_pct"] == pytest.approx(50.0, abs=0.5)
    assert sb["run_s"] == pytest.approx(0.1, abs=0.01), "B 只是闪点，最长仅 0.1s"


def test_collapse_stats_ignores_unvoiced_even_if_f0_high():
    """无声帧即使残留了 400Hz 的 f0，也不该算进崩坏区（#183 同族）。"""
    hop_s = 0.1
    f0 = np.array([400.0, 400.0, 180.0, 180.0])
    ok = np.array([False, False, True, True])
    s = collapse_stats(f0, ok, hop_s=hop_s, collapse_hz=300.0)
    assert s["n"] == 2, "只有 2 帧是有效的"
    assert s["ge_pct"] == pytest.approx(0.0)


def test_collapse_stats_no_valid_frames():
    s = collapse_stats(np.array([np.nan, np.nan]), np.array([False, False]), hop_s=0.1)
    assert s["n"] == 0
    assert np.isnan(s["ge_pct"])


def test_default_collapse_hz_is_documented_as_not_a_ceiling():
    """★ 防止旧错误复辟：300Hz 只是历史口径，**不是模型天花板**。

    10-15 记的「输出 ≥300Hz 有效周期帧全为 0、最高 ~272Hz」来自 `pitch=-12` 的产物。
    同一输入 `pitch=0` 实测输出中位 351Hz、周期度 0.942（2026-10-18 复核）。
    谁要是把这个常量当成物理上限来用，这里会提醒他。
    """
    assert DEFAULT_COLLAPSE_HZ == 300.0


def test_high_run_stats_threshold_edges():
    hop_s = 0.5
    f0 = np.array([299.0, 300.0, 301.0, 250.0])
    ok = np.ones(4, dtype=bool)
    dur, at = high_run_stats(f0, ok, hop_s=hop_s, hz=300.0)
    assert dur == pytest.approx(1.0), ">=300 含等于"
    assert at == pytest.approx(0.5)


# ---------------------------------------------------------------- 回归锚点

def test_regression_anchor_chengdu_vs_lowsong():
    """本机实测锚点：成都 D3–D#4 中位 F#3，好久不见 A2–G3 中位 D3。

    这两条写进过交付文档，谁改了统计口径在这里立刻断。
    """
    assert hz_to_note(145.9) == "D3"
    assert hz_to_note(182.8) == "F#3"
    assert hz_to_note(302.5) == "D#4"
    assert hz_to_note(195.7) == "G3"
    assert hz_to_note(109.3) == "A2"
    # 两首之间的音高差（页面写 3.9 个半音）
    assert semitones_between(145.9, 182.8) == pytest.approx(3.9, abs=0.05)
