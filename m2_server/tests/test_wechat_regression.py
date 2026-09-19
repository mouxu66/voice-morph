"""tools/wechat_regression.py 单测 —— 只测纯判据，不碰真机微信。

回归脚本的价值全在"判得准"：三条判据（终态/时长/清场）只要有一条判错，
它就会把「其实发失败」报成通过（或反过来制造假警报）。这里把判据从 IO 里
拆出来单独锁死，真机部分交给 tools/wechat_regression.py 自己跑。

背景见 docs/犯错指南.md §2.2（60" 截断）、§2.12（静音头被录进去）、
§3.3（outcome=ok 不代表真收到）。
"""

import importlib.util
import sys
import wave
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "wechat_regression.py"


def _load():
    spec = importlib.util.spec_from_file_location("wechat_regression", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wechat_regression"] = mod
    spec.loader.exec_module(mod)
    return mod


wr = _load()


# ---------------- parse_voice_secs ----------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ('语音15"秒', 15.0),
        ('语音7"秒', 7.0),
        ('语音60"秒', 60.0),
        ("15", 15.0),
        (None, None),
        ("", None),
        ("没有数字", None),
    ],
)
def test_parse_voice_secs(name, expected):
    assert wr.parse_voice_secs(name) == expected


# ---------------- 判据 1：终态 ----------------


def test_terminal_passes_on_send_step():
    ok, detail = wr.judge_terminal(["已点 ↑ 绿钮发送", "声卡已还原"], "ok")
    assert ok and "已点" in detail


@pytest.mark.parametrize("steps", [["正在播放"], []])  # 代码走完但没到发送终态
def test_terminal_fails_without_send_step_even_if_outcome_ok(steps):
    ok, detail = wr.judge_terminal(steps, "ok")
    assert not ok and "终态" in detail


def test_terminal_ok_even_if_outcome_not_ok():
    """判据看 steps 终态而非 outcome：引导式手动发送（§2.10）也算走完。"""
    ok, _ = wr.judge_terminal(["已点 ↑ 绿钮发送"], "manual_fallback")
    assert ok


# ---------------- 判据 2：时长 ----------------


def test_duration_within_tolerance():
    ok, detail = wr.judge_duration(3.0, 4.0)  # 3 + 1.2 = 4.2，读到 4 → 过
    assert ok and "4" in detail


def test_duration_flags_60s_truncation():
    """最贵的那个坑：显示 60" 说明录音的按下状态从未解除。"""
    ok, detail = wr.judge_duration(3.0, 60.0)
    assert not ok and "截断" in detail


def test_duration_missing_reading_fails():
    ok, detail = wr.judge_duration(3.0, None)
    assert not ok and "读不到" in detail


def test_duration_too_long_flags_playback_lag():
    """静音头/播放启动被录进去（§2.12）：偏大而不是偏小。"""
    ok, detail = wr.judge_duration(3.0, 12.0)
    assert not ok and "静音头" in detail


# ---------------- 判据 3：清场 ----------------


def test_no_overlay_passes_when_clean():
    ok, detail = wr.judge_no_overlay(None, None, uia_ready=True)
    assert ok and "无挂起浮层" in detail


@pytest.mark.parametrize(
    "overlay,green",
    [
        ((1, 2, 3, 4), None),
        (None, (5, 6)),
        ((1, 2, 3, 4), (5, 6)),
    ],
)
def test_no_overlay_fails_on_either_signal(overlay, green):
    ok, detail = wr.judge_no_overlay(overlay, green, uia_ready=False)
    assert not ok and "挂起录音" in detail


# ---------------- 合成测试音频 ----------------


def test_make_test_wav_duration_and_format(tmp_path):
    p = wr.make_test_wav(tmp_path / "t.wav", secs=2.5, sr=24000)
    with wave.open(str(p), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 24000
        assert w.getnframes() == int(24000 * 2.5)


def test_make_test_wav_is_not_silent(tmp_path):
    """纯静音会被微信当"没说话"处理，回归必须发得出东西。"""
    p = wr.make_test_wav(tmp_path / "t.wav", secs=1.0, sr=16000)
    assert max(p.read_bytes()[44:]) > 0


# ---------------- 汇总 ----------------


def test_overall_aggregates_and_picks_failed_names():
    js = [{"name": "终态", "ok": True, "detail": "x"}, {"name": "时长", "ok": False, "detail": "y"}]
    ok, summary = wr.overall(js)
    assert not ok and "时长" in summary and "终态" not in summary


def test_overall_all_pass():
    ok, summary = wr.overall([{"name": "清场", "ok": True, "detail": "z"}])
    assert ok and "正常" in summary


# ---------------- 参数守门（最便宜的失败路径） ----------------


def test_too_short_audio_rejected_before_touching_wechat():
    """<1.5s 直接被参数校验拦下（微信最短 1 秒），不碰任何真机资源。"""
    assert wr.main(["--secs", "0.5"]) == 2
