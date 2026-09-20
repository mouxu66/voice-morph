"""F4 音频设备诊断还原看板逻辑单测（mock 掉 PowerShell / 进程探测）。"""

import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

# 音频设备看板逻辑已从 server.py 拆到 audio_api（server 只做 app 装配），
# monkeypatch 必须打在逻辑所在模块上才能生效。
import audio_api as server  # noqa: E402


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
    # B 类：扫描失败（None=无法确认）时保守 —— 不判定残留，绝不自动还原用户设备
    monkeypatch.setattr(server, "_any_voice_alive", lambda: None)
    assert server._audio_stale() is False


def test_backup_absent_never_stale(monkeypatch):
    """没有备份时无论探测结果如何都不是「残留」。"""
    monkeypatch.setattr(server, "_backup_exists", lambda: False)
    for alive in (True, False, None):
        monkeypatch.setattr(server, "_any_voice_alive", lambda alive=alive: alive)
        assert server._audio_stale() is False


def test_audio_inspection_does_not_import_optional_plugins(monkeypatch):
    """B 类：内核巡检不能再反向 import 可关插件（关掉/损坏时 500 连坐）。"""
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "rvc_live", None)
    monkeypatch.setitem(_sys.modules, "cascade", None)
    monkeypatch.setattr(server, "voice_proc_alive", lambda: False)
    monkeypatch.setattr(server, "_backup_exists", lambda: True)
    assert server._audio_stale() is True  # 全程未 import 任何插件


# ---------------- runtime.voice_proc_alive 三态与缓存 ----------------


def test_voice_proc_alive_states_and_cache(monkeypatch):
    """True=有 pid / False=空 / None=扫描异常；同 1s 内走缓存不再起 PowerShell。"""
    import runtime

    calls = {"n": 0}
    state = {"stdout": "", "exc": False}

    def fake_run(*a, **kw):
        calls["n"] += 1
        if state["exc"]:
            raise RuntimeError("scan fail")
        return SimpleNamespace(stdout=state["stdout"])

    def reset():
        runtime._VOICE_PID_CACHE.update(ts=None, alive=None)

    monkeypatch.setattr(runtime.subprocess, "run", fake_run)
    monkeypatch.setattr(runtime.time, "time", lambda: 200.0)
    try:
        reset()
        state.update(stdout="123\n456\n", exc=False)
        assert runtime.voice_proc_alive() is True
        assert runtime.voice_proc_alive() is True  # 1s 内命中缓存
        assert calls["n"] == 1

        reset()
        state.update(stdout="", exc=False)
        calls["n"] = 0
        assert runtime.voice_proc_alive() is False

        reset()
        state.update(exc=True)
        calls["n"] = 0
        assert runtime.voice_proc_alive() is None
        assert calls["n"] == 1
    finally:
        reset()  # 缓存残留会影响其它用例，恢复初态


def test_audit_once_no_stale_noop(monkeypatch):
    monkeypatch.setattr(server, "_audio_stale", lambda: False)
    called = []
    monkeypatch.setattr(server, "_run_audio_config", lambda a: called.append(a) or {"ok": True})
    assert server._audit_once() is None
    assert called == []  # 无残留不触发还原


def test_audit_once_restore_ok(monkeypatch):
    monkeypatch.setattr(server, "_audio_stale", lambda: True)
    calls = []
    monkeypatch.setattr(server, "_run_audio_config", lambda a: calls.append(a) or {"ok": True})
    ev = server._audit_once()
    assert ev["action"] == "restore"
    assert ev["result"] == "ok"
    assert calls == ["restore"]  # 成功后不再走 reset
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
    assert calls == ["restore", "reset"]  # restore 失败后兜底 reset


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
    assert len(server._AUDIO_AUDIT["auto_restored"]) == 20  # 只保留最近 20 条
