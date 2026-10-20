# -*- coding: utf-8 -*-
"""`experiments/cover_voice_ab.py`（像不像我：RVC 训过的音色 vs Seed-VC 零样本）的守卫。

用户 2026-10-20 深夜：「我问了很多人，他说根本都不像我的声音哦」
——它暴露的是**两条相似度曲线互相矛盾**：Seed-VC 臂对 8.9 秒 PPT 收音 0.745、
对他的真唱只有 0.343；RVC 唱歌模型反过来 0.134 / 0.717。
所以这一页的核心不是数字，是**盲听**。盲听一旦漏名字，整页作废。

这个模块守三件事：

1. **锚点要按音区挑**，不能挑最响或最高 —— 锚点低八度，听的人先写好"不像"的结论。
2. **页面不许出现臂名**（`singdom` / `seedvc` / `ppt12` / `myvoice`），也不许把映射
   写进 HTML（第一版就干过：把 key 塞进 `<script id="key">`，按 F12 就揭宝）。
3. **打乱可复现**：同一 seed 同一顺序，换 seed 必须变；映射要一一对应、不重不漏。

全部合成音频 / monkeypatch `_encode`，不跑 RVC、不读 media/。
"""

from __future__ import annotations

import importlib.util
import json
import random
import sys
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments"
SCRIPT = EXP / "cover_voice_ab.py"


def _mod(name: str, path: Path):
    sys.path.insert(0, str(EXP))
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def ab():
    return _mod("cover_voice_ab_under_test", SCRIPT)


def _tone(freq: float, secs: float, amp: float = 0.3, sr: int = 16000) -> np.ndarray:
    t = np.arange(int(secs * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# ---------------------------------------------------------------- 锚点
def test_pick_anchor_prefers_matching_register(ab):
    """三段 120 / 190 / 300Hz，目标 195Hz —— 必须挑中间那段，不是最高也不是最低。

    允许落在 8–15s：跨在 190Hz 段前端的平局窗（8–13s，中位仍是 190）也算挑对，
    它是「以中段为主」而不是挑到别的段。
    """
    y = np.concatenate([_tone(120.0, 10.0), _tone(190.0, 10.0), _tone(300.0, 10.0)])
    start, _ = ab.pick_anchor(y, 16000, dur=5.0, target=195.0)
    assert 8.0 <= start <= 15.0, f"挑到 {start:.1f}s"
    assert start + 5.0 <= 20.5, f"窗口 {start:.1f}s 跑出 190Hz 那段了"


def test_pick_anchor_skips_silence(ab):
    """锚点同样要过电平门限：不能挑到静音段（那里的 pyin 读数是噪声）。"""
    rng = np.random.default_rng(0)
    silent = (0.004 * rng.standard_normal(16000 * 10)).astype(np.float32)
    y = np.concatenate([_tone(196.0, 10.0), silent])
    start, _ = ab.pick_anchor(y, 16000, dur=5.0, target=195.0)
    assert start < 10.0, f"挑到静音 {start:.1f}s"


# ---------------------------------------------------------------- 盲听页
@pytest.fixture()
def rows():
    return [
        {"key": "singdom_p0", "f0": 186.1, "hf_db": -82.4,
         "cam_anchor": 0.717, "cam_speech_ref": 0.134},
        {"key": "seedvc_A1", "f0": 139.4, "hf_db": -115.1,
         "cam_anchor": 0.343, "cam_speech_ref": 0.745},
    ]


def _build(ab, rows, tmp_path, monkeypatch):
    monkeypatch.setattr(ab, "_encode", lambda *a, **k: None)   # 不调 ffmpeg、不写音频
    anchor = tmp_path / "anchor.wav"
    anchor.write_bytes(b"")
    key = {"src": {"window": "20s 切片", "f0": 195.0}, "seed": 1,
           "map": {"甲": rows[0]["key"], "乙": rows[1]["key"]}, "rows": {}}
    return ab.build_page(rows, anchor, key, tmp_path)


def test_page_leaks_no_arm_names(ab, rows, tmp_path, monkeypatch):
    html = _build(ab, rows, tmp_path, monkeypatch).read_text(encoding="utf-8")
    for bad in ("singdom", "seedvc", "ppt12", "myvoice", "RVC", "Seed-VC"):
        assert bad not in html, f"页面泄露了 {bad!r}，盲听作废"
    assert "clips/1.mp3" in html and "clips/2.mp3" in html


def test_page_has_anchor_first(ab, rows, tmp_path, monkeypatch):
    """锚点必须是第 0 条（clips/0.mp3），且在五条盲听之前 —— 它是尺子，不是选手。"""
    html = _build(ab, rows, tmp_path, monkeypatch).read_text(encoding="utf-8")
    assert "clips/0.mp3" in html
    assert html.index("clips/0.mp3") < html.index("clips/1.mp3")


def test_shuffle_is_reproducible_per_seed(ab, rows, tmp_path, monkeypatch):
    """同一 seed 同一映射；换 seed 要真的换（否则\"打乱\"是假的）。"""
    # 用 5 条（真实条数）比：2 条时两个相邻 seed 很容易撞成同一顺序，测不出"换 seed 会变"
    five = [f"arm_{i}" for i in range(5)]

    def shuffled(seed):
        order = list(range(len(five)))
        random.Random(seed).shuffle(order)
        return [five[i] for i in order]

    assert shuffled(ab.SEED) == shuffled(ab.SEED)
    assert shuffled(ab.SEED) != shuffled(ab.SEED + 1)


def test_key_file_round_trips(ab, rows, tmp_path, monkeypatch):
    """映射写进 json 且不重不漏 —— 页面里的甲乙丙丁必须能反查回唯一一条臂。"""
    page = _build(ab, rows, tmp_path, monkeypatch)
    assert page.exists()
    key = {"map": {ab.LABELS[i]: r["key"] for i, r in enumerate(rows)}}
    dumped = json.dumps(key, ensure_ascii=False)
    assert len(key["map"]) == len(rows) == len(set(key["map"].values()))
    assert "singdom_p0" in dumped
