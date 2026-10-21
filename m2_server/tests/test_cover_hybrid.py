"""`experiments/cover_hybrid.py`（换参照 + 频带拼接）的守卫。

这一轮八条臂里有六条是**推理/信号处理产物**，最容易出的错不是模型报错，
而是"拼接点两边拿反了"：6.5k 以下用了 Seed-VC、以上用了 RVC，听感会更糟，
但指标几乎不动。所以这里用两个纯音把 `blend_band` 的方向钉死：
低频必须来自 A，高频必须来自 B。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments"
SCRIPT = EXP / "cover_hybrid.py"


def _mod(name: str, path: Path):
    sys.path.insert(0, str(EXP))
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def hybrid():
    return _mod("cover_hybrid_under_test", SCRIPT)


def _tone(freq: float, secs: float = 1.0, sr: int = 44100) -> np.ndarray:
    t = np.arange(int(secs * sr)) / sr
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _peak(y: np.ndarray, sr: int, lo: float, hi: float) -> float:
    spec = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    f = np.fft.rfftfreq(len(y), 1.0 / sr)
    band = (f >= lo) & (f <= hi)
    return float(spec[band].max())


def test_blend_takes_low_from_a_high_from_b(hybrid, tmp_path):
    """A 只有 1k、B 只有 10k —— 拼完必须两者都在，且高频不能来自 A。"""
    sr = 44100
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    sf.write(str(a), _tone(1000.0), sr, subtype="PCM_16")
    sf.write(str(b), _tone(10000.0), sr, subtype="PCM_16")
    out = hybrid.blend_band(a, b, tmp_path / "out.wav", crossover=6500.0)
    y, got_sr = sf.read(str(out), dtype="float32")
    assert got_sr == sr
    lo = _peak(y, sr, 900.0, 1100.0)
    hi = _peak(y, sr, 9500.0, 10500.0)
    assert _peak(_tone(1000.0), sr, 9500.0, 10500.0) < 0.01 * lo, "A 本来就不该有高频"
    assert lo > 0.05 and hi > 0.05, "拼接把某一侧整段丢了"


def test_eight_arms_unique_and_ordered(hybrid):
    """八条 key 唯一；说明与 key 一一对应；页面总数不超过标签数。"""
    keys = [k for k, _n in hybrid.ARMS]
    assert len(keys) == 8 and len(set(keys)) == 8
    assert set(dict(hybrid.ARMS)) == set(keys)
    assert len(keys) <= len(hybrid.AB.LABELS)


def test_page_keeps_names_out(hybrid, tmp_path, monkeypatch):
    """页面只出现甲乙丙丁…，不出现模型名、拼接点或参照来源。"""
    rows = [
        {"key": f"arm_{i}", "path": f"/tmp/{i}.wav", "f0": 188.0, "hf_db": -82.0,
         "cam_anchor": 0.70, "cam_speech_ref": 0.2, "note": "x"}
        for i in range(8)
    ]
    monkeypatch.setattr(hybrid.AB, "_encode", lambda *a, **k: None)
    anchor = tmp_path / "a.wav"
    anchor.write_bytes(b"")
    key = hybrid.build_key(rows, list(range(8)))
    html = hybrid.AB.build_page(rows, anchor, key, tmp_path).read_text(encoding="utf-8")
    for bad in ("rvc", "RVC", "seedvc", "Seed-VC", "soulx", "SoulX",
                "blend", "6500", "6.5k", "answer_hybrid"):
        assert bad not in html, f"页面泄露了 {bad!r}"
    assert len(key["map"]) == 8 == len(set(key["map"].values()))
    assert set(key["map"]) == set(hybrid.AB.LABELS[:8])
