"""tools/check.py 的 `--ci-fidelity` 辅助函数单测。

这个模式的全部价值是"如实复刻 CI"，而它有几处会**静默**失效：

1. **版本号解析**（从 `ci.yml` 读 Python/node 版本）。解析器一旦失效会悄悄回落到
   默认值 —— 那就变成"拿 3.11 假装复刻 3.13"，结论仍然是绿的，最骗人的那种绿。
2. **依赖指纹**（决定要不要重装依赖）。指纹算错会导致"改了 requirements 却没重装"，
   等于放过了这个模式唯一的假想敌：本机 `.venv` 与 CI 之间那批没被声明的包
   （2026-09-13 的 Pillow/comtypes 就是这个形状，见 docs/犯错指南.md §3.9）。
3. **裸 runner 环境**（§5）。这是 2026-09-13 CI 首跑红 13 条之后补的第二根轴：
   光复刻依赖集不够，"开发机有、runner 没有"的资源同样会让 CI 红。见该节注释。

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
    """它是独立模式：跑几分钟且会建 venv，绝不能混进 pre-commit 的默认流程。

    2026-09-14 起默认清单多了 `licenses`（第三方许可登记门禁）：它只要 0.05s 且纯本地，
    进 pre-commit 才拦得住"加依赖却没登记许可"那一次提交 —— 所以这里同步改，
    而不是把它排除在外。改这个集合时**两边都要动**，别只改一边做成假绿。
    """
    assert "ci-fidelity" not in check.STEPS
    assert set(check.STEPS) == {"licenses", "requires", "electron", "ruff", "pytest", "web"}


# ---------------- 5. 裸 runner 环境（"本机资源"那根轴，2026-09-13 补） ----------------
# 背景：CI 首跑红的 13 条**全部**是"开发机有、runner 没有"的资源，而当时只复刻了
# 依赖集这根轴。这段就是第二根轴，两处会静默失效，都要钉：
#   · 忘了设 VM_BARE_RUNNER → 探测照旧判"有"，用例在本机绿、在 CI 红
#   · 只设 VM_BARE_RUNNER 却不动 PATH → **绕过探测**直接调 ffmpeg 的用例照样绿，
#     而那恰恰是 2026-09-13 红掉的 9 条的形状

def test_bare_runner_env_sets_flag_and_unsets_ffmpeg_path(check, monkeypatch):
    """FFMPEG_PATH 必须用 None（= 从子进程环境删掉）表达，不能设成空串。

    设成空串的话 `common.find_ffmpeg()` 里 `os.environ.get("FFMPEG_PATH") or ""`
    仍然是假值、看起来等价 —— 但"删掉"和"设成空"对下游 `Path(cand).exists()`
    之类判定的语义不同，用 None 表达意图更明确，也更好断言。
    """
    monkeypatch.setenv("FFMPEG_PATH", r"C:\fake\ffmpeg.exe")
    extra, _ = check._bare_runner_env()
    assert extra["VM_BARE_RUNNER"] == "1"
    assert extra["FFMPEG_PATH"] is None


def test_bare_runner_env_strips_dirs_containing_ffmpeg(check, tmp_path, monkeypatch):
    """含 ffmpeg 的 PATH 目录必须被摘掉，其余目录原样保留。"""
    with_ffmpeg = tmp_path / "with-ffmpeg"
    plain = tmp_path / "plain"
    with_ffmpeg.mkdir()
    plain.mkdir()
    (with_ffmpeg / "ffmpeg.exe").write_bytes(b"")
    monkeypatch.setenv("PATH", os.pathsep.join([str(with_ffmpeg), str(plain)]))

    extra, dropped = check._bare_runner_env()
    parts = extra["PATH"].split(os.pathsep)
    assert dropped == [str(with_ffmpeg)]
    assert str(with_ffmpeg) not in parts
    assert str(plain) in parts


def test_bare_runner_env_recognises_both_executable_names(check, tmp_path, monkeypatch):
    """POSIX 上可执行文件没有 `.exe` 后缀，探测必须同时认 `ffmpeg`。"""
    d = tmp_path / "bin"
    d.mkdir()
    (d / "ffmpeg").write_bytes(b"")
    monkeypatch.setenv("PATH", str(d))
    _, dropped = check._bare_runner_env()
    assert dropped == [str(d)]


def test_bare_runner_env_ignores_empty_path_entries(check, monkeypatch):
    """PATH 里的空项（Windows 上常见，代表当前目录）不该被当成"含 ffmpeg 的目录"。"""
    monkeypatch.setenv("PATH", os.pathsep.join(["", "C:\\definitely-not-there-20260913", ""]))
    extra, dropped = check._bare_runner_env()
    assert dropped == []
    assert extra["PATH"] == "C:\\definitely-not-there-20260913"
