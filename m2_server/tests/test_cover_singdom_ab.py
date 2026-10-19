# -*- coding: utf-8 -*-
"""`experiments/cover_singdom_ab.py` 的结构守卫（不含 GPU / ffmpeg）。

最贵的一类错是**拿训练集里的素材去 A/B**：那样比的只是"这条背得多熟"，
过采样那一版会天然赢，而结论会被当成"域权重有用"。这个错在跑之前看不出来，
跑完也看不出来（页面照常有数字），所以钉在这里。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_EXP = _ROOT / "experiments"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _EXP / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ab = _load("cover_singdom_ab_under_test", "cover_singdom_ab.py")


def test_default_input_is_outside_the_training_set():
    """A/B 输入必须是**训练集外**的歌声（否则结论无效，见模块头）。"""
    p = ab.DEFAULT_INPUT.resolve()
    assert p.exists(), f"A/B 输入不存在：{p}"
    low = str(p).lower()
    assert "dataset_raw" not in low and "singdom" not in low, f"输入落在训练集里：{p}"


def test_voices_are_one_new_one_baseline():
    tags = [t for t, _ in ab.VOICES]
    voices = [v for _, v in ab.VOICES]
    assert len(set(voices)) == 2, "两个版本必须是不同模型"
    assert any("singdom" in v for v in voices) and any("supp2026" in v for v in voices), voices
    assert len(set(tags)) == 2


def test_rows_cover_input_and_both_arms():
    keys = [k for k, _, _ in ab.ROWS]
    assert keys[0] == "input", "首行必须是输入本身（用户靠听它判断参照）"
    for tag, _ in ab.VOICES:
        assert tag in keys, f"{tag} 没有出现在页面的行里"


def test_pitch_and_index_are_fixed_single_variable():
    """两版模型必须同 pitch/同 index（单变量：只改权重）。"""
    assert ab.PITCH == 0 and ab.INDEX_RATE == 0.5
