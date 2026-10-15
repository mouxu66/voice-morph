# -*- coding: utf-8 -*-
"""`tools/lead_sep_runner.py` 的纯函数守卫。

守 `_last_stem`：audio-separator 在串联/重跑时的复合文件名里可能同时含
(Vocals) 和 (Instrumental)，归类必须以**最后出现**的标记为准 —— 否则会把
"上一级主唱经下一级被判成伴奏"的文件错当主唱（2026-10-15 实测踩过：
简单 glob `*(Vocals)*` 会误中 `x_(Vocals)_A_(Instrumental)_B.wav`）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _load_runner():
    spec = importlib.util.spec_from_file_location(
        "lead_sep_runner_t", _ROOT / "tools" / "lead_sep_runner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_last_stem_simple():
    r = _load_runner()
    assert r._last_stem("song_(Vocals)_X.wav") == "Vocals"
    assert r._last_stem("song_(Instrumental)_X.wav") == "Instrumental"


def test_last_stem_uses_last_marker_in_compound_name():
    r = _load_runner()
    # 串联：先是上级 Vocals，再被下一级判成 Instrumental → 最终是伴奏
    assert r._last_stem(
        "in_(Vocals)_UVR_Main_(Instrumental)_KARA2.wav") == "Instrumental"
    # 反之，最后是 Vocals → 主唱
    assert r._last_stem(
        "in_(Instrumentalsrc)_(Vocals)_KARA.wav") == "Vocals"
