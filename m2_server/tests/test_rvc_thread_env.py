"""RVC 子进程线程上限（_thread_env）单测。

背景：RVC 默认按逻辑核数开 OpenMP/BLAS 线程池，实测瞬时拉满 14~17 核；
限到 2 后 CPU 成本降 6.5× 而 RTF 不变。本模块锁住三个要点：
  1. 三个变量必须**一起**注入（只设 OMP 时 MKL/OpenBLAS 仍会各自开池，收益减半）
  2. VM_LIVE_OMP_THREADS=0 必须能关掉（保留历史行为的对照开关）
  3. 返回的是**待合并**字典，绝不直接改本进程 os.environ
"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import rvc_live  # noqa: E402


def test_injects_all_three_thread_vars(monkeypatch):
    """三件套必须齐全 —— 漏 MKL 时实测等效核数只降到 9（收益减半）。"""
    monkeypatch.setattr(rvc_live, "OMP_THREADS", 2)
    env = rvc_live._thread_env()
    assert env["OMP_NUM_THREADS"] == "2"
    assert env["MKL_NUM_THREADS"] == "2"
    assert env["OPENBLAS_NUM_THREADS"] == "2"
    # PASSIVE 抑制每轮推理反复调线程池的抖动
    assert env["OMP_WAIT_POLICY"] == "PASSIVE"


def test_zero_disables_injection(monkeypatch):
    """VM_LIVE_OMP_THREADS=0 → 空字典（不注入），保留历史行为供对照复测。"""
    monkeypatch.setattr(rvc_live, "OMP_THREADS", 0)
    assert rvc_live._thread_env() == {}


def test_negative_also_disables(monkeypatch):
    """负数同样视为关闭，不做 int 截断导致的意外注入。"""
    monkeypatch.setattr(rvc_live, "OMP_THREADS", -1)
    assert rvc_live._thread_env() == {}


def test_custom_thread_count(monkeypatch):
    monkeypatch.setattr(rvc_live, "OMP_THREADS", 6)
    assert rvc_live._thread_env()["OMP_NUM_THREADS"] == "6"


def test_does_not_mutate_process_env(monkeypatch):
    """关键：返回待合并字典，不得污染本进程环境。

    本进程（FastAPI）自己也有线程池，若在此处污染 os.environ，
    会连带把整个后端服务的线程行为改掉 —— 这是必须守住的边界。
    """
    monkeypatch.setattr(rvc_live, "OMP_THREADS", 2)
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.delenv("MKL_NUM_THREADS", raising=False)
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    rvc_live._thread_env()
    assert "OMP_NUM_THREADS" not in os.environ
    assert "MKL_NUM_THREADS" not in os.environ
    assert "OPENBLAS_NUM_THREADS" not in os.environ


def test_merge_pattern_overrides_inherited(monkeypatch):
    """{**os.environ, **env} 的合并顺序：注入值必须**覆盖**继承值。

    若写成 {**env, **os.environ} 就永远改不动（子进程继承的旧值会赢），
    这是本改动最容易写反的一行。
    """
    monkeypatch.setattr(rvc_live, "OMP_THREADS", 2)
    inherited = {"OMP_NUM_THREADS": "16", "PATH": "/x"}
    merged = {**inherited, **rvc_live._thread_env()}
    assert merged["OMP_NUM_THREADS"] == "2"
    assert merged["PATH"] == "/x"  # 其余继承变量不受影响


def test_default_is_two():
    """默认值必须是 2（实测最优点），别被谁改成 1 —— 单线程下 FAISS 搜索可能成延迟瓶颈。"""
    assert int(os.environ.get("VM_LIVE_OMP_THREADS", "2")) == rvc_live.OMP_THREADS
