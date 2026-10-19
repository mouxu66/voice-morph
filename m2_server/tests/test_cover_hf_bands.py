# -*- coding: utf-8 -*-
"""`experiments/cover_hf_bands.py` 的纯函数守卫（不需要 GPU / ffmpeg / librosa）。

这组测试守的是**归因能不能成立**，不是"脚本能跑"：
1. 三臂必须**只差 8–12k 的来源** —— 亮度目标与门控若被写歪，
   B/C 的差别就会同时包含"更亮"和"换了成分"，结论全部作废；
2. 注入必须**零相位**（带相位的滤镜会把 8–12k 拖后几毫秒，
   听感是"沙沙尾音"，而只看能量的指标却完全正常）；
3. 谱折叠必须落在 8–12k 的**镜像**位置，不能越界污染 4–8k。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
_EXP = _ROOT / "experiments"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _EXP / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


hf = _load("cover_hf_bands_under_test", "cover_hf_bands.py")

SR = 40000


def _tone(seconds=1.0, freq=1000.0, amp=0.3, sr=SR):
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _noise(seconds=1.0, amp=0.1, sr=SR, seed=0):
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal(int(seconds * sr))).astype(np.float32)


# ---------------------------------------------------------------- 带通 / 门控

def test_bandpass_keeps_in_band_and_kills_out_of_band():
    inb = _tone(freq=10000.0, amp=0.5)
    out = _tone(freq=1000.0, amp=0.5)
    y = hf.bandpass(inb + out, SR, 8000, 12000)
    # 带内保留、带外压掉 ≥30dB
    assert hf.rms(y) > 0.3
    assert hf.rms(y) < hf.rms(inb) * 1.05


def test_bandpass_is_zero_phase():
    """相位偏移 > 1ms 就算不合格（20ms 帧的 5%）。"""
    x = _tone(freq=10000.0, amp=0.5)
    y = hf.bandpass(x, SR, 8000, 12000)
    # 互相关峰的位置就是群延迟
    c = np.correlate(y[5000:-5000], x[5000:-5000], "full")
    lag = int(np.argmax(np.abs(c))) - (len(x) - 10000 - 1)
    assert abs(lag) <= int(0.001 * SR), f"群延迟 {lag} 样本（>1ms），不是零相位"


def test_gate_closes_low_envelope_and_passes_loud():
    x = np.concatenate([_tone(seconds=0.5, amp=0.5), np.zeros(SR // 2, dtype=np.float32),
                        _noise(seconds=0.5, amp=1e-4, seed=1)])
    g = hf.gate_gain(x, SR)
    assert g[: int(0.45 * SR)].mean() > 0.9                 # 有声段：开
    assert g[int(0.55 * SR) : int(0.95 * SR)].mean() < 0.05  # 静音段：关
    assert g[int(1.05 * SR) :].mean() < 0.05                 # 只有底噪段：也关
    assert g.min() >= 0.0 and g.max() <= 1.0


def test_gate_gain_length_matches_input():
    x = _tone(seconds=0.3)
    assert len(hf.gate_gain(x, SR)) == len(x)


# ---------------------------------------------------------------- 目标亮度

def test_target_ratio_reads_source_own_hf_over_mid():
    # 8–12k 比 4–8k 低 20dB → ratio ≈ 0.1
    x = _tone(freq=5000.0, amp=0.3) + _tone(freq=10000.0, amp=0.03)
    r = hf.hf_target_ratio(x, SR)
    assert 0.06 < r < 0.16


# ---------------------------------------------------------------- B 臂：源高频移植

def test_transplant_hits_target_brightness():
    """注入后的 8–12k/4–8k 必须≈源的比值（±3dB）—— 这就是"按 4–8k 电平匹配"。"""
    dry = _tone(freq=5000.0, amp=0.4) + _tone(freq=10000.0, amp=0.004)
    src = _tone(freq=5000.0, amp=0.4) + _tone(freq=10000.0, amp=0.04)
    ratio = hf.hf_target_ratio(src, SR)
    out, meta = hf.hf_transplant(dry, src, SR, ratio=ratio)
    got = hf.rms(hf.bandpass(out, SR, 8000, 12000)) / hf.rms(hf.bandpass(out, SR, 4000, 8000))
    assert abs(20 * np.log10(got / ratio)) < 3.0, (got, ratio, meta)


def test_transplant_does_not_touch_silence():
    """输出静音处不许被源高频填成"沙沙的"（门控必须生效）。"""
    dry = np.concatenate([_tone(seconds=0.5, amp=0.4), np.zeros(SR // 2, dtype=np.float32)])
    src = np.concatenate([_tone(seconds=0.5, freq=10000.0, amp=0.3),
                          _tone(seconds=0.5, freq=10000.0, amp=0.3)])
    out, _ = hf.hf_transplant(dry, src, SR, ratio=0.1)
    tail = out[int(0.6 * SR):]
    assert hf.rms(tail) < 0.01, f"静音段被填了 {hf.rms(tail):.4f}"


def test_transplant_requires_source_hf_else_noop():
    """源没有 8–12k（或只有 −80dB 的底噪）时几乎不注入 —— 防止"凭空造亮度"。"""
    dry = _tone(freq=5000.0, amp=0.4)
    src = _tone(freq=5000.0, amp=0.4)
    out, meta = hf.hf_transplant(dry, src, SR, ratio=0.0)
    assert meta["inject_rms"] < 1e-6
    assert np.allclose(out, dry, atol=1e-6)


def test_transplant_length_mismatch_is_trimmed_not_crashed():
    dry = _tone(seconds=1.0)
    src = _tone(seconds=0.6, freq=10000.0)
    out, meta = hf.hf_transplant(dry, src, SR)
    assert len(out) == len(src)          # 只保留可比的那一段
    assert meta["trimmed_samples"] == len(dry) - len(src)


# ---------------------------------------------------------------- C 臂：谱折叠

def test_fold_puts_energy_only_in_hf_band():
    dry = _tone(freq=6000.0, amp=0.4)
    out, meta = hf.spectral_fold(dry, SR, ratio=0.2)
    assert hf.rms(hf.bandpass(out, SR, 8000, 12000)) > 1e-3
    # 4–8k 不许被折回去的那一份污染（镜像轴 16k：4–8k → 8–12k，不落回自身）
    mid_before = hf.rms(hf.bandpass(dry, SR, 4000, 8000))
    mid_after = hf.rms(hf.bandpass(out, SR, 4000, 8000))
    assert abs(20 * np.log10(mid_after / mid_before)) < 1.0
    assert meta["peak_gain"] <= 1.0


def test_fold_source_bin_mapping_is_mirror():
    """6k 的内容应该出现在 10k（16k−6k），而不是掉回低频端。"""
    dry = _tone(freq=6000.0, amp=0.4)
    out, _ = hf.spectral_fold(dry, SR, ratio=1.0)
    hi = hf.rms(hf.bandpass(out, SR, 9500, 10500))
    lo = hf.rms(hf.bandpass(out, SR, 500, 1500)) + 1e-12
    assert hi / lo > 10, (hi, lo)
    # 干声自己 10k 处本来是空的（只有泄漏）
    assert hi > hf.rms(hf.bandpass(dry, SR, 9500, 10500)) * 3


# ---------------------------------------------------------------- 指标

def test_slope_is_flat_for_white_noise_and_rises_with_hf():
    """★ 带宽公平性：白噪声的斜率必须是 0 附近（±1dB/oct）。

    这是 2026-10-19 实测踩过的坑：拿 1–2k（1 个倍频）与 8–12k（约 0.58 个倍频）直接相减，
    白噪声会被读成 +2.15 dB/oct 的"斜坡"—— 于是"模型把高频练亮了"这个结论
    可以凭空从带宽差里长出来。
    """
    flat = _noise(seconds=1.0, amp=0.1)
    bright = flat + _tone(freq=8000.0, amp=0.05)
    s_flat = hf.rolloff_slope_db_per_oct(flat, SR)
    assert abs(s_flat) < 1.0, s_flat
    assert hf.rolloff_slope_db_per_oct(bright, SR) > s_flat + 1.0


def test_modulation_cv_higher_for_gated_than_steady_hf():
    """门控必须落在**帧尺度**（50ms 开关）上才该抬高 CV —— 逐样本抽点不改变帧 RMS。"""
    steady = _tone(freq=10000.0, amp=0.2)
    gated = steady.copy()
    block = SR // 20  # 50ms
    for i in range(0, len(gated), 2 * block):
        gated[i : i + block] = 0.0
    assert hf.modulation_cv(gated, SR) > hf.modulation_cv(steady, SR) + 0.5


def test_rows_and_arm_files_agree():
    """页面只认一张表：run 写的路径必须就是 page 读的路径。

    2026-10-19 实测踩过：`page` 单独跑时按 `{键}.wav` 猜文件名，
    结果读到了**没过后期链**的中间产物（B/C 的 4–8k 差 3.4dB），
    而 A 臂直接显示「缺文件」—— 页面看上去一切正常。
    """
    keys = [k for k, _, _ in hf.ROWS]
    assert set(keys) <= set(hf.ARM_FILES), f"行里出现了表外的臂：{set(keys) - set(hf.ARM_FILES)}"
    vals = list(hf.ARM_FILES.values())
    assert len(set(vals)) == len(vals), f"两个臂指向同一个文件：{vals}"
    for k in ("A_dry", "B_src_hf", "C_fold"):
        assert hf.ARM_FILES[k].endswith("_post.wav"), "三臂必须是过完同一条后期链的产物"
    assert hf.ARM_FILES["00_source"] == "00_source_40k.wav"


def test_peak_guard_scales_only_when_over():
    x = _tone(amp=0.5)
    y, g = hf._peak_guard(x, ceiling=0.99)
    assert g == 1.0 and np.allclose(y, x)
    loud = _tone(amp=2.0)
    y2, g2 = hf._peak_guard(loud, ceiling=0.99)
    assert g2 < 0.6 and float(np.max(np.abs(y2))) <= 0.9901
