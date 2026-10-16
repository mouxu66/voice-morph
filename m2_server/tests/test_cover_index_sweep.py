# -*- coding: utf-8 -*-
"""`experiments/cover_index_sweep.py` 的守卫测试。

守什么（错了不会报错、只会给出错结论的地方，与 `test_cover_ckpt_sweep.py` 同族）：

1. **单变量成立的形态前提**：一档一个名字、名字唯一（重名会让体检条目互相覆盖，
   页面照出、metrics 照写 —— 那是 §8.21 白绿病）；`--rates` 解析必须可复算，
   空输入必须显式拒绝（不许"0 档空跑退出 0"）。
2. **报告形状与 cover_metrics 的键名契约**：`configs/[name/why/wav]` 一旦改口，
   体检会**静默零打分**（同族教训见 test_cover_ckpt_sweep 文件头）。
3. 真产物回归：本机跑过的 index_sweep.json 必须仍能被 `normalize_configs`
   规整出非空行（产物在 media/，不进仓库 ⇒ 不存在就跳过，不假装通过）。

跑法（--fast，不 import torch）：
    .venv/Scripts/python.exe -m pytest m2_server/tests/test_cover_index_sweep.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SWEEP = _ROOT / "experiments" / "cover_index_sweep.py"
_METRICS = _ROOT / "experiments" / "cover_metrics.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_parse_rates_and_unique_names():
    """★ 单变量前提：两档两名字、互不相同；空输入返回空表（由 main 拒绝）。"""
    sw = _load("cover_index_sweep_t1", _SWEEP)
    rates = sw.parse_rates("0.5,0.7")
    assert rates == [0.5, 0.7], rates
    names = [sw.config_name(i, r) for i, r in enumerate(rates)]
    assert names == ["A_index0.5", "B_index0.7"], names
    assert len(names) == len(set(names)), "档位名字必须唯一"
    # 空输入不许被静默当成"跑了个空表"
    assert sw.parse_rates("") == []
    assert sw.parse_rates(" , ") == []


def test_default_rates_are_current_and_candidate():
    """默认扫的正是「现役 0.5 vs 候选 0.7」两档 —— 改默认值要同步改文档口径。"""
    sw = _load("cover_index_sweep_t2", _SWEEP)
    assert sw.parse_rates(sw.DEFAULT_RATES) == [0.5, 0.7]


def test_report_shape_feeds_cover_metrics(tmp_path):
    """报告行必须能被 cover_metrics.normalize_configs 规整出来（键名契约）。"""
    cm = _load("cover_index_sweep_t3_metrics", _METRICS)
    w = tmp_path / "a.wav"
    w.write_bytes(b"x")
    rep = {
        "voice": "v",
        "seed": 1234,
        "pitch": -12,
        "configs": [{"name": "A_index0.5", "why": "why", "wav": str(w)}],
    }
    got = [r["name"] for r in cm.normalize_configs(rep)]
    assert got == ["A_index0.5"], got


def test_real_index_sweep_report_on_disk():
    """真产物回归：本机跑过的两份 index_sweep.json 必须仍能规整出非空行。"""
    cm = _load("cover_index_sweep_t4_metrics", _METRICS)
    found = 0
    for rel in (
        "media/covers/index_ab/t03/index_sweep.json",
        "media/covers/index_ab/supp/index_sweep.json",
    ):
        p = _ROOT / rel
        if not p.exists():
            continue
        rows = cm.normalize_configs(json.loads(p.read_text(encoding="utf-8")))
        assert rows, f"{rel} 规整出 0 行 —— 键名是不是改了？"
        found += 1
    if not found:
        pytest.skip("本机没有 index 扫描产物（media/ 不进仓库）")
