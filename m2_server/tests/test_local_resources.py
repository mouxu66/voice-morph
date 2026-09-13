"""本机资源探测（`m2_server/conftest.py` 顶部那一段）的单测。

为什么这些用例值得存在：探测失效是**静默**的，而且两个方向都有害 ——

  · 把"没有 ffmpeg"判成"有" → 不报错，只是回落到裸字符串 `"ffmpeg"`，
    最后在 subprocess 里变成 `[WinError 2] The system cannot find the file
    specified`。2026-09-13 CI 上那 9 条红就是这么来的，报错完全看不出真因。
  · 把"有"判成"没有" → 一片用例悄悄跳过，覆盖率没了也没人发现。

所以两个方向都要钉住。另见 `tools/check.py` 的 `_bare_runner_env()` ——
它负责把资源**真摘掉**（PATH 层面），本文件只管探测口径。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import conftest


def test_bare_runner_flag_parsing(monkeypatch):
    """`VM_BARE_RUNNER` 的取值口径：空串/0 为关，其余为开。"""
    monkeypatch.delenv("VM_BARE_RUNNER", raising=False)
    assert conftest.bare_runner() is False
    for off in ("", "0"):
        monkeypatch.setenv("VM_BARE_RUNNER", off)
        assert conftest.bare_runner() is False, f"{off!r} 应判为关"
    for on in ("1", "true", "yes"):
        monkeypatch.setenv("VM_BARE_RUNNER", on)
        assert conftest.bare_runner() is True, f"{on!r} 应判为开"


def test_ffmpeg_path_returns_runnable_path(monkeypatch):
    """本机有 ffmpeg 时必须返回**真能跑起来**的路径，而不是裸字符串。

    这条就是上面那个"静默失效"的正面钉子：返回 `"ffmpeg"` 也算"非空"，
    但它是不是可执行必须单独确认（`Path.exists() or shutil.which()`）。
    """
    monkeypatch.delenv("VM_BARE_RUNNER", raising=False)
    exe = conftest.ffmpeg_path()
    assert Path(exe).exists() or shutil.which(exe), f"返回了不可执行的路径：{exe!r}"


def test_ffmpeg_path_skips_on_local_machine(monkeypatch):
    """本机缺资源 → skip，且提示里要写清怎么装（而不是 WinError 2）。"""
    monkeypatch.setenv("VM_BARE_RUNNER", "1")
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(pytest.skip.Exception) as ei:
        conftest.ffmpeg_path()
    msg = str(ei.value)
    assert "ffmpeg" in msg, msg
    assert "winget" in msg or "FFMPEG_PATH" in msg, f"缺少安装提示：{msg}"


def test_ffmpeg_path_fails_on_ci(monkeypatch):
    """CI 上缺资源 → fail。

    刻意的非对称：本机缺是"你没装"，跳过即可；CI 缺说明 workflow 的环境准备
    步骤没生效，**不能**用 skip 把它掩盖成绿色。
    """
    monkeypatch.setenv("VM_BARE_RUNNER", "1")
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception) as ei:
        conftest.ffmpeg_path()
    assert "CI" in str(ei.value), str(ei.value)


def test_missing_local_marks_ci_by_common_flag_values(monkeypatch):
    """`CI` 的判定要认常见写法（GitHub Actions 给的是 "true"）。"""
    for val in ("1", "true", "TRUE", "yes"):
        monkeypatch.setenv("CI", val)
        assert conftest._on_ci() is True, val
    for val in ("", "0", "false"):
        monkeypatch.setenv("CI", val)
        assert conftest._on_ci() is False, val
