# -*- coding: utf-8 -*-
"""`experiments/cover_seedvc_sweep.py`（Seed-VC 参照窗口扫描）的守卫。

这一轮的核心变量不是模型，而是**拿他哪 25 秒当参照**。所以守两件事：

1. 扫描格里的窗口必须落在真唱时长内、key 不重复；
2. 「切窗口」和「去静音拼接」不能用错单位（秒 vs 采样点），
   也不能拼出一条超过 Seed-VC 25 秒上限的参照。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments"
SCRIPT = EXP / "cover_seedvc_sweep.py"


def _mod(name: str, path: Path):
    sys.path.insert(0, str(EXP))
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def sw():
    return _mod("cover_seedvc_sweep_under_test", SCRIPT)


def test_arms_are_valid_and_unique(sw):
    keys = [k for k, _s, _c, _n in sw.ARMS]
    assert len(keys) == len(set(keys)) == 5
    for _k, spec, cfg, steps in sw.ARMS:
        assert 0.0 <= cfg <= 1.2 and steps >= 20
        if spec != "concat":
            a, b = spec
            assert 0 <= a < b <= 73.75, f"窗口 {spec} 超出真唱时长"


def test_make_ref_cuts_by_seconds(sw):
    sr = 16000
    y = np.concatenate([np.full(sr * 3, 0.1, np.float32),
                        np.full(sr * 2, 0.3, np.float32)])
    got = sw.make_ref(y, sr, (3.0, 5.0))
    assert abs(len(got) - 2 * sr) <= 2
    assert np.allclose(got, 0.3, atol=1e-3), "窗口切错位置（切到了前一段）"


def test_make_ref_concat_drops_silence_and_fits_limit(sw):
    sr = 16000
    t = np.arange(sr * 70) / sr
    y = (0.2 * np.sin(2 * np.pi * 180 * t)).astype(np.float32)
    # 每 10 秒塞 1 秒真静音
    for s in range(0, 70, 10):
        y[s * sr:(s * sr) + sr] = 0.0
    got = sw.make_ref(y, sr, "concat")
    assert 0 < len(got) <= int(sw.CONCAT_MAX_S * sr) + 2
    # 拼出来的每一帧都不该是静音（容差按 librosa 的 top_db 给）
    n = (len(got) // 512) * 512
    rms = np.sqrt(np.mean(got[:n].reshape(-1, 512) ** 2, axis=1))
    # 拼接后静音帧应几乎消失（允许切点处各留 1 帧边界）
    assert np.mean(rms > 1e-3) > 0.95, "拼接里还留着成片的静音段"
