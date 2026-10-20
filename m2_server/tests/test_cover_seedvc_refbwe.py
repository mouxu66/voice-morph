"""参照音 BWE 的守卫（2026-10-20，m1u 续）。

这条链路要把用户的 22.05k 参照音变成 44.1k 全带宽参照。两个地方一错，实验结论就假：

① **BWE 模型只吃 8/12/16/24k**。直接喂 22.05k 会被它内部重采样，带宽跟声明不符。
   必须先显式降到 16k，而这个降采样**丢的是 8k 以上** —— 恰好是那个文件本来就空掉的频段
   （实测 −95.9dB），所以不损失信息。这个前提写死在常量里，不许随手改成 24000。
② **拼接臂不能改采样率**。两段参照采样率不同，直接 concat 会变成变调。

测试只用合成的短音频，不读 media/（不入库），也不跑 UniverSR。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

# librosa/soxr 首次加载会触发 importlib.resources 的 DeprecationWarning，
# 而本仓 pytest 开了 -W error。那是第三方库的噪音，和被测逻辑无关，只在本模块放行。
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments"
SCRIPT = EXP / "cover_seedvc_refbwe.py"


def _mod():
    sys.path.insert(0, str(EXP))
    spec = importlib.util.spec_from_file_location("cover_seedvc_refbwe_under_test", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cover_seedvc_refbwe_under_test"] = m
    spec.loader.exec_module(m)
    return m


def test_bwe_input_rate_is_a_supported_tier():
    """BWE 的输入档位必须落在模型支持的集合里，且必须是 16k（8k 以上本来就空）。"""
    m = _mod()
    assert m.BWE_IN_SR == 16000, f"BWE_IN_SR 被改成 {m.BWE_IN_SR}，与实测口径不符"


def test_resample_changes_rate_but_keeps_duration():
    m = _mod()
    t = np.arange(22050 * 2) / 22050
    y16 = m._resample(0.1 * np.sin(2 * np.pi * 200 * t), 22050, 16000)
    assert abs(len(y16) / 16000 - 2.0) < 0.01, f"时长变了：{len(y16) / 16000:.3f}s"


def test_concat_resamples_both_sides():
    """两段不同采样率拼接后必须是单一时基 —— 否则音高会错。"""
    m = _mod()
    with tempfile.TemporaryDirectory() as td:
        a, b, out = Path(td) / "a.wav", Path(td) / "b.wav", Path(td) / "c.wav"
        sf.write(str(a), np.zeros(22050, dtype=np.float32), 22050)
        sf.write(str(b), np.zeros(44100, dtype=np.float32), 44100)
        m.ref_concat(a, b, out)
        y, sr = sf.read(str(out))
        assert sr == 44100, f"拼接后采样率是 {sr}"
        assert abs(len(y) - (44100 + 44100)) < 100, f"拼接后长度 {len(y)}"


def test_no_module_level_audio_io():
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if isinstance(call, ast.Constant) and isinstance(call.value, str):
            continue
        assert isinstance(call, ast.Call), "模块级出现非调用表达式"
        assert ast.unparse(call.func) == "sys.path.insert", f"模块级出现 {ast.unparse(call.func)}"
