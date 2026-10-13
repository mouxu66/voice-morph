# -*- coding: utf-8 -*-
"""band_audit 测试：钉死「逐频点 dB」判带宽的正确性。

为什么这些用例值钱（2026-10-13）：
用错判据会让"素材够不够喂 40k"这个决策反过来 —— 项目 MEMORY.md 明确记着
「判带宽要用逐频点绝对 dB，99.5% 累积能量带宽区分不开」（语音能量本就集中在低频，
累积法在任何正常语音上都给出相近的高频数，看着都"够"）。

所以这里不测"某个真实文件的数字"，而是测**方法的判别力**：
造两个已知带宽的信号 —— 全带宽白噪声 vs 8kHz 硬限带 —— 必须被分开。
"""
import os
import sys

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import band_audit as ba  # noqa: E402


def _white(sr, secs=4.0, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(sr * secs)) * 0.2).astype(np.float32)


def _lowpass_fft(x, sr, cutoff):
    """频域硬截断（砖墙低通），用作「确定带宽」的参照。"""
    n = len(x)
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(n, 1 / sr)
    spec[f > cutoff] = 0
    return np.fft.irfft(spec, n).astype(np.float32)


def _write(path, x, sr):
    sf.write(str(path), x, sr, subtype="PCM_16")
    return str(path)


class TestMethodDiscriminates:
    def test_white_noise_is_flat_across_bands(self, tmp_path):
        """白噪声：各频带应大致平坦（±4dB），说明参考带归一化没问题。"""
        p = _write(tmp_path / "white.wav", _white(44100), 44100)
        sr, lv, used = ba.band_levels(p)
        assert sr == 44100 and used > 0
        # 4-6k 与 8-10k 都应有内容
        assert lv[6400] > -10, f"白噪声 4-6k 应平坦，实测 {lv[6400]:.1f}dB"
        assert lv[10000] > -10, f"白噪声 8-10k 应平坦，实测 {lv[10000]:.1f}dB"

    def test_8k_lowpass_is_separated_from_white(self, tmp_path):
        """★ 核心判别力：8k 限带信号在 8-10k 必须显著低于白噪声。"""
        sr = 44100
        w = _white(sr)
        p_full = _write(tmp_path / "full.wav", w, sr)
        p_band = _write(tmp_path / "band8.wav", _lowpass_fft(w, sr, 8000), sr)

        _, lv_full, _ = ba.band_levels(p_full)
        _, lv_band, _ = ba.band_levels(p_band)

        gap = lv_full[10000] - lv_band[10000]
        assert gap > 20, f"8-10k 处两者应拉开 >20dB，实测只差 {gap:.1f}dB"
        # 砖墙在 8-10k 的读数受窗泄漏支配（实测 -36dB），不是真内容
        assert lv_band[10000] < -25

    def test_ceiling_not_fooled_by_window_leakage(self, tmp_path):
        """★ 回归：砖墙低通紧邻截止点那一带的读数（-30~-36dB 泄漏）不得算「有内容」。

        这是 2026-10-13 实际踩到的坑：以为 Hann 窗泄漏地板在 -60dB，门限就设了
        -50dB —— 实际紧邻截止频率的那一带会读到 **-30~-36dB**（主瓣落在截止点附近），
        于是理想砖墙（确定限带）被误判成"有内容"。
        ⇒ 门限必须明显高于 -36dB（现取 -20dB）。
        """
        sr = 44100
        p = _write(tmp_path / "band8.wav", _lowpass_fft(_white(sr), sr, 8000), sr)
        _, lv, _ = ba.band_levels(p)
        leaked = lv[10000]
        assert -45 < leaked < -25, f"紧邻截止带的泄漏应落在 (-45,-25)，实测 {leaked:.1f}dB"
        assert leaked < ba.PRESENT_DB, (
            f"泄漏 {leaked:.1f}dB 必须低于门限 {ba.PRESENT_DB}，否则限带会被误判有内容"
        )

    def test_leakage_decays_fast_above_cutoff(self, tmp_path):
        """截止点再往上应立刻掉到噪声级（-80dB 区），这是「砖墙」的指纹。"""
        sr = 44100
        p = _write(tmp_path / "band8.wav", _lowpass_fft(_white(sr), sr, 8000), sr)
        _, lv, _ = ba.band_levels(p)
        assert lv[12000] < -70, f"12-14k 应掉到噪声级，实测 {lv[12000]:.1f}dB"

    def test_content_ceiling_detects_bandlimit(self, tmp_path):
        """★ 砖墙 8k：内容上界应被判在 8k 一带（不被泄漏抬到 10k 以上）。"""
        sr = 44100
        p = _write(tmp_path / "band8.wav", _lowpass_fft(_white(sr), sr, 8000), sr)
        rows = ba.audit([p])
        row = rows[0]
        assert row["ceiling_hz"] is not None
        assert row["ceiling_hz"] <= 8000, f"应判为 ≤8k，实测 {row['ceiling_hz']}"

    def test_verdict_flags_truly_empty_hf(self, tmp_path):
        """★ 结论层：**真没有**高频的素材必须标红（用 12-14k 判据）。

        现实对照：本 PPT 素材 12-14k 实测 ≈ -67dB —— 高频空但有底噪。

        构造：在频域把 8k 以上整体压到 -65dB。
        """
        sr = 44100
        w = _white(sr)
        n = len(w)
        f = np.fft.rfftfreq(n, 1 / sr)
        spec = np.fft.rfft(w)
        ref = np.mean(np.abs(spec[(f >= 400) & (f < 800)]) ** 2) + 1e-30
        target = 10 ** (-65 / 10) * ref
        hi = f > 8000
        spec[hi] = spec[hi] * np.sqrt(target / (np.mean(np.abs(spec[hi]) ** 2) + 1e-30))
        p = _write(tmp_path / "emptyhf.wav", np.fft.irfft(spec, n).astype(np.float32), sr)

        row = ba.audit([p])[0]
        v1214 = row["levels"].get(14000)
        assert v1214 is not None, "应能测到 12-14k 带"
        assert v1214 < ba.HF_EMPTY_DB, f"构造失败：12-14k 应 <{ba.HF_EMPTY_DB}dB，实测 {v1214:.0f}dB"
        assert "高频为空" in ba.verdict(row), ba.verdict(row)

    def test_band_8_10k_is_leakage_polluted(self, tmp_path):
        """★ 关键回归：8-10k **不能**当判据 —— 它对『真空』和『压到 -65dB』读同一个值。

        这是 2026-10-13 花了几轮才定位的坑：8-10k 紧邻语音主能量区，
        Hann 窗主瓣把它抬到 -36dB，而这个地板与高频内容无关。
        干净判据在 12-14k（泄漏已降到 -87dB）。
        """
        sr = 44100
        w = _white(sr)
        n = len(w)
        f = np.fft.rfftfreq(n, 1 / sr)
        base = np.fft.rfft(w)

        # A: 8k 以上完全清零（理想砖墙）
        sa = base.copy()
        sa[f > 8000] = 0
        pa = _write(tmp_path / "zero.wav", np.fft.irfft(sa, n).astype(np.float32), sr)

        # B: 8k 以上压到 -65dB
        sb = base.copy()
        ref = np.mean(np.abs(base[(f >= 400) & (f < 800)]) ** 2) + 1e-30
        tgt = 10 ** (-65 / 10) * ref
        hi = f > 8000
        sb[hi] = sb[hi] * np.sqrt(tgt / (np.mean(np.abs(sb[hi]) ** 2) + 1e-30))
        pb = _write(tmp_path / "quiet.wav", np.fft.irfft(sb, n).astype(np.float32), sr)

        _, la, _ = ba.band_levels(pa)
        _, lb, _ = ba.band_levels(pb)

        # 8-10k：两者几乎相同 → 不可用
        assert abs(la[10000] - lb[10000]) < 2.0, (
            f"8-10k 本应对两者读同一值（泄漏污染），实测 {la[10000]:.1f} vs {lb[10000]:.1f}"
        )
        # 12-14k：明显分开 → 可用
        assert lb[14000] - la[14000] > 15, (
            f"12-14k 应能分开两者，实测 {la[14000]:.1f}(零) vs {lb[14000]:.1f}(-65dB)"
        )

    def test_gradual_rolloff_is_not_flagged_empty(self, tmp_path):
        """★ 渐变滚降（真人声常见）不该被标「高频为空」——那是砖墙+噪声底的判据。"""
        sr = 44100
        w = _white(sr)
        n = len(w)
        f = np.fft.rfftfreq(n, 1 / sr)
        spec = np.fft.rfft(w)
        gain = np.ones_like(f)
        hi = f > 6000
        gain[hi] = 10 ** (-(np.log2(f[hi] / 6000) * 12) / 20)
        out = np.fft.irfft(spec * gain, n).astype(np.float32)
        p = _write(tmp_path / "rolloff.wav", out, sr)
        row = ba.audit([p])[0]
        v = ba.verdict(row)
        assert "高频为空" not in v, f"渐变滚降被误判：{v}（8-10k={row['levels'].get(10000):.0f}dB）"

    def test_wideband_verdict_is_positive(self, tmp_path):
        """全带宽素材应判「充足」，不能误报。"""
        p = _write(tmp_path / "white.wav", _white(44100), 44100)
        row = ba.audit([p])[0]
        v = ba.verdict(row)
        assert "充足" in v, v
        assert "高频为空" not in v


class TestRobustness:
    def test_silence_does_not_crash(self, tmp_path):
        p = _write(tmp_path / "sil.wav", np.zeros(44100 * 2, dtype=np.float32), 44100)
        sr, lv, used = ba.band_levels(p)
        assert lv is None or used == 0  # 全静音：不产生假的"高频"

    def test_too_short_returns_none(self, tmp_path):
        p = _write(tmp_path / "short.wav", _white(44100, 0.05), 44100)
        sr, lv, used = ba.band_levels(p)
        assert lv is None

    def test_quiet_but_valid_speech_is_measured(self, tmp_path):
        """很轻但有内容的语音不该被当成静音（活跃帧用相对分位，不看绝对电平）。"""
        sr = 44100
        x = _lowpass_fft(_white(sr), sr, 16000) * 0.005  # 极轻
        p = _write(tmp_path / "quiet.wav", x, sr)
        sr2, lv, used = ba.band_levels(p)
        assert lv is not None and used > 0, "轻信号也应测出内容"

    def test_directory_sampling(self, tmp_path):
        d = tmp_path / "ds"
        d.mkdir()
        for i in range(3):
            _write(d / f"a{i}.wav", _white(44100, seed=i), 44100)
        rows = ba.audit([str(d)])
        assert rows[0]["files"] == 3 and rows[0]["sampled"] == 3

    def test_missing_dir_reports_error(self, tmp_path):
        rows = ba.audit([str(tmp_path / "nope")])
        assert "error" in rows[0]

    def test_nyquist_limit_skips_high_bands(self, tmp_path):
        """22.05k 素材：超过奈奎斯特的频带应被跳过，不能造出假读数。"""
        p = _write(tmp_path / "kb.wav", _white(22050), 22050)
        sr, lv, used = ba.band_levels(p)
        assert sr == 22050
        assert 18000 not in lv, "22.05k 的奈奎斯特是 11k，不该有 18k 带"
        assert 10000 in lv
