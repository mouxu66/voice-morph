"""托管接口：loopback 护栏、授权闸门、配置校验。

接口测试全部从 `TestClient` 打进去 —— 只在函数层调用过的守卫等于没有
（`docs/犯错档案-工程.md` §8.43 的原话）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import autopilot_api as api  # noqa: E402
import chat_autopilot as ap  # noqa: E402


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(api.router)
    return app


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "CONFIG_FILE", tmp_path / "autopilot.json")
    monkeypatch.setattr(ap, "LOG_FILE", tmp_path / "log.jsonl")
    ap.reset_engine_for_test()
    yield TestClient(_app(), client=("127.0.0.1", 52001), raise_server_exceptions=False)
    ap.reset_engine_for_test()


@pytest.fixture()
def lan_client(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "CONFIG_FILE", tmp_path / "autopilot.json")
    monkeypatch.setattr(ap, "LOG_FILE", tmp_path / "log.jsonl")
    ap.reset_engine_for_test()
    yield TestClient(_app(), client=("192.168.1.50", 51234), raise_server_exceptions=False)
    ap.reset_engine_for_test()


def test_lan_is_forbidden(lan_client):
    """这条最要紧：局域网里的任何人都不能替我发微信。"""
    assert lan_client.get("/api/autopilot/state").status_code == 403


def test_state_returns_defaults(client):
    r = client.get("/api/autopilot/state")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["status"] == "off"
    assert body["config"]["whitelist"] == []
    assert body["config"]["question_only"] is True


def test_config_rejects_bad_values(client):
    r = client.put("/api/autopilot/config", json={"whitelist": "117"})
    assert r.status_code == 400
    assert "whitelist" in r.json()["detail"]


def test_config_saves_when_stopped(client):
    r = client.put("/api/autopilot/config", json={"whitelist": ["117"], "poll_s": 30})
    assert r.status_code == 200
    assert r.json()["config"]["whitelist"] == ["117"]
    body = client.get("/api/autopilot/state").json()
    assert body["config"]["poll_s"] == 30


def test_start_without_consent_is_forbidden(client):
    client.put("/api/autopilot/config", json={"whitelist": ["117"]})
    r = client.post("/api/autopilot/start", json={"consent": False})
    assert r.status_code == 403
    assert "授权" in r.json()["detail"]


def test_start_with_empty_whitelist_is_rejected(client):
    r = client.post("/api/autopilot/start", json={"consent": True})
    assert r.status_code == 400
    assert "白名单" in r.json()["detail"]


def test_start_pause_resume_stop(client, monkeypatch):
    client.put("/api/autopilot/config", json={"whitelist": ["117"]})
    eng = ap.engine()
    # 引擎线程用假依赖：只要微信「没开」就直接返回，不碰任何 Windows API
    eng._ops = {
        "wechat_alive": lambda: (False, "微信没开"),
        "read_chat": lambda limit=20: {"ok": False, "error": "x"},
        "now": lambda: 1000.0,
    }
    assert client.post("/api/autopilot/start", json={"consent": True}).status_code == 200
    assert client.get("/api/autopilot/state").json()["status"] in ("running", "paused")
    assert client.post("/api/autopilot/pause").json()["status"] == "paused"
    assert client.post("/api/autopilot/resume").json()["status"] == "running"
    assert client.post("/api/autopilot/stop").json()["status"] == "off"


def test_config_locked_while_running(client):
    client.put("/api/autopilot/config", json={"whitelist": ["117"]})
    eng = ap.engine()
    eng._ops = {"wechat_alive": lambda: (False, "微信没开"), "now": lambda: 1000.0}
    client.post("/api/autopilot/start", json={"consent": True})
    r = client.put("/api/autopilot/config", json={"whitelist": ["妈妈"]})
    assert r.status_code == 409
    client.post("/api/autopilot/stop")


def test_test_send_rejects_empty_and_long(client):
    assert client.post("/api/autopilot/test-send", json={"text": "  "}).status_code == 400
    assert client.post("/api/autopilot/test-send", json={"text": "字" * 41}).status_code == 400


def test_test_send_reports_select_failure(client, monkeypatch):
    import wechat_text

    monkeypatch.setattr(wechat_text, "selected_chat", lambda: "117")
    monkeypatch.setattr(wechat_text, "select_session", lambda title, timeout=1.5: {"ok": False, "chat": "", "error": "找不到"})
    r = client.post("/api/autopilot/test-send", json={"text": "测试"})
    assert r.status_code == 409
    assert "文件传输助手" in r.json()["detail"]


def test_sessions_handles_closed_wechat(client, monkeypatch):
    import wechat_text

    monkeypatch.setattr(wechat_text, "scan_sessions", lambda limit=20: {"ok": False, "sessions": [], "error": "微信没开"})
    r = client.get("/api/autopilot/sessions")
    assert r.status_code == 200
    assert r.json()["ok"] is False
