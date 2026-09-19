"""rvc_common 进程枚举/强杀的失败路径回归测试。

背景（2026-09-11 代码审查发现）：`rvc_common.py` 的 `_find_pids_by_cmdline`
和 `_kill_pids` 在 except 分支里调用 `logger`，但模块内从未定义
`logger = logging.getLogger(__name__)`（只有一句没用的 `import logging`）。
即"文档写着失败返回 [] 并记日志"，实际一旦 PowerShell 枚举失败就会抛
NameError —— 异常处理器自己炸掉，契约失效。

本文件锁死这两条失败路径：**不抛异常、返回安全值**。
"""

from __future__ import annotations

import subprocess

import rvc_common


def test_find_pids_returns_empty_when_enumeration_fails(monkeypatch):
    """枚举进程抛异常时必须返回 []（而不是 NameError）。"""

    def boom(*a, **kw):
        raise OSError("powershell 起不来")

    monkeypatch.setattr(rvc_common.subprocess, "run", boom)
    assert rvc_common._find_pids_by_cmdline("cascade_stream") == []


def test_find_pids_parses_pids_from_stdout(monkeypatch):
    """正常路径：只取纯数字行，忽略报错文本。"""

    class _R:
        stdout = "1234\r\n\r\n6789\nnot a pid\n"

    monkeypatch.setattr(rvc_common.subprocess, "run", lambda *a, **kw: _R())
    assert rvc_common._find_pids_by_cmdline("cascade_stream") == [1234, 6789]


def test_kill_pids_survives_taskkill_failure(monkeypatch):
    """强杀单个进程失败只记日志，不能中断后续进程（也不能 NameError）。"""
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        if cmd[2] == "111":
            raise subprocess.TimeoutExpired(cmd, 30)
        return None

    monkeypatch.setattr(rvc_common.subprocess, "run", fake_run)
    rvc_common._kill_pids([111, 222], label="test")  # 不应抛异常
    assert len(calls) == 2, "第一个失败后仍应继续杀剩下的进程"
