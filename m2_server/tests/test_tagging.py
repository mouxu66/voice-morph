"""F2 素材自动打标单测（mock 掉 ffmpeg / worker，只测分析逻辑）。"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np
import pytest  # noqa: E402
import soundfile as sf  # noqa: E402

import tagging  # noqa: E402


def _write_wav(path, samples):
    sf.write(str(path), samples, 16000, subtype="PCM_16")


def test_analyze_full_speech():
    """带停顿的语音（前有声后静音）→ speech_ratio 中等，has_bgm=False。"""
    t = np.arange(16000, dtype=np.float32) / 16000
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    # 前 0.6s 有声 + 后 0.4s 静音 → 有停顿节奏，非连续音乐
    speech = np.concatenate([tone[: 9600], np.zeros(6400, dtype=np.float32)])
    wav = Path(tagging.__file__).parent / "_test_tone.wav"
    try:
        _write_wav(wav, speech)
        m = tagging._analyze(wav)
        assert m["duration_s"] == pytest.approx(1.0, abs=0.05)
        assert 0.4 < m["speech_ratio"] < 0.8
        assert m["has_bgm"] is False
        assert m["loudness_dbfs"] > -20
    finally:
        wav.unlink(missing_ok=True)


def test_analyze_silence():
    """整段静音 → speech_ratio=0，loudness 极低，has_bgm=False（不是音乐）。"""
    silence = np.zeros(16000, dtype=np.float32)
    wav = Path(tagging.__file__).parent / "_test_silence.wav"
    try:
        _write_wav(wav, silence)
        m = tagging._analyze(wav)
        assert m["speech_ratio"] == 0.0
        assert m["loudness_dbfs"] < -50
        assert m["has_bgm"] is False
    finally:
        wav.unlink(missing_ok=True)


def test_analyze_continuous_low_speech_is_bgm():
    """连续无停顿的均匀信号（模拟纯音乐/单音）→ 疑似 BGM。"""
    t = np.arange(16000 * 3, dtype=np.float32) / 16000
    # 连续正弦、无停顿 → speech_ratio≈1 且帧响度过分均匀（变异系数小）
    music = (0.05 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
    wav = Path(tagging.__file__).parent / "_test_music.wav"
    try:
        _write_wav(wav, music)
        m = tagging._analyze(wav)
        assert m["speech_ratio"] > 0.9
        assert m["has_bgm"] is True
    finally:
        wav.unlink(missing_ok=True)


def test_detect_lang_zh(monkeypatch):
    class _Resp:
        pass
    monkeypatch.setattr(tagging, "_detect_lang", lambda wav: "zh")
    assert tagging._detect_lang(Path("x.wav")) == "zh"


def test_tag_video_ffmpeg_fail(tmp_path, monkeypatch):
    """ffmpeg 失败 → 返回 tagging=False + tag_error，不抛异常。"""
    monkeypatch.setattr(tagging, "_extract_audio", lambda v, o: False)
    fake = tmp_path / "a.mp4"
    fake.write_bytes(b"x")
    m = tagging.tag_video(fake, tmp_dir=tmp_path)
    assert m["tagging"] is False
    assert "ffmpeg" in m.get("tag_error", "")


def test_tag_video_success(tmp_path, monkeypatch):
    """打标成功：分析 + 语言（worker mock）合入 meta。"""
    monkeypatch.setattr(tagging, "_extract_audio", lambda v, o: True)
    monkeypatch.setattr(tagging, "_analyze", lambda wav: {
        "duration_s": 2.0, "loudness_dbfs": -10.0, "speech_ratio": 0.9, "has_bgm": False})
    monkeypatch.setattr(tagging, "_detect_lang", lambda wav: "zh")
    fake = tmp_path / "a.mp4"
    fake.write_bytes(b"x")
    m = tagging.tag_video(fake, tmp_dir=tmp_path)
    assert m["tagging"] is False          # 已完成
    assert m["lang"] == "zh"
    assert m["duration_s"] == 2.0
    assert "tag_error" not in m


def test_tag_video_analyze_error(tmp_path, monkeypatch):
    """分析抛异常 → 捕获为 tag_error，不崩溃。"""
    monkeypatch.setattr(tagging, "_extract_audio", lambda v, o: True)
    def boom(wav):
        raise RuntimeError("boom")
    monkeypatch.setattr(tagging, "_analyze", boom)
    fake = tmp_path / "a.mp4"
    fake.write_bytes(b"x")
    m = tagging.tag_video(fake, tmp_dir=tmp_path)
    assert m["tagging"] is False
    assert "boom" in m.get("tag_error", "")
