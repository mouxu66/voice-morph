# -*- coding: utf-8 -*-
"""`experiments/cover_my_voice.py` 的守卫测试（把歌声换成指定 RVC 音色）。

三条退化都**不会报错，只会给出错误成品**：

1. **变调算错一个八度** —— RVC 只照抄源音频的 F0 曲线，所以音域对齐全靠这一个整数。
   算错就是整首高/低八度，而链路全程无异常、文件照常生成。
2. **削波探测用 pcm_s16 写盘** —— 超过满刻度的和会**先被削到 1.000**，"量峰再缩放"
   就永远量到 1.000、永不生效。本轮实测踩过：第二次混音仍有 47,608 个削波采样点
   （2026-10-14，Lorezo - Feed the One）。探测那遍必须写 float32。
3. **±12 夹紧被去掉** —— 测量被污染时（哼唱段 / 纯气声 / 伴奏漏进人声）会算出 ±30 半音
   这种"对齐"，听感上已经不是同一首歌。

测试策略：`suggest_pitch` 是纯函数直接测；防削波要真 ffmpeg（合成两个满刻度信号），
交给 `ffmpeg_bin` 夹具做"本机缺就跳过 / CI 缺就失败"的非对称处理。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _PROJECT_ROOT / "experiments" / "cover_my_voice.py"


def _load_module():
    """按**路径**加载：`experiments/` 不是包，裸 import 找不到它。"""
    spec = importlib.util.spec_from_file_location("cover_my_voice", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cover_my_voice"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cmv():
    return _load_module()


# ---------------------------------------------------------------- 变调决策


def test_suggest_pitch_gives_one_octave_down_for_this_song(cmv):
    """2026-10-14 实测素材：歌声 219.5 Hz / 目标音色 108.0 Hz ⇒ −12 半音。

    这条钉的是**真实数字**（不是构造值）：它是本脚本第一次跑出来的那个决定，
    以后有人动了 `suggest_pitch`，这里会红。
    """
    assert cmv.suggest_pitch(219.5, 108.0) == -12


def test_suggest_pitch_is_zero_when_already_aligned(cmv):
    """音域本来就接近（说话 vs 说话）就**不该调** —— 调了反而把音色推离参考。"""
    assert cmv.suggest_pitch(110.0, 108.0) == 0


def test_suggest_pitch_clamps_to_one_octave(cmv):
    """离谱的比值（测量被污染时）必须夹在 ±12 —— 超一个八度的"对齐"不是同一个人的音域差。

    500→60 Hz 的原始比值是 −36.7 半音，夹紧后只能是 −12。
    """
    assert cmv.suggest_pitch(500.0, 60.0) == -12
    assert cmv.suggest_pitch(60.0, 500.0) == 12


@pytest.mark.parametrize("song,voice", [(None, 108.0), (219.5, None), (None, None), (0.0, 108.0)])
def test_suggest_pitch_without_measurements_is_zero(cmv, song, voice):
    """量不出来就**不调**：调一个拍脑袋的值比不调更糟（整首跑调 vs 音域略偏）。"""
    assert cmv.suggest_pitch(song, voice) == 0


# ---------------------------------------------------------------- 防削波


#: 两声部的频率**故意取同一个**：不同频率的正弦相加，峰值不是简单相加
#: （220+330 Hz 的合成峰是 0.674×(a+b)，不是 a+b）—— 那会让断言里写死的期望值与
#: 实际对不上，而红的原因看起来像"代码错了"。同频则峰值就是 a+b，一眼可验。
_TEST_FREQ = 220.0


def _loud(path: Path, secs: float = 1.0, amp: float = 0.9) -> Path:
    """写**立体声**测试信号 —— 与真实链路一致（demucs 的 stem 本来就是立体声）。

    单声道输入会被 ffmpeg 的默认下混矩阵衰减 1/√2（实测：两个 0.2 的正弦相加量到
    0.283 而不是 0.4），于是断言里那个"0.2+0.2=0.4"就不成立，而红的理由看着像代码坏了。
    """
    sr = 44100
    t = np.arange(int(sr * secs)) / sr
    mono = (amp * np.sin(2 * np.pi * _TEST_FREQ * t)).astype(np.float32)
    sf.write(str(path), np.stack([mono, mono], axis=1), sr, subtype="PCM_16")
    return path


def test_mix_peak_safe_prevents_clipping(cmv, ffmpeg_bin, tmp_path, monkeypatch):
    """两条满刻度信号按 ×3.0 相加 ⇒ 真峰值 2.7，必须被缩到 ≤0.98 且**一个削波点都没有**。

    前置断言（最后一段）刻意保留：它证明"这份素材确实会削波"——
    否则一个把增益恒定缩到很小的实现也能让上面那些断言通过，而这个用例本该抓住它。
    """
    import common

    monkeypatch.setattr(common, "find_ffmpeg", lambda: ffmpeg_bin)
    vocal = _loud(tmp_path / "v.wav", amp=0.9)   # ×3.0 ⇒ 2.7
    accomp = _loud(tmp_path / "a.wav", amp=0.9)  # ⇒ 相加 3.6，远超满刻度
    out = tmp_path / "mix.wav"

    peak, clipped, applied = cmv.mix_peak_safe(vocal, accomp, out, gains=(3.0, 1.0))

    assert out.is_file(), "混音产物没落盘"
    # 容差 5e-3：缩放系数是按 float 探测值算的，而落盘是 pcm_s16 ——
    # 量化后会有 ~1e-5 级的偏差；真正要守住的是"没有削波采样点"那一条。
    assert peak <= cmv.MIX_CEILING + 5e-3, f"峰值 {peak} 超过上限 {cmv.MIX_CEILING}"
    assert clipped == 0, f"仍有 {clipped} 个削波采样点"
    assert applied[0] < 3.0 and applied[1] < 1.0, "超顶时应等比缩小两侧增益"
    assert applied[0] / applied[1] == pytest.approx(3.0, rel=1e-3), "配比必须保持不变"

    # 前置：不缩放的话确实会超顶（用 float 探测才看得到真值）
    raw = tmp_path / "raw_f32.wav"
    cmv._mix_once(vocal, accomp, (3.0, 1.0), raw, float_out=True)
    true_peak, _ = cmv._peak(raw)
    assert true_peak > cmv.MIX_CEILING, f"素材没超顶（{true_peak}），本用例失去意义"


def test_mix_peak_safe_leaves_quiet_material_untouched(cmv, ffmpeg_bin, tmp_path, monkeypatch):
    """本来就不过顶的素材不许被动 —— 缩放是"救命"，不是常规操作。

    这条防的是"防削波"被写成"一律缩一半"：那会让所有正常翻唱平白变轻。
    """
    import common

    monkeypatch.setattr(common, "find_ffmpeg", lambda: ffmpeg_bin)
    vocal = _loud(tmp_path / "v.wav", amp=0.2)
    accomp = _loud(tmp_path / "a.wav", amp=0.2)
    out = tmp_path / "mix.wav"

    peak, clipped, applied = cmv.mix_peak_safe(vocal, accomp, out, gains=(1.0, 1.0))

    assert applied == (1.0, 1.0), f"不该动增益，实际 {applied}"
    assert clipped == 0
    assert peak == pytest.approx(0.4, abs=0.02), f"峰值应≈0.4（0.2+0.2），实际 {peak}"
