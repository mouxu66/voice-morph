"""A 类回归：跨插件模块级裸 import 的隔离（2026-09-20 清单门控阶段发现）。

问题：offline_vc / seed_vc / ab_chain 在模块级 `from rvc_live import ...`，
capture_api 在模块级 `from mine_api / pipeline_api import ...`。rvc_live、
mine_api、pipeline_api 都属于**可关插件**，后果有二：
    ① 关掉对应插件后（第 6 步）这些 import 依然把它拉进进程（连拉
       qwen3_tts / live_settings）——「关了就不 import」落空；
    ② 依赖模块一旦加载失败，三个互不相干的能力一起挂——步 1 防的
       「一个 import 失败拖垮一片」的变种。

修法：调用点延迟 import + try 兜底（失败按「未运行」处理）。
本文件锁两条契约：
    1. 干净子进程里 import 这些模块，不夹带加载 rvc_live / qwen3_tts /
       live_settings（capture_api 为 mine_api / pipeline_api）；
    2. 依赖模块损坏（语法错误）时，这些模块仍能各自独立 import。
另有进程内用例验证兜底语义（返回 False 而不是抛）。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

M2 = Path(__file__).resolve().parents[1]

# 模块 → 它不得在模块级拉进来的顶层模块（被关的插件必备隔离清单）
NO_DRAG = {
    "offline_vc": ("rvc_live", "qwen3_tts", "live_settings"),
    "seed_vc": ("rvc_live", "qwen3_tts", "live_settings", "cascade"),
    "ab_chain": ("rvc_live", "qwen3_tts", "live_settings"),
    "capture_api": ("mine_api", "pipeline_api"),
}

# 模块 → 允许损坏的依赖（损坏后本模块仍须能 import）
BROKEN_OK = {
    "offline_vc": ("rvc_live",),
    "seed_vc": ("rvc_live", "cascade"),
    "ab_chain": ("rvc_live",),
    "capture_api": ("mine_api", "pipeline_api"),
}


def _run_py(code: str, tmp_out: Path, extra_path: str = "") -> subprocess.CompletedProcess:
    """干净子进程里跑一段 Python。PYTHONPATH 让 stub（若给）优先于 m2_server。"""
    env = dict(os.environ)
    parts = [extra_path, str(M2)] if extra_path else [str(M2)]
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env["VM_OUTPUTS_DIR"] = str(tmp_out)  # 不碰真实 outputs/
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(M2),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _last_stdout_line(r: subprocess.CompletedProcess) -> str:
    assert r.returncode == 0, f"子进程失败\nstdout:\n{r.stdout[-500:]}\nstderr:\n{r.stderr[-800:]}"
    return r.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("module,forbidden", sorted(NO_DRAG.items()))
def test_import_does_not_drag_disabled_plugin_modules(module, forbidden, tmp_path):
    """契约 1：import 业务模块不得夹带加载可关插件的模块。"""
    code = (
        f"import sys, json, {module}; "
        f"print(json.dumps([m for m in {list(forbidden)!r} if m in sys.modules]))"
    )
    loaded = json.loads(_last_stdout_line(_run_py(code, tmp_path)))
    assert loaded == [], f"import {module} 夹带加载了 {loaded}"


@pytest.mark.parametrize("module,broken", sorted(BROKEN_OK.items()))
def test_import_survives_broken_dependency(module, broken, tmp_path):
    """契约 2：依赖模块坏掉（语法错误）也不拖垮本模块的 import。"""
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in broken:
        (stubs / f"{name}.py").write_text("def broken(:\n", encoding="utf-8")  # 语法错误
    r = _run_py(f"import {module}; print('OK')", tmp_path, extra_path=str(stubs))
    assert _last_stdout_line(r) == "OK"


# ---------------- 进程内：兜底语义（返回 False 而不是抛） ----------------


def _freeze(monkeypatch, name: str):
    """sys.modules 里塞 None → 下一次 import 该模块必然 ImportError（无需真删模块）。"""
    monkeypatch.setitem(sys.modules, name, None)


def test_live_running_returns_false_when_rvc_live_unavailable(monkeypatch):
    import ab_chain
    import offline_vc
    import seed_vc

    _freeze(monkeypatch, "rvc_live")
    assert offline_vc._live_running() is False
    assert seed_vc._live_running() is False
    assert ab_chain._live_running() is False


def test_seedvc_cascade_returns_false_when_cascade_unavailable(monkeypatch):
    import seed_vc

    _freeze(monkeypatch, "cascade")
    assert seed_vc._cascade_running() is False


def test_live_running_delegates_when_rvc_live_available(monkeypatch):
    import offline_vc
    import rvc_live

    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: True)
    assert offline_vc._live_running() is True
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: False)
    assert offline_vc._live_running() is False


def test_capture_worker_survives_unavailable_pipeline_and_mine(monkeypatch):
    import capture_api

    _freeze(monkeypatch, "mine_api")
    _freeze(monkeypatch, "pipeline_api")
    capture_api._capture_auto_worker([])  # 不抛 = 通过（后台流程记录即止）