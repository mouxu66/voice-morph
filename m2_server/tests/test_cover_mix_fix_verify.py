# -*- coding: utf-8 -*-
"""`experiments/cover_mix_fix_verify.py` 的守卫测试。

为什么要守这个脚本：它是**下"削波修好了"这个结论用的**。它自己错了，结论就会
带着一个假数字写进文档与提交信息，并被后人引用（同族：`test_cover_metrics.py`）。

它最容易错的地方是**前置对照**：`mix_unscaled()` 复刻的是"修前的行为"（一遍 s16、
不缩放）。若有人"顺手"把它也改成防削波版本，前置断言
「不缩放确实削波」就永远拿不到证据 —— 而用例仍然全绿，只是**什么都没证明**。
这与 §8.21（两边都空 = 白绿）是同一族病，所以这里直接钉住"它必须真会削波"。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "experiments" / "cover_mix_fix_verify.py"
_M2 = _ROOT / "m2_server"
for _p in (_M2,):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load_module():
    """按**路径**加载：`experiments/` 不是包，裸 import 找不到它。"""
    spec = importlib.util.spec_from_file_location("cover_mix_fix_verify", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cover_mix_fix_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cmfv():
    return _load_module()


def _loud(path: Path, amp: float = 0.9, secs: float = 1.0) -> Path:
    """立体声测试信号（与真实链路一致；单声道会被 ffmpeg 下混衰减 1/√2）。"""
    sr = 44100
    t = np.arange(int(sr * secs)) / sr
    mono = (amp * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    sf.write(str(path), np.stack([mono, mono], axis=1), sr, subtype="PCM_16")
    return path


# ---------------------------------------------------------------- 量测


def test_measure_counts_clipping(cmfv, tmp_path):
    """`measure()` 是本脚本所有判据的量尺 —— 它必须真能数出削波点。

    造一个峰值顶到 1.0 的信号：s16 落盘后**必有** ≥0.99 的采样点。
    """
    sr = 44100
    t = np.arange(sr) / sr
    loud = np.clip(1.8 * np.sin(2 * np.pi * 220.0 * t), -1.0, 1.0).astype(np.float32)
    p = tmp_path / "clip.wav"
    sf.write(str(p), np.stack([loud, loud], axis=1), sr, subtype="PCM_16")

    m = cmfv.measure(p)
    assert m["peak"] == pytest.approx(1.0, abs=1e-3), m
    assert m["clipped"] > 0, f"没数出削波点：{m}"
    assert m["sr"] == sr and m["dur_s"] == pytest.approx(1.0, abs=0.01)


def test_measure_reports_clean_signal_as_unclipped(cmfv, tmp_path):
    """反向：干净的信号不能被数出削波点（否则前置断言恒真 = 装饰）。"""
    m = cmfv.measure(_loud(tmp_path / "clean.wav", amp=0.3))
    assert m["clipped"] == 0, m
    assert m["peak"] == pytest.approx(0.3, abs=0.01)


# ---------------------------------------------------------------- ★ 前置对照必须真会削波


def test_unscaled_control_really_clips(cmfv, ffmpeg_bin, tmp_path, monkeypatch):
    """★ **核心断言**：修前行为（一遍 s16、不缩放）在这份素材上**必须真的削波**。

    这条就是"前置对照"的牙齿：如果有人把 `mix_unscaled` 改成也防削波
    （或把两组满刻度信号换小），前置断言就失去了证据力 —— 这条会红。
    同族纪律见 §8.19（变异测试）/ 犯错指南 #174 的"前置断言"。
    """
    import common

    monkeypatch.setattr(common, "find_ffmpeg", lambda: ffmpeg_bin)
    monkeypatch.setattr(cmfv, "find_ffmpeg", lambda: ffmpeg_bin)

    vocal = _loud(tmp_path / "v.wav", amp=0.9)
    accomp = _loud(tmp_path / "a.wav", amp=0.9)
    out = tmp_path / "unscaled.wav"
    cmfv.mix_unscaled(vocal, accomp, (3.0, 1.0), out)   # 真峰 3.6 ⇒ 必削

    m = cmfv.measure(out)
    assert m["peak"] >= 0.999, f"对照没顶到满刻度：{m}"
    assert m["clipped"] > 0, f"对照没有削波点，前置断言失去意义：{m}"


def test_product_mix_back_is_clean_on_the_same_material(cmfv, ffmpeg_bin, tmp_path, monkeypatch):
    """同一份"必削"素材过**产品函数** ⇒ 零削波、峰值≤上限、配比保持。

    与上一条成对：一边证明"素材确实会削"，一边证明"产品确实防住了"。
    只跑其中任何一条都不足以说明修复有效。
    """
    import common

    monkeypatch.setattr(common, "find_ffmpeg", lambda: ffmpeg_bin)
    monkeypatch.setattr(cmfv, "find_ffmpeg", lambda: ffmpeg_bin)
    import cover_api

    vocal = _loud(tmp_path / "v.wav", amp=0.9)
    accomp = _loud(tmp_path / "a.wav", amp=0.9)
    out = tmp_path / "fixed.wav"

    _, applied = cover_api.mix_back(vocal, accomp, out, gains=(3.0, 1.0))

    m = cmfv.measure(out)
    assert m["clipped"] == 0, f"产品路径仍有削波点：{m}"
    assert m["peak"] <= cover_api.MIX_CEILING + 5e-3, m
    assert applied[0] < 3.0 and applied[1] < 1.0, applied
    assert applied[0] / applied[1] == pytest.approx(3.0, rel=1e-3), "配比必须保持"


def test_probe_file_does_not_survive_a_failed_run(cmfv, tmp_path, monkeypatch):
    """探测文件要在**异常路径**上也清掉 —— `finally` 而不是顺序执行。

    ffmpeg 失败时若把 `.probe.f32.wav` 留在会话目录，它是个 float32 整轨
    （一首歌几十 MB），而"退出即删"只认会话目录里的正式产物名。
    """
    import cover_api
    import subprocess as _sp

    def boom(path):
        raise RuntimeError("模拟读取峰值失败")

    monkeypatch.setattr(cover_api, "_true_peak", boom)
    monkeypatch.setattr(cover_api, "find_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(
        cover_api.subprocess, "run",
        lambda cmd, **kw: (Path(cmd[-1]).write_bytes(b"RIFF"),
                           _sp.CompletedProcess(cmd, 0, "", ""))[1],
    )

    with pytest.raises(RuntimeError, match="模拟读取峰值失败"):
        cover_api.mix_back(tmp_path / "v.wav", tmp_path / "a.wav", tmp_path / "o.wav")

    leftovers = [p.name for p in tmp_path.iterdir() if "probe" in p.name]
    assert not leftovers, f"异常路径没清探测文件：{leftovers}"
