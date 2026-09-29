"""实测推理耗时（`rvc_live.infer_latency` / `parse_infer_ms`）—— 桌宠状态条上的那个数。

为什么值得钉：那个数**只用来给用户看**，所以它错的时候没有任何功能会坏 ——
只会让人对着一个假的"延迟"做判断（比如以为变声很卡，其实读到的是三天前的日志尾巴）。
而且它靠**扫日志文本**取值，格式一变（全角冒号、空格、"秒"字改写）就会静默返回空，
于是界面上那个数字悄悄消失，没人会知道。

口径也一并钉住：这里报的是**单块推理耗时**，不是端到端延迟；
端到端还要加档位的分块时长（另由 `block_ms()` 报出）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import live_settings  # noqa: E402
import rvc_live  # noqa: E402


# ------------------------------------------------------------ 文本解析（纯函数）

def test_parses_fullwidth_colon_line():
    assert rvc_live.parse_infer_ms("推理耗时：0.08秒") == [80.0]


def test_parses_halfwidth_colon_and_spaces():
    """日志里两种冒号都出现过 —— 只认一种就会静默丢样本。"""
    assert rvc_live.parse_infer_ms("推理耗时: 0.35秒") == [350.0]


def test_parses_several_lines_in_order():
    text = "推理耗时：0.08秒\n别的日志\n推理耗时：0.10秒\n"
    assert rvc_live.parse_infer_ms(text) == [80.0, 100.0]


def test_ignores_unrelated_lines():
    text = "[headless] STREAM_UP\n模型已就绪\n"
    assert rvc_live.parse_infer_ms(text) == []


def test_malformed_number_is_skipped_not_crashed():
    assert rvc_live.parse_infer_ms("推理耗时：秒") == []
    assert rvc_live.parse_infer_ms("推理耗时：") == []


def test_mark_without_number_still_finds_later_lines():
    text = "推理耗时：秒\n推理耗时：0.09秒\n"
    assert rvc_live.parse_infer_ms(text) == [90.0]


# ------------------------------------------------------------ 读日志取统计

def _write_log(tmp_path: Path, lines: list[str]) -> Path:
    log = tmp_path / "realtime_gui.log"
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


@pytest.fixture()
def fake_exp(monkeypatch, tmp_path):
    """把 `_exp_dirs` 指到临时目录，避免碰真实音色的日志。"""

    def _set(log_dir: Path):
        monkeypatch.setattr(
            rvc_live, "_exp_dirs", lambda exp_name=None: ("fake_exp", log_dir, log_dir)
        )

    return _set


def test_median_and_p95_from_log(fake_exp, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write_log(log_dir, ["推理耗时：0.08秒"] * 9 + ["推理耗时：0.50秒"])
    fake_exp(log_dir)
    st = rvc_live.infer_latency()
    assert st["infer_samples"] == 10
    assert st["infer_ms"] == 80.0
    assert st["infer_ms_p95"] == 500.0


def test_only_tail_samples_are_used(fake_exp, tmp_path):
    """只统计最近 N 条：满载时的耗时会被历史的好数字淹没，那样读数就没意义了。"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write_log(log_dir, ["推理耗时：0.08秒"] * (rvc_live._INFER_TAIL_N + 50))
    fake_exp(log_dir)
    assert rvc_live.infer_latency()["infer_samples"] == rvc_live._INFER_TAIL_N


def test_missing_log_returns_empty(fake_exp, tmp_path):
    """没有日志（从未启动过实时变声）返回 {}，由消费方决定怎么显示 —— 不是 0ms。"""
    fake_exp(tmp_path / "nope")
    assert rvc_live.infer_latency() == {}


def test_log_without_samples_returns_empty(fake_exp, tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    _write_log(log_dir, ["[headless] 模型与推理管线已就绪"])
    fake_exp(log_dir)
    assert rvc_live.infer_latency() == {}


def test_read_error_is_swallowed(fake_exp, tmp_path):
    """状态端点不能因为读日志失败而 500（这里拿一个目录冒充日志文件）。"""
    log_dir = tmp_path / "logs"
    (log_dir / "realtime_gui.log").mkdir(parents=True)
    fake_exp(log_dir)
    assert rvc_live.infer_latency() == {}


# ------------------------------------------------------------ 分块时长

@pytest.mark.parametrize("profile,expect", [("balanced", 250), ("game", 350)])
def test_block_ms_follows_profile(monkeypatch, profile, expect):
    monkeypatch.setattr(live_settings, "get", lambda: {"perf_profile": profile})
    assert rvc_live.block_ms() == expect


def test_block_ms_unknown_profile_is_zero(monkeypatch):
    monkeypatch.setattr(live_settings, "get", lambda: {"perf_profile": "不存在"})
    assert rvc_live.block_ms() == 0


def test_block_ms_setting_error_is_zero(monkeypatch):
    def _boom():
        raise RuntimeError("设置文件坏了")

    monkeypatch.setattr(live_settings, "get", _boom)
    assert rvc_live.block_ms() == 0


def test_game_profile_blocks_longer_than_balanced():
    """档位语义：game 是"省资源换延迟"，分块更长 —— 这条推翻了就该有人来解释。"""
    game = rvc_live.PROFILE_TUNING["game"]["block_time"]
    balanced = rvc_live.PROFILE_TUNING["balanced"]["block_time"]
    assert game > balanced
