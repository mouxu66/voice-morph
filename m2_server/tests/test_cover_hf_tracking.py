"""8–12k「跟随嗓音 vs 静态嘶声」度量的守卫（2026-10-20，m1u 续）。

这条尺子要判的是「Seed-VC 补出来的高频是不是一层不动的嘶」。两个方向都容易坏：

① **静态嘶声必须被认出来，而不是崩掉**。最典型的坏样本就是电平纹丝不动的高频，
   第一版写的是 `a_hf.std() < 1e-6 → 直接退出`，等于"遇到最该报的样本就罢工"。
② **必须对响度不敏感**。跨文件比绝对电平要先把 200–3000 拉到同一档，
   否则 RVC 干声（小得多）和 Seed-VC 输出（接近满刻度）根本不在一条尺子上。

★ 还有一条走过的弯路，留在这里当护栏：**不要回到"按 f0 铺梳状窗算谐波性"**。
合成验证过——纯谐波 320Hz 的 8–12k excess HNR 是 +28.3dB，同一条信号加 3% 颤音
就掉到 +0.4dB。8–12k 是第 30 根谐波，f0 抖 3% 就是 290Hz 位移，而 93ms 窗只有
10.8Hz 分辨率，颤音必然把谐波抹平。真人唱歌全有颤音，所以那把尺子量的是颤音，
不是嘶声。这里因此**完全不估 f0**。

测试只用合成信号，不读 media/（那目录不入库）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments"
SCRIPT = EXP / "cover_hf_tracking.py"
SR = 44100


def _mod():
    sys.path.insert(0, str(EXP))
    spec = importlib.util.spec_from_file_location("cover_hf_tracking_under_test", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["cover_hf_tracking_under_test"] = m
    spec.loader.exec_module(m)
    return m


def _voice(hf: str = "track", secs: float = 6.0) -> np.ndarray:
    """220Hz 谐波堆 + 8–12k 成分。

    hf="track"  → 高频乘同一条包络（嗓音驱动）
    hf="static" → 高频恒定电平（静态嘶声）
    """
    t = np.arange(int(SR * secs)) / SR
    env = 0.02 + 0.98 * (0.5 + 0.5 * np.sin(2 * np.pi * 0.8 * t)) ** 2
    low = sum(np.sin(2 * np.pi * 220 * k * t) / k for k in range(1, 14))
    hi = sum(np.sin(2 * np.pi * 220 * k * t + 0.4) / k for k in range(38, 55))
    hi = hi / max(np.abs(hi).max(), 1e-9)
    y = 0.3 * low / max(np.abs(low).max(), 1e-9) * env + 0.05 * (hi * env if hf == "track" else hi)
    return y.astype(np.float64)


def test_hf_following_the_singer_is_usable():
    """高频跟着包络走：斜率≈1，气口掉得下去，判"可用"。"""
    m = _mod()
    r = m.tracking(_voice("track"), SR)
    assert r["slope"] > 0.8, f"跟随信号斜率只有 {r['slope']}"
    assert r["r_env"] > 0.9, f"跟随信号相关只有 {r['r_env']}"
    assert r["gap_drop_db"] < -15, f"气口只掉了 {r['gap_drop_db']}dB"
    assert "可用" in m.verdict(r, r["hf_active_db"])


def test_static_hiss_is_flagged_not_crashed():
    """静态嘶声：电平不动。必须判成"噪声底"，而且**不能抛异常**。

    第一版在这里 `raise SystemExit`，等于最该报的样本反而罢工。
    """
    m = _mod()
    r = m.tracking(_voice("static"), SR)
    assert r["slope"] < 0.2, f"静态嘶声被判成斜率 {r['slope']}"
    assert r["gap_drop_db"] > -3, f"静态嘶声的气口掉了 {r['gap_drop_db']}dB，不该掉"
    assert "噪声底" in m.verdict(r, r["hf_active_db"])


def test_tracking_is_loudness_invariant():
    """整段乘 0.5，三个指标必须不变 —— 内部先对齐到同一档再算。"""
    m = _mod()
    y = _voice("track")
    a, b = m.tracking(y, SR), m.tracking(y * 0.5, SR)
    for k in ("r_env", "slope", "gap_drop_db", "hf_active_db"):
        assert abs(a[k] - b[k]) < 0.05, f"{k} 随音量变了：{a[k]} → {b[k]}"


def test_bands_are_absolute_hz():
    """频带必须是 Hz 常量、按真实频率取 bin —— 不同采样率的文件混着比才成立。"""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "np.fft.rfftfreq(n, 1.0 / sr)" in src, "频带必须按真实频率取，不能按 bin 下标"
    assert "LOW = (200.0, 3000.0)" in src and "HF = (8000.0, 12000.0)" in src


def test_gap_uses_quantile_not_true_silence():
    """切片里不一定有真静音，"气口"必须用分位数取，否则永远算不出来。"""
    src = SCRIPT.read_text(encoding="utf-8")
    assert "np.percentile(lo, 10)" in src, "气口要按低频能量的分位数取"


def test_slice_refuses_out_of_range():
    """越界切片必须报错，而不是静默产出空音频。"""
    m = _mod()
    try:
        m.slice_of(np.zeros(SR * 3, dtype=np.float32), SR)
    except SystemExit as e:
        assert "越界" in str(e)
    else:
        raise AssertionError("越界切片应该直接退出")


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
        assert ast.unparse(call.func) == "sys.path.insert", (
            f"模块级出现 {ast.unparse(call.func)}（import 时就执行）"
        )
