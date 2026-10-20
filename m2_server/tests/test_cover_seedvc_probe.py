"""Seed-VC 对照实验的守卫（2026-10-20，m1u）。

这个脚本要回答的是"换架构能不能不重录就补回高频"，所以**度量口径必须能真的看见高频**，
而且必须能在增益差几十 dB 的两个文件之间比。四个容易悄悄坏掉的地方：

① **用绝对能量分档不行**：m1t 的 −40/−20/−8 dBFS 档在整曲上成立，搬到 20 秒片段就崩——
   Seed-VC 输出接近满刻度、RVC 干声小得多，同一个"silent 档"装的不是同一类内容
   （实测 RVC 有 71% 的帧被判成 silent，而它明明在唱）。必须按**分位数**取有声帧 + 响度对齐。
② **按 bin 下标取频带不行**：三个文件采样率不同（44.1k / 40k / 22.05k），
   同一"频带"必须是同一段 Hz。
③ **切片点必须落在文件内**：改源之后静默切出空音频，度量会退化成噪声底。
④ **模块级不能有 I/O**：import 这个模块只应该拿到常量。

测试只做静态/数值断言，不读 media/ 下的产物（那目录不入库），也不加载 Seed-VC 权重。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments"
PROBE = EXP / "cover_seedvc_probe.py"


def _mod():
    sys.path.insert(0, str(EXP))
    spec = importlib.util.spec_from_file_location("cover_seedvc_probe_under_test", PROBE)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cover_seedvc_probe_under_test"] = m
    spec.loader.exec_module(m)
    return m


def _tone(sr: int = 40000, secs: float = 6.0, hf: bool = True) -> np.ndarray:
    """基频 180Hz 的谐波堆；hf=False 时只保留 4k 以下。"""
    rng = np.random.default_rng(7)
    t = np.arange(int(sr * secs)) / sr
    y = sum(np.sin(2 * np.pi * 180 * k * t) / k for k in range(1, 80))
    y = 0.2 * y / max(np.abs(y).max(), 1e-9)
    y = y + 0.01 * rng.standard_normal(y.size)
    if not hf:
        from scipy.signal import butter, sosfilt

        y = sosfilt(butter(8, 4000 / (sr / 2), btype="low", output="sos"), y)
    return y.astype(np.float64)


def test_analyze_is_loudness_invariant():
    """同一段音频乘 0.5，对齐后的频带电平必须不变 —— 这就是"响度对齐"的意义。

    第一版没对齐，直接把 dB 绝对值拿去比，结果 Seed-VC（接近满刻度）和
    RVC 干声（小得多）根本不在同一条尺子上。
    """
    m = _mod()
    y = _tone()
    a = m.analyze(y, 40000)
    b = m.analyze(y * 0.5, 40000)
    for k in a["bands"]:
        assert abs(a["bands"][k] - b["bands"][k]) < 0.6, \
            f"{k} 随音量变了 {a['bands'][k]} → {b['bands'][k]}"


def test_analyze_sees_missing_high_band():
    """4k 以上被砍掉的信号，8–12k 必须显著低于宽带信号（差 > 30dB）。"""
    m = _mod()
    wide = m.analyze(_tone(hf=True), 40000)
    band = m.analyze(_tone(hf=False), 40000)
    d = wide["bands"]["8000-12000"] - band["bands"]["8000-12000"]
    assert d > 30, f"度量看不见高频：宽带 {wide['bands']['8000-12000']} vs 带限 {band['bands']['8000-12000']}"


def test_active_frames_use_percentile_not_absolute_threshold():
    """有声帧必须按分位数取，不能用 m1t 的绝对 dB 档（那套只在整曲成立）。"""
    src = PROBE.read_text(encoding="utf-8")
    assert "np.percentile(elo, 50)" in src, "有声帧要按低频能量的分位数取"
    assert "ALIGN_DB" in src, "必须先做响度对齐再比频带"


def test_band_edges_are_absolute_hz_not_bin_index():
    """采样率不同时，同一标签必须对应同一段频率。"""
    src = PROBE.read_text(encoding="utf-8")
    assert "np.fft.rfftfreq(n, 1.0 / sr)" in src, "频带必须按真实频率取，不能按 bin 下标"
    assert "for a, b in BANDS" in src, "BANDS 必须以 Hz 对出现"


def test_cut_refuses_out_of_range():
    """切片越界必须报错而不是静默产出空音频 —— 那会让所有度量退化成噪声底。"""
    import tempfile

    import soundfile as sf

    m = _mod()
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "short.wav"
        sf.write(str(p), np.zeros(4000, dtype=np.float32), 40000)
        try:
            m.cut(p, Path(td) / "x.wav", 5.0, 2.0)
        except SystemExit as e:
            assert "越界" in str(e)
        else:
            raise AssertionError("越界切片应该直接退出")


def test_probe_does_no_module_level_audio_io():
    """模块级不许读音频/加载模型 —— import 这个模块只应该拿到常量。

    用 AST 看模块体：只放行 sys.path.insert（方便 import 同目录脚本）。
    """
    import ast

    tree = ast.parse(PROBE.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if isinstance(call, ast.Constant) and isinstance(call.value, str):
            continue  # 模块文档字符串
        assert isinstance(call, ast.Call), "模块级出现非调用表达式"
        name = ast.unparse(call.func)
        assert name == "sys.path.insert", f"模块级出现 {name}（import 时就执行，会让工具变脆）"
