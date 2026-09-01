"""F4 音频设备诊断还原看板逻辑单测（mock 掉 PowerShell / 进程探测）。"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import server  # noqa: E402


@pytest.fixture(autouse=True)
def reset_audit_state():
    """每个用例前清空看板状态，避免互相污染。"""
    server._AUDIO_AUDIT["auto_restored"] = []
    server._AUDIO_AUDIT["last_error"] = ""
    yield


def test_backup_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_AUDIO_BACKUP", tmp_path / "rvc_audio_backup.txt")
    assert server._backup_exists() is False
    (tmp_path / "rvc_audio_backup.txt").write_bytes(b"x")
    assert server._backup_exists() is True


def test_audio_stale_logic(monkeypatch):
    monkeypatch.setattr(server, "_backup_exists", lambda: True)
    monkeypatch.setattr(server, "_any_voice_alive", lambda: False)
    assert server._audio_stale() is True
    monkeypatch.setattr(server, "_any_voice_alive", lambda: True)
    assert server._audio_stale() is False


def test_audit_once_no_stale_noop(monkeypatch):
    monkeypatch.setattr(server, "_audio_stale", lambda: False)
    called = []
    monkeypatch.setattr(server, "_run_audio_config", lambda a: called.append(a) or {"ok": True})
    assert server._audit_once() is None
    assert called == []                      # 无残留不触发还原


def test_audit_once_restore_ok(monkeypatch):
    monkeypatch.setattr(server, "_audio_stale", lambda: True)
    calls = []
    monkeypatch.setattr(server, "_run_audio_config", lambda a: calls.append(a) or {"ok": True})
    ev = server._audit_once()
    assert ev["action"] == "restore"
    assert ev["result"] == "ok"
    assert calls == ["restore"]              # 成功后不再走 reset
    assert server._AUDIO_AUDIT["last_error"] == ""


def test_audit_once_restore_fail_then_reset(monkeypatch):
    monkeypatch.setattr(server, "_audio_stale", lambda: True)
    calls = []

    def fake(action):
        calls.append(action)
        return {"ok": True} if action == "reset" else {"ok": False, "error": "restore 失败"}

    monkeypatch.setattr(server, "_run_audio_config", fake)
    ev = server._audit_once()
    assert ev["action"] == "reset"
    assert ev["result"] == "ok"
    assert calls == ["restore", "reset"]     # restore 失败后兜底 reset


def test_audit_once_all_fail(monkeypatch):
    monkeypatch.setattr(server, "_audio_stale", lambda: True)
    monkeypatch.setattr(server, "_run_audio_config", lambda a: {"ok": False, "error": "boom"})
    ev = server._audit_once()
    assert ev["result"] == "fail"
    assert server._AUDIO_AUDIT["last_error"] == "boom"


def test_audit_history_capped(monkeypatch):
    monkeypatch.setattr(server, "_audio_stale", lambda: True)
    monkeypatch.setattr(server, "_run_audio_config", lambda a: {"ok": True})
    for _ in range(25):
        server._audit_once()
    assert len(server._AUDIO_AUDIT["auto_restored"]) == 20   # 只保留最近 20 条
