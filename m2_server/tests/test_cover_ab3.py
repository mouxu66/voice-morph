# -*- coding: utf-8 -*-
"""`experiments/cover_ab3.py`（三臂盲听：RVC / Seed-VC 真唱参照 / DDSP）的守卫。

三臂这一页比上一版更容易出事：它把**不同目录**的音频拿进同一个盲听页，
路径一写错就会静默换成另一条臂，盲听结论直接作废。这里守三件事：

1. 三条臂 key 不重复，且各自指向明确文件（不是同一个文件）；
2. 映射 `ab3.json` 只写 key，不把 `path` / 人话说明泄露进 rows；
3. 页面只有甲乙丙三条，且不出现任何臂名。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments"
SCRIPT = EXP / "cover_ab3.py"


def _mod(name: str, path: Path):
    sys.path.insert(0, str(EXP))
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def ab3():
    return _mod("cover_ab3_under_test", SCRIPT)


@pytest.fixture()
def rows():
    return [
        {"key": "rvc_singdom", "path": "/tmp/a.wav", "f0": 186.1,
         "hf_db": -82.4, "cam_anchor": 0.717, "cam_speech_ref": 0.134},
        {"key": "seedvc_true_sing", "path": "/tmp/b.wav", "f0": 186.1,
         "hf_db": -82.3, "cam_anchor": 0.707, "cam_speech_ref": 0.137},
        {"key": "ddsp_2700", "path": "/tmp/c.wav", "f0": 186.1,
         "hf_db": -87.6, "cam_anchor": 0.297, "cam_speech_ref": 0.475},
    ]


def test_three_arms_are_distinct(ab3):
    """三臂必须是三条不同来源，不能有两条指到同一个 wav。"""
    keys = [k for k, _p, _n in ab3.ARMS]
    paths = [str(p) for _k, p, _n in ab3.ARMS]
    assert len(keys) == 3 and len(set(keys)) == 3
    assert len(set(paths)) == 3, "两条臂指向了同一个文件，盲听会重复"


def test_key_keeps_map_only(ab3, rows):
    """`ab3.json` 的 rows 不许带 path/说明，且映射不重不漏。"""
    key = ab3.build_key(rows, [0, 1, 2])
    assert len(key["map"]) == 3 == len(set(key["map"].values()))
    for row in key["rows"].values():
        assert "path" not in row and "key" not in row
    assert key["mapfile"] == "ab3.json"
    dumped = json.dumps(key, ensure_ascii=False)
    assert "rvc_singdom" in dumped and "ddsp_2700" in dumped


def test_page_hides_arm_names(ab3, rows, tmp_path, monkeypatch):
    """页面只给甲乙丙，不能泄露 RVC / Seed-VC / DDSP，也不能出现文件名。"""
    monkeypatch.setattr(ab3.AB, "_encode", lambda *a, **k: None)
    anchor = tmp_path / "anchor.wav"
    anchor.write_bytes(b"")
    key = ab3.build_key(rows, [0, 1, 2])
    page = ab3.AB.build_page(rows, anchor, key, tmp_path)
    html = page.read_text(encoding="utf-8")
    for bad in ("singdom", "seedvc", "seed-vc", "Seed-VC", "RVC", "ddsp", "DDSP", "wav"):
        assert bad not in html, f"页面泄露了 {bad!r}"
    assert "3 条盲听" in html and "clips/1.mp3" in html and "clips/3.mp3" in html
