"""素材带宽体检的守卫（2026-10-20）。

为什么值得锁住：`voice_dataset_check.py` 的判定直接决定「这批素材要不要重录」。
判错的代价是一轮完整的模型训练（几十分钟 + 一次主观失望）。
真正的坑在口径：
  · 必须**相对 0.2–3k 归一化** —— 录音音量不同，绝对 dB 会把「录得小声」误判成「没高频」
  · 必须**只统计有声帧** —— 静音帧的高频是噪声底，混进来会把合格素材判成不合格
这两条都是 2026-10-20 实测踩过的（同一批素材，松门限算出 8–12k −46dB「边缘」，
紧门限算出 −50.9「不合格」，差 5dB）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "voice_dataset_check.py"


def _mod():
    spec = importlib.util.spec_from_file_location("vdc_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vdc_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_verdict_thresholds():
    m = _mod()
    assert m.verdict(-40.0) == "合格"
    assert m.verdict(-50.0) == "合格", "合格线本身算合格（>= 而非 >）"
    assert m.verdict(-54.0) == "边缘"
    assert m.verdict(-58.0) == "边缘"
    assert m.verdict(-60.2) == "不合格", "用户现有素材实测值"
    assert m.verdict(float("nan")) == "无数据"


def test_profile_normalizes_to_low_band(tmp_path):
    """音量变 4 倍不该改变谱形 —— 判定必须只看形状，不看响度。

    ★ 必须用**宽带**信号：纯正弦在空频带只剩数值噪声（−86 vs −99dB），
    那两个数不可比，测出来的差异是浮点误差而不是归一化。
    """
    import soundfile as sf
    from scipy.signal import butter, sosfilt

    sr = 40000
    rng = np.random.default_rng(42)
    white = rng.normal(0.0, 0.05, sr * 3)
    # 2kHz 低通：模拟「只有低频、高频是空」的素材
    y = sosfilt(butter(6, 2000.0, fs=sr, btype="lowpass", output="sos"), white)
    y = (y / np.abs(y).max() * 0.3).astype(np.float32)

    m = _mod()
    quiet = tmp_path / "quiet.wav"
    loud = tmp_path / "loud.wav"
    sf.write(str(quiet), y, sr)
    sf.write(str(loud), (y * 4).astype(np.float32), sr)

    pq, _, nq = m.band_profile(quiet)
    pl, _, nl = m.band_profile(loud)
    assert pq and pl, "带限信号应该有有声帧"
    assert pq[(8000, 12000)] < -50, "2kHz 低通的素材，8–12k 必须判为空"
    for band in [(200, 1000), (1000, 2000), (2000, 3000)]:
        assert abs(pq[band] - pl[band]) < 1.0, f"{band} 音量变 4 倍不该改变归一化后的谱形"


def test_silence_is_not_counted_as_voiced(tmp_path):
    """纯静音文件不能产出「合格」——它是无数据，不是好消息。"""
    import soundfile as sf

    m = _mod()
    p = tmp_path / "silence.wav"
    sf.write(str(p), np.zeros(40000 * 2, dtype=np.float32), 40000)
    prof, _sr, nv = m.band_profile(p)
    assert prof == {} and nv == 0


def test_key_band_is_8_12k():
    """判定频带必须是 8–12k：4–5k 在用户素材上是 −34.6（看着还行），
    那里判定会把「5k 断崖 + 8k 以上全空」的素材放行。"""
    m = _mod()
    assert m.KEY_BAND == (8000, 12000)


@pytest.mark.parametrize("band", [(4000, 5000), (5000, 6000), (8000, 12000)])
def test_bands_are_contiguous_and_ordered(band):
    m = _mod()
    assert band in m.BANDS
    assert m.BANDS == sorted(m.BANDS), "频带必须升序，否则表格列会对不上标签"
