"""tools/check.py 的 `--ci-fidelity` 辅助函数单测。

这个模式的全部价值是"如实复刻 CI"，而它有两处会**静默**失效：

1. **版本号解析**（从 `ci.yml` 读 Python/node 版本）。解析器一旦失效会悄悄回落到
   默认值 —— 那就变成"拿 3.11 假装复刻 3.13"，结论仍然是绿的，最骗人的那种绿。
2. **依赖指纹**（决定要不要重装依赖）。指纹算错会导致"改了 requirements 却没重装"，
   等于放过了这个模式唯一的假想敌：本机 `.venv` 与 CI 之间那批没被声明的包
   （2026-09-13 的 Pillow/comtypes 就是这个形状，见 docs/犯错指南.md §3.9）。

对真实 `ci.yml` 的断言**只验"能解析出形如 X.Y 的东西"，不验具体数字** —— 否则以后
升 CI 版本会让这里误红一片，人就开始无视它了。
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def check():
    """按路径加载 tools/check.py（它是脚本不是包；导入期只有常量与函数定义）。"""
    spec = importlib.util.spec_from_file_location("tools_check", ROOT / "tools" / "check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tools_check"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------- 1. 版本号解析（静默失效风险最高的一处） ----------------

@pytest.mark.parametrize("text,expect", [
    ('python-version: "3.13"', "3.13"),
    ("python-version: '3.12'", "3.12"),
    ("python-version: 3.11", "3.11"),                    # 不带引号
    ('      python-version: "3.11"\n      cache: pip', "3.11"),   # 缩进 + 后随行
    ("runs-on: windows-latest\n  steps:\n", None),        # 没有该字段
    ("", None),
])
def test_ci_python_version_parses_shapes(check, text, expect):
    assert check._ci_python_version(text) == expect


@pytest.mark.parametrize("text,expect", [
    ('node-version: "22"', "22"),
    ("node-version: '20'", "20"),
    ('node-version: "22.14.0"', "22.14.0"),
    ("runs-on: ubuntu-latest", None),
])
def test_ci_node_version_parses_shapes(check, text, expect):
    assert check._ci_node_version(text) == expect


def test_real_ci_workflow_is_parseable(check):
    """真实 ci.yml 必须解析得出（防"改了格式、解析器默默变 None"）。"""
    text = check._ci_workflow_text()
    assert text, "读不到 .github/workflows/ci.yml"
    py = check._ci_python_version(text)
    node = check._ci_node_version(text)
    assert py and re.fullmatch(r"\d+\.\d+", py), f"python-version 解析异常：{py!r}"
    assert node and re.fullmatch(r"\d+(\.\d+)*", node), f"node-version 解析异常：{node!r}"


# ---------------- 2. 依赖指纹 ----------------

def test_deps_stamp_stable_for_same_input(check):
    assert check._deps_stamp(b"flask\n", "3.11") == check._deps_stamp(b"flask\n", "3.11")


def test_deps_stamp_changes_with_requirements(check):
    """requirements 变一个字节就要重装 —— 这是整个模式的关键判定。"""
    assert check._deps_stamp(b"flask\n", "3.11") != check._deps_stamp(b"flask\npillow\n", "3.11")


def test_deps_stamp_changes_with_python_version(check):
    assert check._deps_stamp(b"flask\n", "3.11") != check._deps_stamp(b"flask\n", "3.13")


def test_deps_stamp_shape(check):
    digest, _, version = check._deps_stamp(b"flask\n", "3.11").partition("|")
    assert len(digest) == 12 and all(c in "0123456789abcdef" for c in digest)
    assert version == "3.11"


# ---------------- 3. 解释器定位必须优雅退化 ----------------

def test_probe_python_returns_none_for_missing_executable(check):
    assert check._probe_python(["definitely-not-a-python-20260913"]) is None


def test_find_python_never_crashes_on_absent_version(check):
    """CI 钉了一个本机没有的版本时，必须回落到某个解释器而不是抛异常。"""
    cmd, version = check._find_python("99.99")
    assert cmd, "至少要返回一个可用的启动器"
    assert version is None or re.fullmatch(r"\d+\.\d+", version)


def test_venv_python_path_matches_platform(check):
    py = check._venv_python(Path("somevenv"))
    parts = py.parts[-2:]
    assert parts == (("Scripts", "python.exe") if os.name == "nt" else ("bin", "python"))


# ---------------- 4. 别把慢模式混进默认检查 ----------------

def test_ci_fidelity_is_not_a_default_step(check):
    """它是独立模式：跑几分钟且会建 venv，绝不能混进 pre-commit 的默认流程。"""
    assert "ci-fidelity" not in check.STEPS
    assert set(check.STEPS) == {"requires", "electron", "ruff", "pytest", "web"}
