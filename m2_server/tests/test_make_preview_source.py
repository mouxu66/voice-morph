"""tools/make_preview_source.py 单测（打桩 TTS，只验证音频处理与写盘逻辑）。

背景：市场试听是 RVC voice-to-voice，试听台词由 assets/preview_source.wav 决定，
换台词必须换这个源句音频 —— 本工具就是干这个的。真跑 TTS 太慢且需 GPU，
这里只验：重采样到 16k、峰值归一、静音保护、备份 + 原子替换。
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "make_preview_source.py"


def _load():
    spec = importlib.util.spec_from_file_location("make_preview_source", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_preview_source"] = mod
    spec.loader.exec_module(mod)
    return mod


mps = _load()


def _tone(sr: int, dur: float = 0.5, freq: int = 220) -> np.ndarray:
    t = np.arange(int(sr * dur)) / sr
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_normalize_resamples_to_16k_and_peak():
    x, sr = _tone(24000), 24000
    out = mps.normalize(x, sr)
    # 24000 → 16000 采样率，长度按比例缩短
    assert abs(len(out) - len(x) * 16000 / 24000) < 5
    assert float(np.max(np.abs(out))) == pytest.approx(mps.TARGET_PEAK, abs=1e-3)


def test_normalize_keeps_16k_unchanged_length():
    x = _tone(16000)
    out = mps.normalize(x, 16000)
    assert len(out) == len(x)


def test_silent_synth_does_not_overwrite(tmp_path, monkeypatch):
    """TTS 吐出近静音时不能覆盖现有源句（否则整个市场试听全变哑巴）。"""
    src = tmp_path / "preview_source.wav"
    src.write_bytes(b"ORIGINAL")
    monkeypatch.setattr(mps, "synth", lambda text, ref: (np.zeros(1600, dtype=np.float32), 16000))
    monkeypatch.setattr(mps, "DEFAULT_OUT", src)
    monkeypatch.setattr(
        "sys.argv",
        ["make_preview_source.py", "--text", "你好", "--ref", str(src), "--out", str(src), "--no-backup"],
    )
    assert mps.main() == 1
    assert src.read_bytes() == b"ORIGINAL"


def test_backup_and_atomic_replace(tmp_path, monkeypatch):
    src = tmp_path / "preview_source.wav"
    src.write_bytes(b"OLD")
    monkeypatch.setattr(mps, "synth", lambda text, ref: (_tone(24000), 24000))
    monkeypatch.setattr(
        "sys.argv",
        ["make_preview_source.py", "--text", "大家好，这是我的新声音，你觉得怎么样？",
         "--ref", str(src), "--out", str(src)],
    )
    assert mps.main() == 0
    assert (tmp_path / "preview_source.wav.orig.bak").read_bytes() == b"OLD"
    # 已被真实 wav 替换，且不再是旧内容
    assert src.read_bytes() != b"OLD"
    import soundfile as sf

    x, sr = sf.read(str(src))
    assert sr == 16000
    assert len(x) > 0
    # 临时文件不应残留
    assert not (tmp_path / "preview_source_tmp.wav").exists()
