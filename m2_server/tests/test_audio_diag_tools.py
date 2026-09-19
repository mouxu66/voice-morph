"""tools/ 下音频诊断工具的冒烟测试。

只覆盖纯逻辑（关键词匹配 / 信号生成 / 注册表取值兜底），
不做真实音频 IO —— 那需要真实声卡且会占住设备。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def diag():
    return _load("cable_diag")


@pytest.fixture(scope="module")
def watch():
    return _load("cable_watch")


def test_interest_keywords_match_virtual_devices(diag):
    assert diag._match("CABLE Output (VB-Audio Virtual Cable)")
    assert diag._match("扬声器 (VB-Audio Virtual Cable)")
    assert diag._match("麦克风 (Steam Streaming Microphone)")
    # 物理设备不应被当成虚拟设备
    assert not diag._match("麦克风阵列 (Senary Audio)")


def test_tone_shape_and_nonzero(diag):
    sr, ch, sec = 48000, 2, 0.5
    buf = diag._tone(sr, ch, sec)
    assert buf.shape == (sr * sec, ch)
    assert buf.dtype == np.float32
    assert np.abs(buf).max() > 1e-3  # 有信号
    assert np.abs(buf).max() <= 1.0  # 未削波


def test_tone_is_silent_free_edge(diag):
    """首尾不得有突然跳变, 否则 intro RMS 判断会被数字冲击误导。"""
    buf = diag._tone(16000, 1, 0.5)[:, 0]
    assert abs(buf[0]) < 0.05


def test_name_of_tolerates_garbage_input(watch):
    """注册表缺失 / 空 ID 时必须返回 None 而不是抛异常。"""
    assert watch.name_of("") is None
    assert watch.name_of(None) is None
    assert watch.name_of("SWD\\MMDEVAPI\\{deadbeef}") is None


def test_role_names_cover_all_core_audio_roles(watch):
    assert watch.ROLE_NAMES == {0: "Console", 1: "Multimedia", 2: "Communications"}


def test_diag_has_all_scan_dimensions(diag):
    for fn in ("scan_apis", "scan_formats", "scan_channels", "scan_pair"):
        assert callable(getattr(diag, fn, None)), f"缺少扫描维度 {fn}"
