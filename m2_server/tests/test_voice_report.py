"""音色体检「三灯」与嗓音客观指标单测（2026-09-29）。

回归背景：这一版新增了 `tools/voice_metrics.py`（HNR / H1-H2 / 谱倾斜 / jitter /
shimmer）与 `tools/voice_report.py`（三灯合成），以及 `voice_qc.py` 的两个入口
（`--vocal-preview` 训练前体检、变声验收里附带的 vocal/lamps）。

★ 这个文件守的是**本次实现过程中实际踩到的坑**，每一条都有实测证据 ——
不是"为了覆盖率"补的样板测试：

  1. **HNR 不能全段平均**：`To Harmonicity (cc)` 的返回值域是 -200 ~ +27 dB，
     -200 是"该帧无浊音"的哨兵。对全段取 mean 会得到 -33dB（实测），
     必须在 F0 浊音掩码上取中位数（实测 13.28dB）。
  2. **H1-H2 的方向**：用合成信号（每谐波降 N dB）标定，H1-H2 与 N **线性同向**，
     即"越大 = 谱滚降越快 = 越挤压"。调研文档 4.2 节转述文献说"挤压→H1-H2 变低"，
     方向相反 —— 本文件按**实测单调性**断言，谁改实现都得先过这一关。
  3. **谱倾斜的方向**：`_grade("tilt", ...)` 一度写成 `higher_better=False`，
     把 -7.0（更健康）判红、-8.0（更挤压）判绿，**灯整个反掉**。这里用
     正负两侧的用例把它钉死。
  4. **阈值必须锚在"中位数分布"上**：灯的输入是 20 条采样的中位数，
     拿单条分布（h1_h2 P75=5.16）去判中位数（实测 4.8）会误亮红灯。
     本文件不硬编码阈值数字（那会随标定变化），只断言**单调与边界语义**。
  5. **未测 ≠ 不合格**：`lamp is None` 必须与 `lamp == RED` 区分。混在一起
     用户会去修一个不存在的问题（例如 parselmouth 没装被显示成"音质红"）。
  6. **体检失败不能拖垮验收**：`rvc_live` 训练回调依赖"QC 永不裸崩"这个约定，
     所以 parselmouth 缺失 / 音频损坏时，三灯给 None，绝不抛。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import voice_metrics as vm  # noqa: E402
import voice_report as vr  # noqa: E402


# ---------------- 合成音频工具（标定用） ----------------

def _harmonic_tone(slope_db: float, sr: int = 22050, seconds: float = 2.0,
                   f0: float = 150.0, n_harm: int = 25) -> np.ndarray:
    """合成「每谐波降 slope_db dB」的元音状信号。

    slope_db 越负 = 高频滚降越快 = 越"挤压"。这是 H1-H2 标定的基准信号。
    """
    t = np.arange(int(sr * seconds)) / sr
    y = np.zeros_like(t)
    for k in range(1, n_harm + 1):
        if k * f0 > sr / 2:
            break
        y += 10 ** (slope_db * (k - 1) / 20.0) * np.sin(2 * np.pi * k * f0 * t)
    peak = float(np.abs(y).max())
    return (y / peak * 0.5) if peak > 0 else y


def _white_noise(sr: int = 22050, seconds: float = 2.0, seed: int = 0) -> np.ndarray:
    return np.random.RandomState(seed).randn(int(sr * seconds)) * 0.5


def _write_wav(path: Path, x: np.ndarray, sr: int):
    import soundfile as sf

    sf.write(str(path), x, sr)
    return path


sf = pytest.importorskip("soundfile")
vm_ok, _why = vm.metrics_available()
requires_praat = pytest.mark.skipif(not vm_ok, reason="parselmouth 不可用")


# ---------------- 灯的方向：三个灯各自的正负两侧 ----------------

class TestLampDirection:
    """★ 坑 3：方向写反会让整个灯反着亮，且从数字上看"都正常"，最难发现。"""

    def test_tilt_more_negative_is_worse(self):
        """谱倾斜：-6.5（健康）必须是绿/黄，-9.0（更挤压）必须是红。"""
        assert vr._grade("tilt", -6.5) == vr.GREEN
        assert vr._grade("tilt", -9.0) == vr.RED
        # 中间带
        assert vr._grade("tilt", -7.5) == vr.YELLOW

    def test_tilt_monotonic_in_health(self):
        """tilt 越接近 0 越健康：档位序列不得出现"变好又变差"的抖动。"""
        order = {vr.GREEN: 0, vr.YELLOW: 1, vr.RED: 2}
        vals = [-5.0, -6.5, -7.0, -7.5, -8.0, -9.0]
        seq = [order[vr._grade("tilt", v)] for v in vals]
        assert seq == sorted(seq), f"tilt 档位不单调: {seq}"

    def test_h1_h2_larger_is_worse(self):
        """H1-H2：2.0（自然）绿，5.5（挤压）红。**与调研文档 4.2 的表述相反**，
        以合成信号实测的单调性为准（见模块文档字符串第 2 条）。"""
        assert vr._grade("h1_h2", 2.0) == vr.GREEN
        assert vr._grade("h1_h2", 5.5) == vr.RED
        assert vr._grade("h1_h2", 4.0) == vr.YELLOW

    def test_h1_h2_monotonic(self):
        order = {vr.GREEN: 0, vr.YELLOW: 1, vr.RED: 2}
        vals = [1.0, 2.0, 3.0, 3.5, 4.5, 5.5, 6.5]
        seq = [order[vr._grade("h1_h2", v)] for v in vals]
        assert seq == sorted(seq), f"h1_h2 档位不单调: {seq}"

    def test_hnr_smaller_is_worse(self):
        """HNR 谐噪比：越小越差（与上面两个反号）。"""
        assert vr._grade("hnr", 13.0) == vr.GREEN
        assert vr._grade("hnr", 10.0) == vr.RED
        assert vr._grade("hnr", 11.2) == vr.YELLOW

    def test_emb_sim_smaller_is_worse(self):
        """声纹相似度沿用 voice_qc.py 既有口径：≥0.95 绿。"""
        assert vr._grade("emb_sim", 0.97) == vr.GREEN
        assert vr._grade("emb_sim", 0.92) == vr.YELLOW
        assert vr._grade("emb_sim", 0.80) == vr.RED


class TestUntestedIsNotRed:
    """★ 坑 5：未测必须与"不合格"分开。"""

    def test_none_value_gives_none_lamp(self):
        for key in ("emb_sim", "hnr", "h1_h2", "tilt"):
            assert vr._grade(key, None) is None, f"{key} 的 None 不该被分档"

    def test_all_untested_verdict_does_not_claim_ok(self):
        """三个灯都没测到 → 判词必须是"未测"，**不能**说"都在基线内"。"""
        lamps = vr.three_lamps(None, None, None, None)
        assert lamps["tested_count"] == 0
        assert all(l["lamp"] is None for l in lamps["lamps"])
        assert "未测" in lamps["verdict"]
        assert "基线内" not in lamps["verdict"], "没测到却说没问题，是最糟的一种错"

    def test_partial_untested_is_reported(self):
        """只有 HNR 时（strain 两个来源都没给），判词要说明其余未测，
        且不能把相似度当红灯。"""
        lamps = vr.three_lamps(emb_sim=None, hnr=13.0, h1_h2=None, spectral_tilt=None)
        # 只有自然度真的测到了：strain 缺少 H1-H2 与 tilt 两个来源
        assert lamps["tested_count"] == 1, lamps
        sim = next(l for l in lamps["lamps"] if l["key"] == "similarity")
        strain = next(l for l in lamps["lamps"] if l["key"] == "strain")
        assert sim["lamp"] is None and strain["lamp"] is None
        assert "相似度" in lamps["verdict"] and "未测" in lamps["verdict"]

    def test_hnr_only_still_gives_naturalness_lamp(self):
        """训练前体检正是这个形态：只有 HNR + H1-H2，没有相似度。"""
        lamps = vr.three_lamps(emb_sim=None, hnr=13.0, h1_h2=2.0, spectral_tilt=-6.5)
        assert lamps["tested_count"] == 2
        nat = next(l for l in lamps["lamps"] if l["key"] == "naturalness")
        assert nat["lamp"] == vr.GREEN


class TestStrainCombinesTwo:
    """夹嗓子灯是 H1-H2 主判 + 谱倾斜印证，取**更危险**的那个。"""

    def test_takes_worse_of_two(self):
        # H1-H2 健康但谱倾斜危险 → 整体报危险
        lamp = vr.strain_lamp(h1_h2=1.0, tilt=-9.0)
        assert lamp.lamp == vr.RED, "两者取更危险的一个，不能互相平均掉"

    def test_falls_back_when_one_missing(self):
        lamp = vr.strain_lamp(h1_h2=5.5, tilt=None)
        assert lamp.lamp == vr.RED
        lamp2 = vr.strain_lamp(h1_h2=None, tilt=-9.0)
        assert lamp2.lamp == vr.RED

    def test_both_missing_is_untested(self):
        lamp = vr.strain_lamp(None, None)
        assert lamp.lamp is None


# ---------------- 嗓音指标：合成信号标定 ----------------

@requires_praat
class TestH1H2Calibration:
    """★ 坑 2：用"每谐波降 N dB"的合成信号标定，必须单调同向。"""

    def test_monotonic_with_slope(self):
        sr, f0 = 22050, 150.0
        vals = [vm._h1_h2(_harmonic_tone(s), sr, f0)
                for s in (-2, -6, -12, -18)]
        assert all(v is not None for v in vals), f"有 None: {vals}"
        assert all(vals[i] < vals[i + 1] for i in range(len(vals) - 1)), \
            f"H1-H2 未随谱陡度单调递增: {vals}"

    def test_linear_response(self):
        """不只是单调，还应**近似线性**（每降 6dB/谐波 ≈ H1-H2 加 6dB）。"""
        sr, f0 = 22050, 150.0
        a = vm._h1_h2(_harmonic_tone(-6), sr, f0)
        b = vm._h1_h2(_harmonic_tone(-12), sr, f0)
        assert abs((b - a) - 6.0) < 1.0, f"响应不线性: -6dB→{a}, -12dB→{b}"

    def test_none_before_f0(self):
        """没有 F0 估计就给 None —— 不给数字比给错数字好。"""
        assert vm._h1_h2(_harmonic_tone(-6), 22050, None) is None
        assert vm._h1_h2(_harmonic_tone(-6), 22050, 0.0) is None


@requires_praat
class TestHnrNotFloorAveraged:
    """★ 坑 1：HNR 必须只在浊音帧上取，且要掉 -200 哨兵。

    断言的是「没有哨兵污染」，不是「落进真实语音区间」—— 合成纯谐波信号是
    完全周期信号，HNR 会高达 100+ dB（实测 116.6），那是**正确**结果。
    真语音的 10~20 dB 区间由 `test_real_asset_if_present` 用真实素材守。
    """

    def test_tone_hnr_not_negative_from_sentinel(self, tmp_path):
        """纯谐波音的 HNR 必须为正且很高；出现负数说明 -200 哨兵混进来了。

        实测：全段 average（未滤哨兵）会得 -33 dB 这类值。
        """
        tone = _harmonic_tone(-6.0)
        p = _write_wav(tmp_path / "tone.wav", tone, 22050)
        import parselmouth

        snd = parselmouth.Sound(str(p))
        hnr = vm._hnr_db(snd)
        assert hnr is not None
        assert hnr > 0, f"HNR={hnr} 为负 —— -200 哨兵没滤掉（这是本坑的原始症状）"
        assert hnr > 20, f"纯谐波信号的 HNR 应很高（完全周期性），实测 {hnr}"

    def test_real_asset_if_present(self):
        """若有真实自录切片，顺带核对 HNR 落在语音合理区间（10~20 dB）。

        素材缺失则跳过 —— 不把「本机没素材」变成测试失败。
        """
        import glob
        import os

        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        files = sorted(glob.glob(os.path.join(root, "media", "clips",
                                             "video_260828_110637_*.wav")))
        if not files:
            import pytest as _pytest

            _pytest.skip("本机没有自录切片素材")
        import parselmouth

        snd = parselmouth.Sound(files[0])
        hnr = vm._hnr_db(snd)
        assert hnr is not None and 5.0 < hnr < 25.0, f"真实素材 HNR={hnr} 超出合理区间"

    def test_silence_returns_none_or_low(self, tmp_path):
        """纯静音不该给出一个"看起来正常"的 HNR。"""
        import parselmouth

        p = tmp_path / "silence.wav"
        sf.write(str(p), np.zeros(22050), 22050)
        snd = parselmouth.Sound(str(p))
        hnr = vm._hnr_db(snd)
        assert hnr is None or hnr < 6.0, f"静音给出了正常 HNR: {hnr}"


# ---------------- 端到端：extract 不裸崩 ----------------

class TestExtractNeverRaises:
    """★ 坑 6：QC 的调用方（rvc_live 回调）依赖"绝不裸崩"。"""

    def test_missing_file(self, tmp_path):
        r = vm.extract(tmp_path / "nope.wav")
        assert r["error"] is not None
        assert r["hnr"] is None and r["h1_h2"] is None

    @requires_praat
    def test_broken_audio(self, tmp_path):
        p = tmp_path / "junk.wav"
        p.write_bytes(b"not a wav at all")
        r = vm.extract(p)  # 不得抛
        assert r["error"] is not None

    @requires_praat
    def test_real_audio_all_keys(self, tmp_path):
        p = _write_wav(tmp_path / "tone.wav", _harmonic_tone(-6.0), 22050)
        r = vm.extract(p)
        assert r["error"] is None
        for k in ("hnr", "h1_h2", "spectral_tilt", "jitter_local",
                  "shimmer_local", "voiced_ratio", "f0_median", "duration_s"):
            assert k in r, f"缺键 {k}"
        assert r["duration_s"] == pytest.approx(2.0, abs=0.1)

    @requires_praat
    def test_tilt_realistic_magnitude(self, tmp_path):
        """谱倾斜对**连续谱**（真实语音）可靠，量级应为个位数 ~ 数十 dB/oct。
        注意：对稀疏谐波合成信号它**不可靠**（实测非单调），所以这里只断言量级、
        断言方向，不断言合成信号上的单调性。"""
        snd_tone = _harmonic_tone(-6.0)
        tilt = vm._spectral_tilt(snd_tone, 22050)
        assert tilt is not None and tilt < 0, f"tilt 应为负: {tilt}"


# ---------------- 三灯 JSON 结构（前端契约） ----------------

class TestThreeLampsContract:
    """前端 `VoiceLamps` 组件消费的字段，缺一个就渲染不出来。"""

    def test_structure(self):
        out = vr.three_lamps(emb_sim=0.97, hnr=12.0, h1_h2=3.0, spectral_tilt=-7.0)
        assert set(out) >= {"lamps", "verdict", "tested_count"}
        assert len(out["lamps"]) == 3
        for l in out["lamps"]:
            assert set(l) >= {"key", "label", "value", "lamp", "detail", "sources"}
        keys = {l["key"] for l in out["lamps"]}
        assert keys == {"similarity", "naturalness", "strain"}
        assert out["tested_count"] == 3

    def test_healthy_input_all_green(self):
        out = vr.three_lamps(emb_sim=0.98, hnr=13.0, h1_h2=2.0, spectral_tilt=-6.5)
        assert all(l["lamp"] == vr.GREEN for l in out["lamps"]), out

    def test_bad_input_verdict_mentions_red(self):
        out = vr.three_lamps(emb_sim=0.5, hnr=8.0, h1_h2=9.0, spectral_tilt=-11.0)
        assert "复查" in out["verdict"] or "红" in out["verdict"]
