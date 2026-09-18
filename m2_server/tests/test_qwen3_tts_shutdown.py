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


# ---------------- netstat 解码：2026-09-18 实测故障（§2.33） ----------------

def test_pids_on_port_survives_none_stdout(monkeypatch):
    """netstat 解码失败（stdout 为 None）时不能抛异常，必须返回空列表。

    真实故障链（2026-09-18 实测）：中文 Windows 的 netstat 输出 **GBK** 表头
    （「活动连接」= ``BB EE``），而 `text=True` 按 locale 编码解码 ——
    在 UTF-8 模式（``PYTHONUTF8=1``；PEP 686 计划 3.15 起默认开启）下按 utf-8 解
    → reader 线程抛 UnicodeDecodeError（被 pytest 记成 warning、**测试照样绿**）
    → ``subprocess.run(...).stdout`` 变成 **None**
    → ``out.splitlines()`` 抛 AttributeError
    → 本函数返回空 → ``shutdown_worker()`` 回收不了遗留 worker（~4.8GB 显存泄漏）。

    这条守卫锁的就是最后一环：**stdout 为 None 也不能崩**。
    """
    class _R:
        stdout = None

    monkeypatch.setattr(qwen3_tts.subprocess, "run", lambda *a, **k: _R())
    assert qwen3_tts._pids_on_port() == []


def test_pids_on_port_parses_listening_rows(monkeypatch):
    """从 netstat 输出里挑出 LISTEN 目标端口的 PID（带中文 GBK 表头也不受影响）。"""
    sample = (
        "\r\n活动连接\r\n\r\n"
        "  协议  本地地址          外部地址        状态           PID\r\n"
        "  TCP    127.0.0.1:8001         0.0.0.0:0              LISTENING       4540\r\n"
        "  TCP    127.0.0.1:9999         0.0.0.0:0              LISTENING       1111\r\n"
        "  TCP    127.0.0.1:8001         127.0.0.1:55000        ESTABLISHED     2222\r\n"
    )

    class _R:
        stdout = sample

    monkeypatch.setattr(qwen3_tts.subprocess, "run", lambda *a, **k: _R())
    # 只认 LISTENING 行：9999 不是目标端口、2222 是 ESTABLISHED，都不该被收进来
    assert qwen3_tts._pids_on_port() == [4540]


_SYSTEM_CMDS = ("netstat", "powershell", "taskkill", "wmic", "nvidia-smi")


def test_system_command_calls_specify_encoding():
    """调用 Windows 系统命令的 subprocess **必须显式指定 encoding**。

    这类命令的输出编码是**控制台代码页**（中文 Windows = GBK），
    **不受 PYTHONUTF8 影响**；而 `text=True` 默认按
    ``locale.getpreferredencoding()`` 解码 —— UTF-8 模式下就是 utf-8，于是解不开。
    实测对照：cp936 环境 stdout 正常 136 行；``PYTHONUTF8=1`` 环境 stdout 变 None。

    这是一条**文本级**守卫（比行为测试脆弱），但它拦的是"新写一处系统命令调用
    又忘了 encoding" —— 那种改动不会有任何行为测试覆盖到（要等真出故障才发现）。
    只检查 ``text=True`` 的调用：bytes 模式不解码，本来就不会踩这个坑。
    """
    bad: list[str] = []
    for path in sorted(_ROOT.glob("*.py")):
        lines = path.read_text("utf-8").splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # 注释里提到命令名（如本条故障说明）不算
            if not any(c in line for c in _SYSTEM_CMDS):
                continue
            window = "\n".join(lines[max(0, i - 4): i + 5])
            if "subprocess." not in window:
                continue  # 不是在起子进程
            if "text=True" not in window:
                continue  # bytes 模式，不解码
            if "encoding=" not in window:
                bad.append(f"{path.name}:{i + 1}: {stripped}")
    assert not bad, (
        "以下调用 Windows 系统命令却没指定 encoding —— 在 UTF-8 模式下会解码失败、"
        "stdout 变 None，随后 .splitlines() 抛 AttributeError：\n  " + "\n  ".join(bad)
    )