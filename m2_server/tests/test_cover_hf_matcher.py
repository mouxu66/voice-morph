"""匹配 EQ 的守卫（2026-10-20，m1u 续 · 带宽/音色解耦）。

这条 EQ 的作用只有一个：把"已经有内容的高频"抬到目标电平。三个地方一错就毁掉前提：

① **4k 以下必须不动**。那是音色本体，改了就不像用户了 —— 而本实验的全部意义就是保住音色。
② **增益不能无上限**。超过 +18dB 就不是"补"，是把底噪一起搬上来（A 臂就是这样翻车的）。
   满量程还要防削波。
③ **施加方式必须整段零相位**。逐帧 STFT 施加 EQ 会给每个 bin 独立相位，等于往高频加噪，
   正好毁掉"这层高频是内容"这个要验证的东西。

测试只用合成信号，不读 media/（不入库）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments"
SCRIPT = EXP / "cover_hf_matcher.py"
SR = 44100


def _mod():
    sys.path.insert(0, str(EXP))
    spec = importlib.util.spec_from_file_location("cover_hf_matcher_under_test", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cover_hf_matcher_under_test"] = m
    spec.loader.exec_module(m)
    return m


def _noise(secs: float = 4.0) -> np.ndarray:
    rng = np.random.default_rng(11)
    return (0.05 * rng.standard_normal(int(SR * secs))).astype(np.float64)


def _bands(y: np.ndarray) -> np.ndarray:
    """直接对整段做 FFT，比 band_levels 更干净，用来验证增益量。"""
    X = np.abs(np.fft.rfft(y)) ** 2
    f = np.fft.rfftfreq(len(y), 1.0 / SR)
    out = []
    for lo, hi in ((500.0, 2000.0), (8000.0, 12000.0)):
        out.append(10 * np.log10(X[(f >= lo) & (f < hi)].mean()))
    return np.array(out)


def test_eq_moves_band_by_the_requested_gain():
    """指定 +12dB 就必须真的抬 12dB（±1.5），且中频不动（±0.3）。"""
    m = _mod()
    y = _noise()
    af = np.array([500.0, 4000.0, 6000.0, 10000.0])
    ag = np.array([0.0, 0.0, 6.0, 12.0])
    before, after = _bands(y), _bands(m.apply_eq(y, SR, af, ag))
    assert abs((after[1] - before[1]) - 12.0) < 1.5, f"8–12k 只抬了 {after[1] - before[1]:.2f}dB"
    assert abs(after[0] - before[0]) < 0.3, f"中频被动了 {after[0] - before[0]:.2f}dB"


def test_low_band_anchor_is_always_zero():
    """增益曲线在 4k 以下必须回落到 0dB —— 否则音色被改，实验前提就没了。"""
    m = _mod()
    target = {"5000-6000": -46.0, "6000-8000": -49.7, "8000-12000": -52.5, "12000-16000": -53.8}
    cur = {"5000-6000": -67.8, "6000-8000": -62.6, "8000-12000": -68.1, "12000-16000": -62.2}
    af, ag = m.eq_curve(target, cur)
    assert af[0] == 500.0 and ag[0] == 0.0, "最低锚点必须是 0dB"
    assert ag[1] == 0.0, "4k 锚点必须是 0dB"
    assert np.all(np.diff(af) > 0), "锚点频率必须递增"


def test_gain_is_clipped_to_cap():
    """差 40dB 也只给 MAX_GAIN —— 无上限的 EQ 等于把底噪搬上来。"""
    m = _mod()
    target = {"5000-6000": -20.0, "6000-8000": -20.0, "8000-12000": -20.0, "12000-16000": -20.0}
    cur = dict.fromkeys(target, -60.0)
    _af, ag = m.eq_curve(target, cur)
    assert max(ag[2:]) <= m.MAX_GAIN + 1e-9, f"增益没夹住：{max(ag[2:]):.1f}dB"


def test_apply_eq_does_not_clip():
    """满量程输入也不能削波 —— 削波会造假的高频（方波边沿）。"""
    m = _mod()
    y = np.zeros(SR * 2)
    y[::100] = 0.95
    out = m.apply_eq(y, SR, *_noise_curve())
    assert np.abs(out).max() <= 1.0 + 1e-6, f"峰值 {np.abs(out).max():.3f} 越界"


def _noise_curve():
    return np.array([500.0, 4000.0, 6000.0, 10000.0]), np.array([0.0, 0.0, 8.0, 14.0])


def test_band_levels_is_loudness_invariant():
    """响度对齐之后，同一段乘 0.5 的频带电平必须一样。"""
    m = _mod()
    y = _noise(6.0) + 0.02 * np.sin(2 * np.pi * 220 * np.arange(SR * 6) / SR)
    a, b = m.band_levels(y, SR), m.band_levels(y * 0.5, SR)
    for k in a:
        assert abs(a[k] - b[k]) < 0.6, f"{k} 随音量变了"


def test_eq_curve_cap_is_honored():
    """cap_db 必须真的夹住增益。

    2026-10-20 实测：同一套 +18dB 逻辑搬到降调后的那一段，slope 从 0.38 跌到 0.29
    （正好过「噪声底」阈值）—— 上限要跟着素材的余量走，所以它得是参数，
    并且默认值必须保持 MAX_GAIN，不让旧结论（+18dB）在无提示的情况下被改掉。
    """
    m = _mod()
    target = {k: -30.0 for k in ("5000-6000", "6000-8000", "8000-12000", "12000-16000")}
    current = {k: -60.0 for k in target}      # 每条都差 30dB
    _f, g_default = m.eq_curve(target, current)
    assert max(g_default[2:]) == m.MAX_GAIN == 18.0
    _f, g_cap = m.eq_curve(target, current, cap_db=12.0)
    assert max(g_cap[2:]) == 12.0
    assert all(v >= 0.0 for v in g_cap[2:])   # 不往下减：不把已有内容压掉


def test_no_module_level_audio_io():
    """模块级不许读音频 —— import 只应该拿到常量。"""
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
