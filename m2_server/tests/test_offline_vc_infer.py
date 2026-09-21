"""offline_vc_infer.postprocess_audio 回归测试。

历史坑：旧实现结尾 np.clip(y, -0.99, 0.99) 硬削波，RVC 瞬态 + IIR 低通过冲
后 ~0.17% 样本被削平，听感刺耳（NatScore 掉到负值区）。重构为「只衰减」的
峰值归一（-1.5 dBFS 目标）后，用例钉死：不削波、响度锚点保持、静音安全。
"""

import os

import numpy as np
import pytest

pytest.importorskip("numpy")
pytest.importorskip("scipy")

import offline_vc_infer  # noqa: E402

TARGET = 10 ** (-1.5 / 20)


def _speech_like(
    n: int = 48000,
    sr: int = 48000,
    sine_amp: float = 0.3,
    n_impulse: int = 20,
    impulse_amp: float = 6.0,
) -> np.ndarray:
    """正弦 + 稀疏大幅瞬态：高波峰因子，模拟 RVC 输出的过冲尖峰。
    瞬态只放后半段，保证前半段是纯正弦（供波形形状断言用）。"""
    t = np.arange(n) / sr
    y = sine_amp * np.sin(2 * np.pi * 220 * t)
    rng = np.random.default_rng(7)
    idx = rng.integers(n // 2, n - 100, size=n_impulse)
    y[idx] += impulse_amp
    return y.astype(np.float32)


def test_loud_input_no_hard_clip_peak_at_target():
    y = _speech_like()
    out = offline_vc_infer.postprocess_audio(y, 48000)
    assert out.dtype == np.float32
    peak = float(np.abs(out).max())
    assert peak == pytest.approx(TARGET, rel=1e-3)  # 峰值衰减到 -1.5 dBFS
    assert peak < 0.99  # 不再顶硬限幅沿
    # 无平顶削波：贴着目标的样本占比必须远低于旧 clip 实现
    assert (np.abs(out) >= TARGET * 0.999).mean() < 0.005


def test_no_flat_top_clipping():
    """等比缩放不产生平顶：贴峰的长连续段是硬削波特征，等比缩放没有。"""
    y = _speech_like()
    out = offline_vc_infer.postprocess_audio(y, 48000)
    peak = float(np.abs(out).max())
    at_peak = np.abs(np.abs(out) - peak) < 1e-4 * peak
    idx = np.where(at_peak)[0]
    assert len(idx) > 0
    segments = np.split(idx, np.where(np.diff(idx) > 1)[0] + 1)
    max_run = max(len(s) for s in segments)
    assert max_run <= 3  # 硬削波会形成远超 3 个样本的连续平顶


def test_normal_signal_rms_anchor():
    """峰值安全的正常信号：RMS 应锚定在 -18 dBFS，不被再放大。"""
    t = np.arange(48000) / 48000
    y = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    out = offline_vc_infer.postprocess_audio(y, 48000)
    rms = float(np.sqrt((out**2).mean()))
    assert rms == pytest.approx(10 ** (-18 / 20), rel=0.05)
    assert float(np.abs(out).max()) < TARGET  # 未触发峰值衰减


def test_low_sr_filter_guard():
    """22.05k 模型输出：低通截止自动让出 Nyquist（min(11k, 0.45·sr)），不报错。"""
    y = _speech_like(n=22050, sr=22050)
    out = offline_vc_infer.postprocess_audio(y, 22050)
    assert np.isfinite(out).all()
    assert float(np.abs(out).max()) <= TARGET + 1e-6


def test_silence_and_dc_safe():
    out = offline_vc_infer.postprocess_audio(np.zeros(4800, dtype=np.float32), 48000)
    assert out.shape == (4800,) and np.isfinite(out).all() and float(np.abs(out).max()) == 0.0
    out2 = offline_vc_infer.postprocess_audio(np.full(4800, 0.5, dtype=np.float32), 48000)
    assert np.isfinite(out2).all() and float(np.abs(out2).max()) <= TARGET + 1e-6


# ============== 权重加载失败的判读（2026-09-21 补） ==============
#
# 由来：本机 `auto_rb` 音色报「PyTorch 2.6 weights_only」，于是被记成「PyTorch 版本问题」。
# 实测真因是**文件本身不是权重** —— 它正好是 test_market_search_install.py 的夹具
# `b"\x80\x02" + os.urandom(512*1024-2)`（512KiB 随机数据 + 两个假魔数字节）。
#
# 这个坑值钱的地方在于**torch 的报错主动指错方向**：它写着
# "Re-running torch.load with weights_only set to False will likely succeed" ——
# 而对这个文件并不会 succeed，照它做只会白改一遍代码、还把第三方 ckpt 的
# `__reduce__` 载荷风险重新引进来（音色市场的权重来自公网，正是威胁模型）。
#
# 所以下面两组用例钉两件事：
#   1. `diagnose_pth` 能**只读结构**说清「这文件为什么不可用」（不反序列化，对不可信文件安全）；
#   2. `load_checkpoint` 的报错必须**明确否掉**「改用 weights_only=False」这个错误修法，
#      并且必须带上结构判读 —— 否则下一个人还会照 torch 的文案去改。
#
# 本文件在 check.py 的 FAST_TESTS 里，所以这里**不 import torch**
# （实测 2.5s，会挤掉 pre-commit 预算）：改用桩 torch 只测错误路径与形状校验。
# 真实 torch + 真实权重的守护在 test_offline_vc_infer_pth_guard.py（全量跑）。


class _FakeTorchRaises:
    """桩 torch：`load` 抛指定的异常，用于测错误路径而不装真 torch。"""

    def __init__(self, exc: Exception):
        self._exc = exc

    def load(self, *_a, **_kw):
        raise self._exc


class _FakeTorchReturns:
    def __init__(self, value):
        self._value = value

    def load(self, *_a, **_kw):
        return self._value


def test_diagnose_rejects_random_bytes_with_fake_magic(tmp_path):
    """★ 核心用例：`\x80\x02` + 随机数据（就是那个夹具的形状）必须被判为「不是权重文件」。

    只查前两字节的魔数校验会放它过去（`market_download._torch_header_ok` 就是那么做的，
    且那是**刻意的**廉价校验）—— 所以判读必须真的走一遍 pickle 结构。
    """
    p = tmp_path / "junk.pth"
    p.write_bytes(b"\x80\x02" + os.urandom(512 * 1024 - 2))
    msg = offline_vc_infer.diagnose_pth(str(p))
    assert "pickle 流在**早期就非法**" in msg, f"没识破假魔数文件：{msg}"
    assert "524,288 字节" in msg, f"应报出文件大小（便于判断是否下载不完整）：{msg}"


def test_diagnose_rejects_html(tmp_path):
    """下到错误页面（404 HTML）是最常见的「不是权重」形态。"""
    p = tmp_path / "page.pth"
    p.write_text("<!DOCTYPE html><html><body>404</body></html>", encoding="utf-8")
    msg = offline_vc_infer.diagnose_pth(str(p))
    assert "既不是 zip(PK) 也不是 pickle" in msg, msg
    assert "错误页面" in msg, msg


def test_diagnose_reports_broken_zip(tmp_path):
    """PK 开头但 zip 已损坏 —— 典型的下半截文件。"""
    p = tmp_path / "half.pth"
    p.write_bytes(b"PK\x03\x04" + os.urandom(200))
    msg = offline_vc_infer.diagnose_pth(str(p))
    assert "zip 容器已损坏" in msg, msg


def test_diagnose_reports_zip_without_data_pkl(tmp_path):
    """zip 能开但不含 data.pkl → 不是 torch 存档。"""
    import zipfile

    p = tmp_path / "nope.pth"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("readme.txt", "hello")
    msg = offline_vc_infer.diagnose_pth(str(p))
    assert "没有 data.pkl" in msg, msg


def test_diagnose_accepts_genuine_zip_shape(tmp_path):
    """像样的 zip 存档（含 data.pkl）不该被报成「不是权重文件」。"""
    import zipfile

    p = tmp_path / "ok.pth"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("archive/data.pkl", b"\x80\x04N.")
    msg = offline_vc_infer.diagnose_pth(str(p))
    assert "含 data.pkl" in msg, msg
    assert "不是权重文件" not in msg, f"不该否定一个形状正确的存档：{msg}"


def test_diagnose_handles_missing_file(tmp_path):
    """路径不存在要给人话，而不是抛 FileNotFoundError 糊到调用方脸上。"""
    msg = offline_vc_infer.diagnose_pth(str(tmp_path / "gone.pth"))
    assert "读不到" in msg, msg


def test_load_checkpoint_error_denies_the_weights_only_fix(tmp_path):
    """★ 报错必须**明确否掉** torch 文案建议的那个修法。

    这是本组用例的真正目的：错误信息如果不点破，下一个 agent 看到
    "Re-running with weights_only=False will likely succeed" 就会去关掉保护。
    """
    p = tmp_path / "junk.pth"
    p.write_bytes(b"\x80\x02" + os.urandom(4096 - 2))
    torch = _FakeTorchRaises(
        RuntimeError(
            "Weights only load failed. In PyTorch 2.6, we changed the default value of the "
            "`weights_only` argument in `torch.load` from `False` to `True`. Re-running "
            "`torch.load` with `weights_only` set to `False` will likely succeed."
        )
    )
    with pytest.raises(RuntimeError) as ei:
        offline_vc_infer.load_checkpoint(str(p), torch)
    msg = str(ei.value)
    assert "不是 PyTorch 版本问题" in msg, f"没点破误诊方向：{msg}"
    assert "weights_only=False" in msg, f"没警告那个错误修法：{msg}"
    assert "结构判读" in msg, f"没带结构判读，下一个人仍无从判断：{msg}"
    assert "pickle 流在**早期就非法**" in msg, f"判读结论没进来：{msg}"


def test_load_checkpoint_rejects_wrong_shape(tmp_path):
    """能反序列化但不是 RVC ckpt（缺 weight/config）也要拦，且说清实际拿到了什么。"""
    p = tmp_path / "notrvc.pth"
    p.write_bytes(b"\x80\x04N.")
    torch = _FakeTorchReturns({"model": 1, "iteration": 2})  # 这是预训练底模的形状
    with pytest.raises(RuntimeError) as ei:
        offline_vc_infer.load_checkpoint(str(p), torch)
    msg = str(ei.value)
    assert "格式不对" in msg, msg
    assert "weight/config" in msg, msg
    assert "['model', 'iteration']" in msg, f"应报出实际键（好判断这是哪种文件）：{msg}"


def test_load_checkpoint_accepts_genuine_shape(tmp_path):
    """正常形状要通过，且**原样返回**（不能顺手改内容）。"""
    p = tmp_path / "ok.pth"
    p.write_bytes(b"\x80\x04N.")
    payload = {"weight": {"a": 1}, "config": [1, 2, 3], "f0": 1, "version": "v2"}
    torch = _FakeTorchReturns(payload)
    assert offline_vc_infer.load_checkpoint(str(p), torch) is payload
