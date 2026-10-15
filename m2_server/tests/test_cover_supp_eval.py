# -*- coding: utf-8 -*-
"""`experiments/cover_supp_eval.py` 的守卫（补录 A/B 汇总）。

`summarize()` 是这页 A/B 结论的唯一机械闸：三臂必须只有"权重"一列不同，
pitch/index_rate 任何一个串味，频段与 CAM++ 的差就可能来自第二变量。
第一版若不核对，报告照出、试听页照渲染，结论却不可信（同族：multi_pitch 的
单变量纪律、§8.21 白绿病）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_EXP = _ROOT / "experiments"
for _p in (_ROOT / "m2_server", _EXP):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load():
    spec = importlib.util.spec_from_file_location("cover_supp_eval", _EXP / "cover_supp_eval.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cover_supp_eval"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ev():
    return _load()


def _row(voice, pitch=-12, index_rate=0.5, b48=-30.0, b811=-45.0, rms=-20.0,
         cam_me=0.6, cam_orig=0.08):
    return {"name": voice, "voice": voice, "pitch": pitch, "index_rate": index_rate,
            "bands": {"rms_dbfs": rms, "4-8k_dbfs": b48, "8-11k_dbfs": b811},
            "cam_me": cam_me, "cam_orig": cam_orig}


def test_summarize_rejects_pitch_drift_across_arms(ev):
    rows = [_row("myvoice_tilt_t00"), _row("myvoice_tilt_t03"),
            _row("myvoice_supp2026", pitch=0)]
    with pytest.raises(ValueError, match="pitch"):
        ev.summarize(rows)


def test_summarize_rejects_index_rate_drift(ev):
    rows = [_row("myvoice_tilt_t00"), _row("myvoice_tilt_t03"),
            _row("myvoice_supp2026", index_rate=0.0)]
    with pytest.raises(ValueError, match="index_rate"):
        ev.summarize(rows)


def test_summarize_rejects_empty(ev):
    with pytest.raises(ValueError):
        ev.summarize([])


def test_summarize_deltas_are_against_t00_and_net_of_rms(ev):
    rows = [
        _row("myvoice_tilt_t00", b48=-32.0, b811=-50.0, rms=-21.0),
        _row("myvoice_tilt_t03", b48=-28.0, b811=-44.0, rms=-20.5),
        _row("myvoice_supp2026", b48=-26.0, b811=-40.0, rms=-20.0),
    ]
    s = ev.summarize(rows)
    assert s["base"] == "myvoice_tilt_t00" and s["shared"] == {"pitch": -12, "index_rate": 0.5}
    by = {r["voice"]: r for r in s["table"]}
    # t00 自身差为 0；supp 的 4-8k 相对 t00 +6 dB
    assert by["myvoice_tilt_t00"]["d48_vs_t00"] == 0.0
    assert by["myvoice_supp2026"]["d48_vs_t00"] == 6.0
    assert by["myvoice_supp2026"]["d811_vs_t00"] == 10.0
    # 净变化 = 频段 − 总电平（supp: -26 - -20 = -6）
    assert by["myvoice_supp2026"]["4-8k_net_vs_rms"] == -6.0
    # CAM++ 透传
    assert by["myvoice_supp2026"]["cam_me"] == 0.6
