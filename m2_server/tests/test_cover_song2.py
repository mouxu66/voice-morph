# -*- coding: utf-8 -*-
"""《唯一》多变调档那条线（song2）的守卫（2026-10-15 补写）。

背景：这批脚本（`cover_multi_pitch.py` / `cover_f0_check.py` /
`cover_f0_framewise.py` / `cover_song2_page.py` / `cover_song2_verify.py`）
在 2026-10-14 夜里跑出来、试听页也在浏览器里走过一遍，但**守卫测试当时没落盘**
（只存在于会话叙事里），而一次错配提交（信息说的是 song2、内容是别的会话暂存的
wechat 三文件）让"已经提交了"看起来成立。本文件把这批给人下判断用的脚本的
纯函数口径钉住。

守的全是**静默失效**（退化后不报错，只给错误结论/假对照）：

1. `framewise_row`：变调核验必须**只在两边都有声的帧**上逐帧算 ——
   "中位数之商"会把两边的清浊分布差异混进来（本批实测 −12 档被它读成 −9.70）。
   掩码放宽一条，结论就能偏两个半音。
2. `f0_stats`：pyin 的 NaN（无声帧）必须剔除；不剔除中位数直接被拉歪。
3. `duplicate_groups`：试听页 mp3 必须按**内容 md5** 分组查假对照
   （字节数相同不代表不同；本批真撞过八条字节数完全一样）。
4. `render_html`：不存在的条目要跳过、文件名要 HTML 转义、**注入的 groups 必须生效**
   （第一版形参 `groups` 在函数体里根本没用，遍历的是模块级常量 —— docstring 声称
   可注入，实际注入了也白注入）。
5. `suggest_pitch`：《唯一》275.5 Hz vs 参考音 108 Hz（差 16 半音）必须夹到 −12。

真实音频产物在 `media/`（gitignore），所以全部用构造信号锚定口径，
不依赖本机音频（与 test_cover_tilt 同策略）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_EXP = _ROOT / "experiments"
for _p in (_ROOT / "m2_server", _EXP):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _EXP / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fw():
    return _load("cover_f0_framewise_t", "cover_f0_framewise.py")


@pytest.fixture(scope="module")
def f0c():
    return _load("cover_f0_check_t", "cover_f0_check.py")


@pytest.fixture(scope="module")
def page():
    return _load("cover_song2_page_t", "cover_song2_page.py")


@pytest.fixture(scope="module")
def verify():
    return _load("cover_song2_verify_t", "cover_song2_verify.py")


@pytest.fixture(scope="module")
def mp():
    # 这个脚本模块级 import 了 cover_api / rvc_convert（产品依赖，不启预热）；
    # 只为测它的纯函数 suggest_pitch，main() 不会被执行。
    return _load("cover_multi_pitch_t", "cover_multi_pitch.py")


# ------------------------------------------------- framewise_row（逐帧变调核验）


def test_framewise_measures_exact_octave_on_both_voiced(fw):
    """other 严格 = src/2 ⇒ 每帧 −12 半音，中位/p10/p90 全部 −12。"""
    src = np.full(2000, 200.0)
    row = fw.framewise_row(src, src * 0.5, "-12")
    assert row is not None
    assert row["both_voiced"] == 2000
    assert row["frame_shift_median"] == -12.0
    assert row["p10"] == -12.0 and row["p90"] == -12.0
    assert row["spread"] == 0.0
    assert row["p90_ratio_semitones"] == -12.0


def test_framewise_uses_only_frames_voiced_on_both_sides(fw):
    """★ 核心口径：一边无声（0.0）的帧必须剔除 —— 这是它比"中位数之商"稳的原因。

    src 后 500 帧无声、other 在那些帧上照样有值（RVC 输出的清浊分布可以与原唱不同）：
    这些帧不许进统计，both_voiced 必须是 1500 而不是 2000。
    """
    src = np.concatenate([np.full(1500, 200.0), np.zeros(500)])
    other = np.full(2000, 100.0)  # 全程有声
    row = fw.framewise_row(src, other, "x")
    assert row is not None
    assert row["both_voiced"] == 1500
    assert row["frame_shift_median"] == -12.0

    # 反过来：other 侧无声同样剔除
    row2 = fw.framewise_row(other, src, "x")
    assert row2["both_voiced"] == 1500


def test_framewise_refuses_when_too_few_both_voiced(fw):
    """共同有声帧 < MIN_BOTH_VOICED（100）⇒ 不下结论（返回 None），别给一个随机数。"""
    src = np.concatenate([np.full(50, 200.0), np.zeros(2000)])
    other = np.full(2050, 100.0)
    assert fw.framewise_row(src, other, "x") is None


def test_framewise_ignores_length_mismatch_beyond_min(fw):
    """两条曲线长度不等时按短的对齐（真实产物 hop 对齐可能差几帧），不许崩。"""
    src = np.full(1000, 200.0)
    other = np.concatenate([np.full(1000, 100.0), np.full(100, 100.0)])
    row = fw.framewise_row(src, other, "x")
    assert row is not None and row["both_voiced"] == 1000


# ------------------------------------------------------------- f0_stats（粗判）


def test_f0_stats_drops_silent_nan_frames(f0c):
    """pyin 的 NaN = 无声帧，必须剔除后再算统计量。"""
    f0 = np.array([100.0, 200.0, 300.0, np.nan, np.nan])
    row = f0c.f0_stats(f0, "标签", "some/path.wav")
    assert row is not None
    assert row["median_f0_hz"] == 200.0
    assert row["max_hz"] == 300.0
    assert row["p90_hz"] == round(float(np.percentile([100.0, 200.0, 300.0], 90)), 1)
    assert row["label"] == "标签" and row["path"] == "some/path.wav"


def test_f0_stats_all_silent_is_none(f0c):
    assert f0c.f0_stats(np.array([np.nan, np.nan]), "x", "p") is None
    assert f0c.f0_stats(np.array([], dtype=float), "x", "p") is None


# ------------------------------------------------- duplicate_groups（假对照核验）


def test_duplicate_groups_detects_same_content_mp3(verify, tmp_path):
    """同内容（哪怕文件名不同）必须落进同一 md5 组 —— 那整页 A/B 就是假的。"""
    (tmp_path / "a.mp3").write_bytes(b"IDENTICAL-BYTES")
    (tmp_path / "b.mp3").write_bytes(b"IDENTICAL-BYTES")
    (tmp_path / "c.mp3").write_bytes(b"different")
    groups = verify.duplicate_groups(tmp_path)
    # 反查：a/b 同组，c 另一组
    owner = {name: h for h, names in groups.items() for name in names}
    assert owner["a.mp3"] == owner["b.mp3"]
    assert owner["c.mp3"] != owner["a.mp3"]


def test_duplicate_groups_respects_pattern(verify, tmp_path):
    """默认只收 mp3：同名同内容的 .wav 不许混进试听页对照核验。"""
    (tmp_path / "a.mp3").write_bytes(b"X")
    (tmp_path / "a.wav").write_bytes(b"X")
    groups = verify.duplicate_groups(tmp_path)
    names = [n for ns in groups.values() for n in ns]
    assert names == ["a.mp3"]


def test_duplicate_groups_empty_dir(verify, tmp_path):
    assert verify.duplicate_groups(tmp_path) == {}


# ----------------------------------------------------------- render_html（试听页）


def test_render_html_embeds_present_files_and_skips_missing(page, tmp_path):
    (tmp_path / "one.mp3").write_bytes(b"AAAA")
    (tmp_path / "two.mp3").write_bytes(b"BBBB")
    groups = [("组", [("one.mp3", "甲", ""), ("ghost.mp3", "不存在的条目", "")])]
    doc = page.render_html(tmp_path, groups=groups)
    assert doc.count("data:audio/mpeg;base64,") == 1
    assert "ghost.mp3" not in doc, "缺失文件不许出现在页面里"
    assert "甲" in doc


def test_render_html_escapes_injected_text(page, tmp_path):
    """条目名/备注/组标题都是面向页面的文本，含 <& 必须转义，防注入破页。"""
    (tmp_path / "one.mp3").write_bytes(b"AAAA")
    evil = page.render_html(tmp_path,
                           groups=[("<script>x</script>", [("one.mp3", "<b>&", "a&b")])])
    safe = page.render_html(tmp_path,
                           groups=[("ok", [("one.mp3", "plain", "c")])])
    assert "<script>x</script>" not in evil
    # 名字里的 <b> 必须被转义：恶意名与安全名产出的**真标签数必须相等**
    # （未转义会多出一个 <b> 开标签）；转义串本身要在
    assert "&lt;b&gt;&amp;" in evil
    assert evil.count("<b>") == safe.count("<b>")
    assert "a&amp;b" in evil


def test_render_html_honors_injected_groups(page, tmp_path):
    """★ 形参 groups 必须真的被遍历（第一版写死遍历模块级 GROUPS，注入无效）。"""
    (tmp_path / "one.mp3").write_bytes(b"AAAA")
    marker = "自定义组标题ZZQ"
    doc = page.render_html(tmp_path, groups=[(marker, [("one.mp3", "x", "")])])
    assert marker in doc
    # 默认 GROUPS 里的条目名不许混进来（注意：页面静态 .sub 文案里本来就有
    # 「保原调/降八度」字样，所以这里钉的是默认**条目名**，不是组标题词）
    assert "只换音色（无伴奏）" not in doc
    assert "原唱人声（demucs 分离）" not in doc


def test_render_html_exposes_playback_probe(page, tmp_path):
    """无头走查靠 window.__vmAudio / __vmClips 探针确认"真的在播"，少一个就没法验。"""
    doc = page.render_html(tmp_path, groups=[])
    assert "__vmAudio" in doc and "__vmClips" in doc


# ------------------------------------------------------------- suggest_pitch（决策）


def test_suggest_pitch_song2_decision_clamps_to_minus_twelve(mp):
    """《唯一》实测：歌声中位 275.5 Hz / 参考音 108 Hz = −16.1 半音 ⇒ 夹到 −12。

    这条钉的是本脚本第一次跑出来的那个真实决定（与试听页文案一致）。
    """
    assert mp.suggest_pitch(275.5, 108.0) == -12


@pytest.mark.parametrize("song,voice,expect", [
    (108.0, 108.0, 0),       # 已对齐：不动
    (60.0, 500.0, 12),       # 反向超一个八度：夹到 +12
    (None, 108.0, 0),        # 量不出来：不调（拍脑袋的值更糟）
    (275.5, None, 0),
    (None, None, 0),
])
def test_suggest_pitch_edge_cases(mp, song, voice, expect):
    assert mp.suggest_pitch(song, voice) == expect
