# -*- coding: utf-8 -*-
"""`experiments/cover_metrics.py` / `cover_knobs.py` 的守卫测试。

这两个脚本的作用是**给人下判断用的**（\"闷不闷 / 掺没掺原唱 / 这个旋钮有没有用\"），
所以它们自己错了比产品错了更危险：结论会带着一个假的数字写进文档，
而下一个人会把那个数字当证据引用。

本轮实测踩到并在这里钉死的失败方式：

1. **频段 dB 用了四舍五入后的比值** —— `band_8-11k` 那种近乎为空的频段，
   能量比是 1e-8 量级，先 `round(..., 5)` 变成 0.0，再取 log10 就得到一个
   **假的 -120 dB**；同一个文件用未舍入的值算是 -75.5 dB。差 44 dB，
   而且两次都\"看起来像个正经数字\"。
2. **dB 的参照系必须写清** —— 分频段能量比直接比（0.0002 vs 0.0109）读不出量级，
   所以报告以 0-4k 为 0 dB；那么\"4-8k 掉 18 dB\"才是可读、可复算的一句话。
3. **静音段会把配置之间的差异抹平** —— 20 秒里必然有换气与间奏，必须只统计有声帧，
   否则每个配置的比值都被拉向同一个数（差异消失，而不是被证明不存在）。

跑法（进 `--fast`，约 0.2s，纯 tmp_path + soundfile，不 import torch）：
    .venv/Scripts/python.exe -m pytest m2_server/tests/test_cover_metrics.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

pytest.importorskip("numpy")
pytest.importorskip("soundfile")

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 分析窗（与 `cover_metrics.frame_spectrum` 内部一致）—— 测试要能预测到 bin 上
N = 1024
SR = 48000
BIN = SR / N  # 46.875 Hz


def _load(name: str, rel: str):
    """按**路径**加载：`experiments/` 不是包，裸 import 找不到它。"""
    spec = importlib.util.spec_from_file_location(name, _PROJECT_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cm():
    return _load("cover_metrics", "experiments/cover_metrics.py")


@pytest.fixture(scope="module")
def ck():
    return _load("cover_knobs", "experiments/cover_knobs.py")


def _write(path: Path, y: np.ndarray, sr: int = SR) -> Path:
    sf.write(str(path), y.astype(np.float32), sr)
    return path


def _bin_hz(k: int) -> float:
    """第 k 根谱线对应的频率 —— 测试信号直接坐落在 bin 上，量出来的值才能预测。"""
    return k * BIN


def test_band_db_keeps_tiny_bands_readable(cm, tmp_path):
    """★ 核心用例：近乎为空的频段不能变成假的 -120 dB。

    构造：响的 200 Hz 单音 + 极轻的 9 kHz 单音（幅度比 3e-4 ⇒ 功率比 ~9e-8 ≈ -70 dB）。
    旧实现（先 round 到 5 位再取 log）会把它算成 -120 dB —— 那会让\"8  kHz 以上是空的\"
    这种结论**看起来**成立得多（-120 dB 像\"完全没有\"，-70 dB 只是\"很小\"）。
    """
    t = np.arange(SR) / SR
    k9000 = int(round(9000 / BIN))
    y = np.sin(2 * np.pi * _bin_hz(16) * t) + 3e-4 * np.sin(2 * np.pi * _bin_hz(k9000) * t)
    m = cm.frame_spectrum(_write(tmp_path / "tiny.wav", y / 1.001))
    got = m["band_db"]["8-11k"]
    assert got is not None, "8-11k 被判成\"无能量\"（band_db 里是 None）"
    # 幅度比 3e-4 ⇒ 功率比 9e-8 ⇒ 相对 0-4k 约 -70 dB；宽裕断言只排除\"被舍成 -120\"
    assert -85 < got < -60, f"8-11k 相对 dB 不合理：{got}（-120 就是精度被舍掉的假值）"


def test_band_db_is_relative_to_the_low_band(cm, tmp_path):
    """dB 的参照系：1 kHz 幅度 1.0 + 5 kHz 幅度 0.1 ⇒ 4-8k ≈ -20 dB。"""
    t = np.arange(SR) / SR
    y = np.sin(2 * np.pi * _bin_hz(21) * t) + 0.1 * np.sin(2 * np.pi * _bin_hz(110) * t)
    m = cm.frame_spectrum(_write(tmp_path / "ratio.wav", y / 1.001))
    assert m["band_db"]["4-8k"] == pytest.approx(-20.0, abs=1.5), m["band_db"]
    assert m["band_0-4k"] > m["band_4-8k"], "0-4k 应当仍是最强频段"


def test_silence_frames_are_ignored(cm, tmp_path):
    """静音段不得进入统计：空一半与全满的分析结果必须一致（1 dB 内）。

    这条守的是\"差异是被静音抹平的\"这个失效方式 —— 抹平之后**没有任何信号**会显示
    有问题，只会看到所有配置的数字都一样，然后得出\"旋钮都没用\"的错误结论。
    """
    t = np.arange(SR) / SR
    tone = np.sin(2 * np.pi * _bin_hz(21) * t) + 0.1 * np.sin(2 * np.pi * _bin_hz(110) * t)
    full = cm.frame_spectrum(_write(tmp_path / "full.wav", tone / 1.001))
    half = cm.frame_spectrum(_write(tmp_path / "half.wav", np.concatenate([tone / 1.001, np.zeros(SR)])))
    assert half["band_db"]["4-8k"] == pytest.approx(full["band_db"]["4-8k"], abs=1.0)
    assert half["voiced_frames"] < half["frames"], "静音帧应当被门限挡掉（否则这条用例没测到东西）"


def test_knob_configs_only_use_real_parameters(ck):
    """扫描表里的旋钮名必须是 `convert_audio` 真有的参数（防写错名字）。

    `cover_knobs` 运行时也会 assert 不剩未消费的 kwargs，但那要等模型加载完（~7s + GPU）；
    这条是静态的，能在提交那一刻就把拼错的名字拦住。
    """
    sys.path.insert(0, str(_PROJECT_ROOT / "m2_server"))
    import inspect

    import offline_vc_infer as ov

    params = set(inspect.signature(ov.convert_audio).parameters)
    # pitch / index_rate 由脚本**特意**先 pop 出来当位置参数处理
    special = {"pitch", "index_rate"}
    names = [name for name, _why, _kw in ck.CONFIGS]
    assert len(names) == len(set(names)), f"扫描表里有重名配置：{names}"
    for name, _why, kw in ck.CONFIGS:
        unknown = set(kw) - params - special
        assert not unknown, f"配置 {name} 用了不存在的旋钮：{unknown}"
    assert {"protect", "rms_mix_rate", "lowpass_hz"} <= {k for _n, _w, kw in ck.CONFIGS for k in kw}
