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


def test_history_marks_whether_file_still_exists(client, tmp_path, monkeypatch):
    """`available`：这条历史现在还能不能重发 / 试听。

    为什么必须有它 —— 产物是**会话级**的（退出即删；`session_out` 模块注释记着用户
    2026-09-25 的原话「不需要的退出直接就删掉」），所以历史里多数条目在退出应用后
    文件已经不在磁盘上（本机实测：20 条历史、文件命中 0 处）。
    没有这个字段时，界面上的「重发」对已清理的条目照样可点 —— 点了必然失败，
    是个**假承诺**。

    注意断言里既有 True 也有 False：用户点过「保存」的产物会落在 outputs 根，
    那些**仍然可用** —— "历史全都不能重发"是错的。
    """
    import config as cfg

    outs = tmp_path / "outs"
    outs.mkdir()
    (outs / "tts_here.wav").write_bytes(b"RIFF")  # 还在磁盘上的那条
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", outs)

    (tmp_path / "wechat_send_history.json").write_text(
        json.dumps(
            [
                {"wav": "tts_here.wav", "duration_s": 2.5, "ts": 1756600000},
                {"wav": "tts_gone.wav", "duration_s": 3.0, "ts": 1756600100},
            ],
            ensure_ascii=False,
        ),
        "utf-8",
    )

    items = client.get("/api/wechat/history").json()["items"]
    assert {it["wav"]: it["available"] for it in items} == {
        "tts_here.wav": True,
        "tts_gone.wav": False,
    }


def test_history_available_is_computed_not_persisted(client, tmp_path, monkeypatch):
    """`available` 必须**每次现算**，不能写回历史文件。

    可用性是**动态**的：文件随时可能被清理、用户随时可能点保存。存进 json 只会立刻过期
    —— 一条今天可用的记录，明天文件被清掉后仍会显示「可重发」。
    """
    import config as cfg

    outs = tmp_path / "outs"
    outs.mkdir()
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", outs)
    hist_path = tmp_path / "wechat_send_history.json"
    hist_path.write_text(
        json.dumps([{"wav": "tts_1.wav", "duration_s": 2.5, "ts": 1756600000}], ensure_ascii=False),
        "utf-8",
    )

    assert client.get("/api/wechat/history").json()["items"][0]["available"] is False

    # 文件出现了 → 同一个请求就该说 True（说明是现算的）
    (outs / "tts_1.wav").write_bytes(b"RIFF")
    assert client.get("/api/wechat/history").json()["items"][0]["available"] is True

    # 两次请求都不该把 available 落进历史文件
    assert "available" not in hist_path.read_text("utf-8")
