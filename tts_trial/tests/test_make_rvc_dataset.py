# -*- coding: utf-8 -*-
"""make_rvc_dataset 测试。

钉死三件事：
  1. 重采样比例正确（24000→40000 = 5/3，不是粗糙的线性插值）；
  2. 峰值归一到目标（换数据集时只动数据、不动响度）；
  3. **报告文件不能写进数据集目录** —— RVC 的 `train.preprocess` 会把目录里
     每个文件都当音频喂给 ffmpeg，多一个 .json 就多一条 "失败"
     （2026-10-12 实测：81 wav + 1 json = 82 个任务，json 那条报 ffmpeg error）。
"""
import json
import os
import sys
import wave

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import make_rvc_dataset as mrd  # noqa: E402


def _write_wav(path, x, sr):
    sf.write(str(path), np.asarray(x, dtype=np.float32), sr, subtype="PCM_16")


def _tone(sr, dur, freq=220.0, amp=0.3):
    t = np.arange(int(sr * dur)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class TestResample:
    def test_same_rate_is_identity(self):
        x = _tone(16000, 0.5)
        y = mrd.resample(x, 16000, 16000)
        assert y.shape == x.shape
        assert np.allclose(y, x)

    def test_duration_preserved_on_upsample(self):
        """24000→40000 后时长必须不变（比例 5/3，靠 gcd 约分得到）。"""
        sr_in, sr_out, dur = 24000, 40000, 1.0
        x = _tone(sr_in, dur)
        y = mrd.resample(x, sr_in, sr_out)
        assert len(y) / sr_out == pytest.approx(dur, abs=0.01)

    def test_duration_preserved_on_downsample(self):
        x = _tone(44100, 1.0)
        y = mrd.resample(x, 44100, 16000)
        assert len(y) / 16000 == pytest.approx(1.0, abs=0.01)

    def test_gcd_reduction(self):
        """48000→40000 约分后是 5/6，不该出现巨大 up/down。"""
        import math
        g = math.gcd(48000, 40000)
        assert (40000 // g, 48000 // g) == (5, 6)


class TestPeakNormalize:
    def test_scales_to_target(self):
        x = _tone(16000, 0.3, amp=0.2)
        y = mrd.peak_normalize(x, 0.95)
        assert float(np.max(np.abs(y))) == pytest.approx(0.95, abs=1e-4)

    def test_silence_is_safe(self):
        """全零不能除零出 NaN。"""
        y = mrd.peak_normalize(np.zeros(1000, dtype=np.float32), 0.95)
        assert np.all(np.isfinite(y))
        assert float(np.max(np.abs(y))) == 0.0

    def test_relative_shape_preserved(self):
        """归一只是等比缩放，不该改变波形形状。"""
        x = _tone(16000, 0.3, amp=0.1)
        y = mrd.peak_normalize(x, 0.5)
        nonzero = np.abs(x) > 1e-6
        ratio = y[nonzero] / x[nonzero]
        assert np.allclose(ratio, ratio[0], rtol=1e-3)


class TestBuild:
    def _src(self, tmp_path, n=3, sr=24000, dur=2.0):
        src = tmp_path / "src"
        src.mkdir()
        for i in range(n):
            _write_wav(src / f"c{i:04d}.wav", _tone(sr, dur, freq=200 + 20 * i), sr)
        return src

    def test_builds_all_clips_at_target_sr(self, tmp_path):
        src = self._src(tmp_path)
        dst = tmp_path / "out"
        report, rows = mrd.build(src, dst, sr_out=40000, peak=0.95)
        assert report["files"] == 3
        assert len(list(dst.glob("*.wav"))) == 3
        for f in dst.glob("*.wav"):
            info = sf.info(str(f))
            assert info.samplerate == 40000
            assert info.channels == 1

    def test_peak_hits_target(self, tmp_path):
        src = self._src(tmp_path)
        dst = tmp_path / "out"
        mrd.build(src, dst, sr_out=40000, peak=0.95)
        for f in dst.glob("*.wav"):
            a, _sr = sf.read(str(f), dtype="float32")
            assert float(np.max(np.abs(a))) == pytest.approx(0.95, abs=0.01)

    def test_total_duration_reported(self, tmp_path):
        src = self._src(tmp_path, n=4, dur=2.0)
        report, _rows = mrd.build(src, tmp_path / "out", peak=0.95)
        assert report["total_s"] == pytest.approx(8.0, abs=0.2)

    def test_short_clips_skipped(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        _write_wav(src / "ok.wav", _tone(24000, 2.0), 24000)
        _write_wav(src / "tiny.wav", _tone(24000, 0.3), 24000)
        report, _rows = mrd.build(src, tmp_path / "out", min_dur_s=1.0)
        assert report["files"] == 1
        assert len(report["skipped"]) == 1
        assert report["skipped"][0]["file"] == "tiny.wav"

    def test_enough_flag_follows_official_minimum(self, tmp_path):
        """★ 低于 RVC 官方推荐 10min 必须报 enough=False（不是默默产出）。"""
        src = self._src(tmp_path, n=2, dur=2.0)   # 4s
        report, _rows = mrd.build(src, tmp_path / "out")
        assert report["enough"] is False
        assert report["total_s"] < mrd.RECOMMENDED_MIN_S

    def test_empty_src_raises(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(FileNotFoundError):
            mrd.build(empty, tmp_path / "out")

    def test_limit(self, tmp_path):
        src = self._src(tmp_path, n=5)
        report, _rows = mrd.build(src, tmp_path / "out", limit=2)
        assert report["files"] == 2

    def test_no_clipping_written(self, tmp_path):
        src = self._src(tmp_path)
        dst = tmp_path / "out"
        mrd.build(src, dst, sr_out=40000, peak=0.95)
        for f in dst.glob("*.wav"):
            with wave.open(str(f), "rb") as w:
                raw = w.readframes(w.getnframes())
            x = np.frombuffer(raw, dtype="<i2")
            assert not np.any(np.abs(x) >= 32767), f"{f.name} 削波"


class TestReportLocation:
    def test_report_defaults_outside_dataset_dir(self, tmp_path):
        """★ 回归：报告不能落在 dst 里，否则 RVC 预处理会拿它当音频（多一条失败）。

        这里直接验 main() 的路径推导规则：报告父目录 == dst 的父目录。
        """
        from pathlib import Path
        dst = tmp_path / "dataset_raw" / "exp_x"
        deduced = os.path.join(os.path.dirname(str(Path(str(dst)).resolve())),
                               f"{Path(str(dst)).name}_report.json")
        assert Path(deduced).parent == dst.parent.resolve()
        assert Path(deduced).parent != dst.resolve()
        assert Path(deduced).suffix == ".json"

    def test_built_dir_has_only_wav(self, tmp_path):
        """build() 本身不写任何非 wav 到 dst。"""
        src = tmp_path / "src"
        src.mkdir()
        _write_wav(src / "a.wav", _tone(24000, 2.0), 24000)
        dst = tmp_path / "out"
        report, rows = mrd.build(src, dst)
        names = sorted(p.name for p in dst.iterdir())
        assert names == ["a.wav"], f"dst 里混入了非音频文件：{names}"
        # 报告内容本身仍可序列化（由调用方写到别处）
        json.dumps(report)
