# -*- coding: utf-8 -*-
"""GET /api/voices 音色清单回归测试（临时 voicebank 结构，不碰真实音色库）。"""
import soundfile as sf

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """VOICEBANK / RVC_ROOT 全部指向临时目录，与真实环境完全隔离。"""
    import config as cfg
    import server
    import voices_api

    vb = tmp_path / "voicebank"
    vb.mkdir()
    # 音色清单逻辑在 voices_api（server.py 只做 app 装配），patch 必须落在逻辑模块上
    monkeypatch.setattr(voices_api, "VOICEBANK", vb)
    monkeypatch.setattr(cfg, "RVC_ROOT", tmp_path / "rvc")   # 不存在 -> 跳过 RVC 实验扫描
    return TestClient(server.app), vb


def _make_ref(vb, voice_id, display=None):
    d = vb / voice_id
    d.mkdir(parents=True)
    sf.write(str(d / "reference.wav"), [0.0] * 2205, 22050, subtype="PCM_16")  # 0.1s 静音
    if display:
        (d / "meta.json").write_text(
            f'{{"display_name": "{display}"}}', encoding="utf-8")


def test_voices_empty(client):
    c, _ = client
    r = c.get("/api/voices")
    assert r.status_code == 200
    assert r.json() == {"voices": []}


def test_voices_lists_voicebank_entry(client):
    c, vb = client
    _make_ref(vb, "merg_004", "袋鼠骑士")
    r = c.get("/api/voices")
    assert r.status_code == 200
    voices = r.json()["voices"]
    assert len(voices) == 1
    v = voices[0]
    assert v["id"] == "merg_004"
    assert v["display_name"] == "袋鼠骑士"
    assert v["has_reference"] is True
    assert "duration_s" in v


def test_voices_skips_dir_without_reference(client):
    c, vb = client
    _make_ref(vb, "has_ref")
    (vb / "no_ref").mkdir()          # 无 reference.wav 的目录不算音色
    r = c.get("/api/voices")
    voices = r.json()["voices"]
    assert [v["id"] for v in voices] == ["has_ref"]


def test_voices_display_name_falls_back_to_id(client):
    c, vb = client
    _make_ref(vb, "plain_id")        # 无 meta.json
    v = c.get("/api/voices").json()["voices"][0]
    assert v["display_name"] == "plain_id"
