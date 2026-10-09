"""发声负荷测量单测（大创减负实测层，2026-10-09）。

被测：`tools/voice_load.py`（measure / compare，复用 voicebank_eval.detect_silences）。

★ 守的都是"算错了不会报错、只会给假结论"的地方：
  1. 占空比与连续段边界：合成音频的发声/静音模式是已知的，
     工具给出来的占空比、最长连续发声、段数必须对得上（留斜坡容差）。
  2. 全静音 → duty=0、段数 0；全程发声 → duty≈1.0 —— 两个极端都别给中间数。
  3. 节省率的分母保护：A 侧发声为 0 时 saving_rate 必须是 None（"没法定义"），
     绝不能是 0.0（会被读成"测过了，没减负"）或 100%（被读成"全减掉了"）。
  4. 找不到 ffmpeg 时必须报错：detect_silences 的降级口径（返回空表）在
     切档那边是"退化成硬切"，在减负这边就是"全程在发声"的假结论 ——
     voice_load 明确不接这个降级（模块 docstring 口径 4）。
  5. 参数落 JSON：换 --min-silence 就是换口径，必须能从落盘文件里复现出来。
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pytest  # noqa: E402
import soundfile as sf  # noqa: E402

import voice_load as vl  # noqa: E402


def _write_wav(path: Path, segments: list[tuple[float, float]], total: float = 10.0,
               sr: int = 16000, amp: float = 0.4, freq: float = 440.0) -> Path:
    """按 (起, 止) 发声段写合成音频；其余时间严格为 0。"""
    t = np.arange(int(sr * total)) / sr
    y = np.zeros_like(t)
    for s, e in segments:
        m = (t >= s) & (t < e)
        y[m] = amp * np.sin(2 * np.pi * freq * t[m])
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), y, sr)
    return path


# 1s 静音 + 2s 发声 + 1s 静音 + 3s 发声 + 2s 静音 + 1s 发声 = 10s，发声 6s
SEG_PATTERN = [(1.0, 3.0), (4.0, 7.0), (9.0, 10.0)]


class TestMeasure:
    def test_duty_and_segments_on_known_pattern(self, tmp_path, ffmpeg_bin):
        wav = _write_wav(tmp_path / "a.wav", SEG_PATTERN)
        st = vl.phonation_stats(wav)
        assert st["total_s"] == pytest.approx(10.0, abs=0.01)
        assert st["phonation_s"] == pytest.approx(6.0, abs=0.5)  # 斜坡/边界容差
        assert st["duty_cycle"] == pytest.approx(0.6, abs=0.05)
        assert st["longest_continuous_s"] == pytest.approx(3.0, abs=0.3)
        assert st["n_segments"] == 3
        # 段明细要能对回总和（复核用的字段不能是摆设）
        assert st["phonation_s"] == pytest.approx(
            sum(e - s for s, e in st["segments"]), abs=0.01)

    def test_silence_only_gives_zero(self, tmp_path, ffmpeg_bin):
        wav = _write_wav(tmp_path / "sil.wav", [], amp=0.0)
        st = vl.phonation_stats(wav)
        assert st["phonation_s"] == pytest.approx(0.0, abs=0.05)
        assert st["duty_cycle"] == pytest.approx(0.0, abs=0.01)
        assert st["n_segments"] == 0
        assert st["longest_continuous_s"] == pytest.approx(0.0, abs=0.01)

    def test_continuous_speech_gives_full_duty(self, tmp_path, ffmpeg_bin):
        wav = _write_wav(tmp_path / "full.wav", [(0.0, 10.0)])
        st = vl.phonation_stats(wav)
        assert st["duty_cycle"] > 0.95
        assert st["n_segments"] == 1
        assert st["longest_continuous_s"] == pytest.approx(10.0, abs=0.3)

    def test_params_recorded_in_stats(self, tmp_path, ffmpeg_bin):
        wav = _write_wav(tmp_path / "a.wav", SEG_PATTERN)
        st = vl.phonation_stats(wav, min_silence_s=0.5)
        assert st["min_silence_s"] == 0.5
        assert isinstance(st["noise_db"], float)  # 门限必须落盘，换口径可追溯

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            vl.phonation_stats(tmp_path / "nope.wav")


class TestCompare:
    def test_saving_rate_on_known_pattern(self, tmp_path, ffmpeg_bin):
        a = vl.phonation_stats(_write_wav(tmp_path / "a.wav", SEG_PATTERN))  # 6s
        b = vl.phonation_stats(_write_wav(tmp_path / "b.wav", [(0.0, 2.0)]))  # 2s
        res = vl.compare_load(a, [b])
        sv = res["saving"]
        assert sv["a_phonation_s"] == pytest.approx(6.0, abs=0.5)
        assert sv["b_phonation_s"] == pytest.approx(2.0, abs=0.5)
        assert sv["saving_rate"] == pytest.approx(2.0 / 3.0, abs=0.08)
        # 最长连续：A 3s → B 2s（逐文件取最大，不是求和）
        assert sv["b_longest_continuous_s"] == pytest.approx(2.0, abs=0.3)

    def test_zero_a_phonation_gives_none_not_number(self, tmp_path, ffmpeg_bin):
        a = vl.phonation_stats(_write_wav(tmp_path / "a.wav", [], amp=0.0))
        b = vl.phonation_stats(_write_wav(tmp_path / "b.wav", [(0.0, 2.0)]))
        res = vl.compare_load(a, [b])
        assert res["saving"]["saving_rate"] is None
        # 绝不夹成 0.0 或 1.0 —— 那是"测过了"的假结论；saving_s 本身如实给负值
        assert res["saving"]["saving_s"] == pytest.approx(-2.0, abs=0.5)

    def test_empty_b_rejected(self, tmp_path, ffmpeg_bin):
        a = vl.phonation_stats(_write_wav(tmp_path / "a.wav", SEG_PATTERN))
        with pytest.raises(ValueError, match="至少要给一条"):
            vl.compare_load(a, [])

    def test_synth_recorded_but_not_in_saving(self, tmp_path, ffmpeg_bin):
        a = vl.phonation_stats(_write_wav(tmp_path / "a.wav", SEG_PATTERN))
        b = vl.phonation_stats(_write_wav(tmp_path / "b.wav", [(0.0, 2.0)]))
        syn = vl.synth_duration(_write_wav(tmp_path / "n.wav", [(0.0, 9.0)],
                                           total=9.0))
        res = vl.compare_load(a, [b], synth=syn)
        assert res["synth"]["total_s"] == pytest.approx(9.0, abs=0.05)
        # 合成旁白不参与节省率：把 synth 拿掉再算一遍，saving 必须一字不差
        res2 = vl.compare_load(a, [b])
        assert res["saving"] == res2["saving"]


class TestCli:
    def test_measure_json_roundtrip(self, tmp_path, ffmpeg_bin):
        wav = _write_wav(tmp_path / "a.wav", SEG_PATTERN)
        out = tmp_path / "out.json"
        rc = vl.main(["measure", str(wav), "--json", str(out),
                      "--min-silence", "0.3"])
        assert rc == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["tool"] == "voice_load"
        assert data["params"]["min_silence_s"] == 0.3
        assert len(data["files"]) == 1
        assert data["files"][0]["duty_cycle"] == pytest.approx(0.6, abs=0.05)

    def test_compare_json_includes_saving(self, tmp_path, ffmpeg_bin):
        a = _write_wav(tmp_path / "a.wav", SEG_PATTERN)
        b = _write_wav(tmp_path / "b.wav", [(0.0, 2.0)])
        out = tmp_path / "cmp.json"
        rc = vl.main(["compare", "--a", str(a), "--b", str(b), "--json", str(out)])
        assert rc == 0
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["saving"]["saving_rate"] == pytest.approx(2.0 / 3.0, abs=0.08)

    def test_cli_error_is_reported_not_crash(self, tmp_path):
        rc = vl.main(["measure", str(tmp_path / "nope.wav")])
        assert rc == 2
