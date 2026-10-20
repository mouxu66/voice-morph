# -*- coding: utf-8 -*-
"""`experiments/cover_register.py`（音区 vs 音色）的守卫。

用户 2026-10-20 的判断是「你给的原声是童声」。实测源就是赵雷（CAM++ 0.987 对官方 MV），
真正错的是**我挑的那 20 秒在 345Hz**，而用户自己的参照音中位 116Hz ——
Seed-VC 的 f0-condition 只跟源走，于是输出成了 +19 半音的假声。

这个模块守四件「跑之前看不出来、跑完也看不出来」的事：

1. **降调必须是负的、必须是整数半音**。把 345Hz 搬到 144Hz 得 −15；
   写反了页面照样出、只是出来更高更尖，听起来像"模型坏了"。
2. **挑窗口和报音高必须是同一把尺子（pyin）**。第一版用 YIN 挑，挑到 329.5s 并报 500Hz
   （= fmax 边界），pyin 复核同一窗只有 178.8Hz —— 窗口选错，页面上那句"源 345Hz"
   就整个不成立。fmax 边界的帧必须剔除。
3. **A3 只能是 A2 的 EQ 派生，不许跑模型**。跑一遍模型就换了随机性，
   「补前补后 slope 一样」这个论据当场失效。
4. **页面不许内嵌 base64**，音频走 `clips/` 相对引用（与 refbwe 页同口径）。

全部合成音频 / monkeypatch，不读 media/、不跑 Seed-VC。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

# librosa/soxr 首次加载会触发 importlib.resources 的 DeprecationWarning，
# 而本仓 pytest 开了 -W error。那是第三方库的噪音，和被测逻辑无关，只在本模块放行。
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments"
SCRIPT = EXP / "cover_register.py"
PAGE = EXP / "cover_register_page.py"


def _mod(name: str, path: Path):
    sys.path.insert(0, str(EXP))
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def reg():
    return _mod("cover_register_under_test", SCRIPT)


# ---------------------------------------------------------------- 定调
def test_shift_is_negative_and_integer(reg):
    """345Hz 搬到 144Hz = 降 15 个半音（负整数）。写反就是"更高更尖"。"""
    s = reg.shift_for(345.3, 144.0)
    assert s == -15, s
    assert isinstance(s, int)


def test_shift_octave_and_identity(reg):
    assert reg.shift_for(220.0, 440.0) == 12
    assert reg.shift_for(330.0, 330.0) == 0
    assert reg.shift_for(330.0, 165.0) == -12


def test_shift_rejects_nonpositive(reg):
    with pytest.raises(ValueError):
        reg.shift_for(0.0, 144.0)
    with pytest.raises(ValueError):
        reg.shift_for(300.0, -1.0)


def test_shift_actually_lands_near_target(reg):
    """四舍五入到整数半音后，误差必须 < 1 个半音（否则"降到你的音区"这句话是假的）。"""
    src, tgt = 345.3, 144.0
    got = src * 2 ** (reg.shift_for(src, tgt) / 12)
    assert abs(12 * np.log2(got / tgt)) < 0.6, f"{got:.1f}Hz vs 目标 {tgt}Hz"


# ---------------------------------------------------------------- 挑窗口
def _tone(freq: float, secs: float, sr: int = 16000) -> np.ndarray:
    t = np.arange(int(secs * sr)) / sr
    return (0.35 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_pick_window_finds_high_register(reg):
    """先 12s 的 120Hz、再 8s 的 180Hz：窗口必须落在高音那一段里。

    边界处会出现"半个窗高、半个窗低"的平局（中位仍算 180），那是可接受的
    （起点 8–14 都意味着窗口以高音为主），所以断言给一个区间而不是一个点。
    """
    y = np.concatenate([_tone(120.0, 12.0), _tone(180.0, 8.0)])
    start, med = reg.pick_window(y, 16000, dur=6.0, scan_from=0.0)
    assert 8.0 <= start <= 14.0, f"挑到 {start:.1f}s，没落在高音段里"
    assert 165 <= med <= 195, f"该窗中位 {med:.1f}Hz"


def test_pick_window_ignores_fmax_boundary(reg):
    """fmax 边界的帧是失败帧，不是高音 —— 不能把窗口拽到最后那段纯噪声上。"""
    rng = np.random.default_rng(0)
    noise = (0.2 * rng.standard_normal(16000 * 6)).astype(np.float32)
    y = np.concatenate([_tone(150.0, 10.0), noise])
    start, med = reg.pick_window(y, 16000, dur=6.0, scan_from=0.0)
    assert start < 10.0, f"被噪声段骗到了 {start:.1f}s"
    assert med < 300, med


def test_pick_window_stays_inside_audio(reg):
    y = _tone(150.0, 5.0)
    start, _ = reg.pick_window(y, 16000, dur=5.0, scan_from=0.0)
    assert 0.0 <= start <= 0.1, start


# ---------------------------------------------------------------- 臂的定义
def test_a3_is_derived_not_a_model_run(reg):
    """A3 必须是 A2 的 EQ 派生：重跑模型会换随机性，"补前补后 slope 一样"就废了。"""
    arms = {a["key"]: a for a in reg.arms_spec(-15, Path("ref_bwe.wav"))}
    a3 = arms["A3_降调_补高频"]
    assert a3["model"] is False
    assert a3["from"] == "A2_降调_你的参照BWE"
    assert a3["shift"] == -15


def test_only_a0_keeps_source_register(reg):
    """除 A0（复现上一页的童声）外，其余臂必须全部降调，且降同一个数。"""
    arms = reg.arms_spec(-15, Path("ref_bwe.wav"))
    assert arms[0]["shift"] == 0
    for a in arms[1:]:
        assert a["shift"] == -15, a["key"]
    assert len(arms) == 5


def test_arms_cover_the_three_references(reg):
    """三条参照必须是三份不同的文件：你的 22k / 你的 BWE / 别人的全带宽。"""
    arms = reg.arms_spec(-15, Path("ref_bwe.wav"))
    refs = [str(a["ref"]) for a in arms]
    assert refs[0] == refs[1] != refs[3], "A1 与 A0 必须同参照（只换音区）"
    assert len({refs[1], refs[2], refs[4]}) == 3


# ---------------------------------------------------------------- 页面
def test_page_does_not_inline_audio(reg):
    page = _mod("cover_register_page_under_test", PAGE)
    rows = [
        {"name": "源（赵雷，原调）", "f0": 345.0, "hf_db": -52.5, "r_env": None,
         "slope": None, "gap_drop": None, "secs_cos": None},
        {"name": "A0_原调_你的参照", "f0": 330.0, "hf_db": -93.0, "r_env": 0.2,
         "slope": 0.05, "gap_drop": -1.0, "secs_cos": 0.43,
         "title": "A0 原调 · 你的参照（复现上一页）", "cls": "bad", "verdict": "几乎没有高频"},
        {"name": "A1_降调_你的参照", "f0": 145.0, "hf_db": -70.0, "r_env": 0.8,
         "slope": 0.4, "gap_drop": -20.0, "secs_cos": 0.41,
         "title": "A1 降15半音 · 你的参照", "cls": "good", "verdict": "跟随嗓音（可用）"},
    ]
    meta = {"src": "src.wav", "start": 313.5, "dur": 20.0, "shift": -15,
            "src_f0": 345.3, "ref_median": 115.9, "ref_p90": 144.0, "rows": rows}
    page._encode = lambda *a, **k: None  # noqa: SLF001  不跑 ffmpeg
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        (out / "src.wav").write_bytes(b"")
        html = page.build(out, meta).read_text(encoding="utf-8")
    assert "base64" not in html
    assert "clips/1.mp3" in html and "clips/2.mp3" in html
    for r in rows[1:]:
        assert r["title"] in html
    assert "降 <b>15 半音</b>" in html
    assert "CAM++ = 0.9866" in html or "0.9866" in html
