# -*- coding: utf-8 -*-
"""`experiments/cover_singdom_dataset.py` 的计划层守卫（不写盘、不训练）。

训练集构建错了只能等 25 分钟训练完才发现，所以权重/时长/占比必须在写盘前用断言钉住。
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


ds = _load("cover_singdom_dataset_under_test", "cover_singdom_dataset.py")


def test_plan_keeps_all_base_clips_and_oversamples_singing():
    plan = ds.build_plan()
    base = [r for r in plan if r["tag"] == "base"]
    assert len(base) == len(sorted(ds.BASE.glob("*.wav"))) > 80
    assert all(r["weight"] == 1 and not r["singing"] for r in base)

    counts = {}
    for r in plan:
        if r["tag"] != "base":
            counts[r["tag"]] = counts.get(r["tag"], 0) + 1
    assert counts == {"sing": 3, "glide": 3, "falsetto488": 3, "sustain245": 1}


def test_singing_share_is_about_a_third():
    cov = ds.coverage(ds.build_plan())
    assert 600 < cov["total_seconds"] < 1500
    assert 0.20 < cov["singing_share"] < 0.30, cov


def test_plan_names_are_unique():
    names = [r["name"] for r in ds.build_plan()]
    assert len(names) == len(set(names))


def test_extra_sources_exist():
    for src, *_ in ds.EXTRA:
        assert src.exists(), src
