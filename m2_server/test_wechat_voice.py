"""wechat_voice 发送历史回归测试（无 GPU / 无微信依赖，纯逻辑层）。"""

import json
from pathlib import Path

import pytest
import wechat_voice
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离的历史文件 + TestClient（不碰真实 outputs/）。"""
    monkeypatch.setattr(wechat_voice, "HISTORY_FILE", tmp_path / "wechat_send_history.json")
    import server

    return TestClient(server.app)


def test_history_empty(client):
    r = client.get("/api/wechat/history")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "items": []}


def test_append_history_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(wechat_voice, "HISTORY_FILE", tmp_path / "h.json")
    ok = wechat_voice._append_history(Path("tts_001.wav"), 3.2)
    assert ok
    ok = wechat_voice._append_history(Path("tts_002.wav"), 5.0)
    data = json.loads((tmp_path / "h.json").read_text("utf-8"))
    assert ok and len(data) == 2
    # 599b1ac 起 _append_history 记录带 outcome 字段
    assert data[-1] == {
        "wav": "tts_002.wav",
        "duration_s": 5.0,
        "ts": data[-1]["ts"],
        "outcome": "ok",
    }


def test_append_history_caps_at_20(tmp_path, monkeypatch):
    monkeypatch.setattr(wechat_voice, "HISTORY_FILE", tmp_path / "h.json")
    for i in range(25):
        wechat_voice._append_history(Path(f"tts_{i:03d}.wav"), 1.0 + i)
    data = json.loads((tmp_path / "h.json").read_text("utf-8"))
    assert len(data) == wechat_voice.HISTORY_MAX == 20
    assert data[0]["wav"] == "tts_005.wav"  # 最老的 5 条被截掉
    assert data[-1]["wav"] == "tts_024.wav"


def test_history_api_returns_records(client, tmp_path):
    (tmp_path / "wechat_send_history.json").write_text(
        json.dumps([{"wav": "tts_1.wav", "duration_s": 2.5, "ts": 1756600000}], ensure_ascii=False),
        "utf-8",
    )
    r = client.get("/api/wechat/history")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["items"][0]["wav"] == "tts_1.wav"


def test_history_api_tolerates_corrupt_file(client, tmp_path):
    (tmp_path / "wechat_send_history.json").write_text("not-json{{", "utf-8")
    r = client.get("/api/wechat/history")
    assert r.status_code == 200
    assert r.json()["items"] == []
