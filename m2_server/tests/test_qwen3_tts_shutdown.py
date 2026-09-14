# -*- coding: utf-8 -*-
"""Qwen3-TTS worker 生命周期（游戏档卸载路径）测试。

背景（2026-09-15）：切到「游戏低占用」档时要把语音合成 worker 杀卸载，
给游戏腾出 ~4.8GB 显存（模型文件本来就在盘里 tts_models/，杀进程即释放）。

覆盖两个关键语义：
  1. shutdown_worker 不只能杀本进程拉起的 _proc —— 服务重启后 _proc 为 None、
     worker 被 _ensure_worker 复用（孤儿），只杀 _proc 会漏掉它占住的显存；
     必须按命令行把端口 8001 上本项目残留 worker 一并清掉。
  2. 只认本项目 worker（命令行含 qwen3_tts_service.py），绝不误杀占用 8001
     的其他程序。
  worker_alive 以 /health 实况为准（复用场景下没有 _ready，靠健康探测）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import qwen3_tts  # noqa: E402


def test_worker_alive_uses_health(monkeypatch):
    """worker_alive 通 /health 才算：复用场景（无 _ready/_proc）也能正确反映。"""
    monkeypatch.setattr(qwen3_tts, "_health_ok", lambda: True)
    assert qwen3_tts.worker_alive() is True
    monkeypatch.setattr(qwen3_tts, "_health_ok", lambda: False)
    assert qwen3_tts.worker_alive() is False


def test_shutdown_worker_kills_orphan_on_port(monkeypatch):
    """服务重启后复用的孤儿 worker（_proc 为 None）：必须仍能被清掉。"""
    killed: list[int] = []
    monkeypatch.setattr(qwen3_tts, "_proc", None)
    monkeypatch.setattr(qwen3_tts, "_terminate_proc", lambda: None)
    monkeypatch.setattr(qwen3_tts, "_pids_on_port", lambda: [4242])
    monkeypatch.setattr(qwen3_tts, "_is_our_worker", lambda pid: True)
    monkeypatch.setattr(qwen3_tts, "_kill_pid", lambda pid: killed.append(pid))
    qwen3_tts.shutdown_worker()
    assert killed == [4242]
    assert qwen3_tts._ready is False


def test_shutdown_worker_does_not_kill_foreign_port_owner(monkeypatch):
    """端口被别的程序占用：按命令行认领，绝不误杀。"""
    killed: list[int] = []
    monkeypatch.setattr(qwen3_tts, "_proc", None)
    monkeypatch.setattr(qwen3_tts, "_terminate_proc", lambda: None)
    monkeypatch.setattr(qwen3_tts, "_pids_on_port", lambda: [9999])
    monkeypatch.setattr(qwen3_tts, "_is_our_worker", lambda pid: False)
    monkeypatch.setattr(qwen3_tts, "_kill_pid", lambda pid: killed.append(pid))
    qwen3_tts.shutdown_worker()
    assert killed == []