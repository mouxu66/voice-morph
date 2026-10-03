"""common.py 公共工具单测（音色 ID 校验 / 上传限制 / 音色档案读取）。"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import config as cfg  # noqa: E402
from common import (  # noqa: E402
    MAX_UPLOAD_BYTES,
    is_valid_voice_id,
    selected_voice,
    voice_ref,
)
from fastapi import HTTPException  # noqa: E402

# ---------------- is_valid_voice_id ----------------


def test_valid_voice_ids():
    for vid in ("abc", "ABC123", "a_b-c", "meituan_rat"):
        assert is_valid_voice_id(vid) is True


def test_invalid_voice_ids():
    for vid in (
        "",  # 空串
        "../etc",  # 路径穿越
        "a/b",  # 斜杠
        "a\\b",  # 反斜杠
        "a b",  # 空格
        "中文",  # 非 ASCII
        "a.b",  # 点（文件名分隔符）
        "..",
        "a?b",
        "a*b",
    ):
        assert is_valid_voice_id(vid) is False, f"{vid!r} 应被拒绝"


def test_upload_limit_sane():
    # 500MB 上限，且为合理数量级
    assert MAX_UPLOAD_BYTES > 0
    assert MAX_UPLOAD_BYTES >= 100 * 1024 * 1024


# ---------------- voice_ref / selected_voice（用 monkeypatch 隔离路径） ----------------


def _fake_voicebank(tmp_path):
    """在 tmp 下搭一个最小 voicebank 目录，返回其路径。"""
    vb = tmp_path / "voicebank"
    (vb / "abc").mkdir(parents=True)
    (vb / "abc" / "reference.wav").write_bytes(b"RIFF")
    (vb / "abc" / "ref_text.txt").write_text("  你好世界  ", encoding="utf-8")
    return vb


def test_voice_ref_returns_audio_and_text(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MEDIA_DIR", _fake_voicebank(tmp_path).parent)
    ref, text = voice_ref("abc")
    assert ref.name == "reference.wav"
    assert text == "你好世界"  # strip 后返回


def test_voice_ref_invalid_id_raises_400(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MEDIA_DIR", _fake_voicebank(tmp_path).parent)
    with pytest.raises(HTTPException) as e:
        voice_ref("../evil")
    assert e.value.status_code == 400


def test_voice_ref_missing_voice_raises_404(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MEDIA_DIR", _fake_voicebank(tmp_path).parent)
    with pytest.raises(HTTPException) as e:
        voice_ref("nope")
    assert e.value.status_code == 404


def test_selected_voice_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MEDIA_DIR", _fake_voicebank(tmp_path).parent)
    assert selected_voice() == ""


def test_selected_voice_reads_json(tmp_path, monkeypatch):
    # 注意：_fake_voicebank 会建出 abc/reference.wav，故这是「音色确实存在」的选中态
    vb = _fake_voicebank(tmp_path)
    (vb / "selected_voice.json").write_text(json.dumps({"voice_id": "abc"}), encoding="utf-8")
    monkeypatch.setattr(cfg, "MEDIA_DIR", vb.parent)
    assert selected_voice() == "abc"


def test_selected_voice_corrupt_json_returns_empty(tmp_path, monkeypatch):
    vb = _fake_voicebank(tmp_path)
    (vb / "selected_voice.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cfg, "MEDIA_DIR", vb.parent)
    assert selected_voice() == ""


def test_selected_voice_dangling_id_returns_empty(tmp_path, monkeypatch):
    """★ JSON 里有值、但该音色已被删掉（目录不存在）→ 按未选中处理。

    悬空 id 会被七处端点当默认音色用（tts / audiobook / wechat_voice /
    voice_phrases / capture / openai_compat / rvc_dataset），原样返回等于让
    这些入口一起 404。此处把期待值钉在「返回空」。
    """
    vb = _fake_voicebank(tmp_path)
    (vb / "selected_voice.json").write_text(json.dumps({"voice_id": "ghost"}), encoding="utf-8")
    monkeypatch.setattr(cfg, "MEDIA_DIR", vb.parent)
    assert selected_voice() == ""


def test_selected_voice_missing_reference_returns_empty(tmp_path, monkeypatch):
    """音色目录在、但 reference.wav 缺（半删除 / 手工建目录）→ 也按未选中处理。"""
    vb = _fake_voicebank(tmp_path)
    (vb / "hollow").mkdir()
    (vb / "selected_voice.json").write_text(json.dumps({"voice_id": "hollow"}), encoding="utf-8")
    monkeypatch.setattr(cfg, "MEDIA_DIR", vb.parent)
    assert selected_voice() == ""


def test_selected_voice_rejects_traversal_id(tmp_path, monkeypatch):
    """JSON 被手改成路径穿越 id → 返回空，不拼出 MEDIA_DIR 之外的路径。"""
    vb = _fake_voicebank(tmp_path)
    (vb / "selected_voice.json").write_text(json.dumps({"voice_id": "../abc"}), encoding="utf-8")
    monkeypatch.setattr(cfg, "MEDIA_DIR", vb.parent)
    assert selected_voice() == ""
