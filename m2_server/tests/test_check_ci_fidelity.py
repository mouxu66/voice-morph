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


@pytest.mark.parametrize(
    "text,expect",
    [
        ('python-version: "3.13"', "3.13"),
        ("python-version: '3.12'", "3.12"),
        ("python-version: 3.11", "3.11"),  # 不带引号
        ('      python-version: "3.11"\n      cache: pip', "3.11"),  # 缩进 + 后随行
        ("runs-on: windows-latest\n  steps:\n", None),  # 没有该字段
        ("", None),
    ],
)
def test_ci_python_version_parses_shapes(check, text, expect):
    assert check._ci_python_version(text) == expect


@pytest.mark.parametrize(
    "text,expect",
    [
        ('node-version: "22"', "22"),
        ("node-version: '20'", "20"),
        ('node-version: "22.14.0"', "22.14.0"),
        ("runs-on: ubuntu-latest", None),
    ],
)
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
    而不是把它排除在外。同日又多了 `nodetest` 与 `ps1lint`（前者复刻"CI 不装 npm 依赖"，
    后者体检 scripts/*.ps1）；两者都不进 --fast，由 pre-push / CI 兜底。
    改这个集合时**两边都要动**，别只改一边做成假绿。

    2026-09-21 又多了 `gate` 与 `ownership`，两者都进 --fast：
    * `gate` = `audit_endpoint_ownership.py --check`（约 0.4s）：核心路由页裸渲染可关
      组件 → 关掉插件后用户点出 404。缺口藏在共享组件调用链里，整页自己不调一个 API，
      **人肉 review 追不住**（09-21 实际漏过一次）。
    * `ownership` = `audit_plugin_deps.py --ownership-only`（约 2s）：每个 first-party
      模块都得有归属，红的是"死代码还是漏接"必须有人做决定。症状同样是零。
    两者与 `licenses` 同类：**漏了就补不回来 / 事后才发现**，所以都进 pre-commit。

    ⚠️ 本用例只在**全量**跑（不在 `FAST_TESTS` 里）—— 也就是说改了 `STEPS`
    集合后 `--fast` 是绿的、`pre-push`/CI 才红。2026-09-21 就是这样：`gate`
    与 `ownership` 两次提交各自只跑了 `--fast`，直到全量才发现这条没同步。
    改 `STEPS` 请顺手跑一次全量（或至少 `pytest m2_server/tests/test_check_ci_fidelity.py`）。
    """
    assert "ci-fidelity" not in check.STEPS
    assert set(check.STEPS) == {
        "licenses",
        "gate",
        "ownership",
        "requires",
        "electron",
        "ps1lint",
        "nodetest",
        "ruff",
        "pytest",
        "web",
    }


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


# ---------------- 6. `pyproject` 的 pytest 配置 ←→ `requirements-dev.txt` ----------------
# 这一节来自 2026-09-19 `--ci-fidelity` 首跑红：`pyproject.toml` 的 addopts 默认带
# `--cov=m2_server --cov-report=…`，而 `pytest-cov` **从没进过任何 requirements** ——
# 本机 `.venv` 里它只是某次手工装的残留，于是本机全绿、瘦环境连**参数解析**都过不去：
#     python -m pytest: error: unrecognized arguments: --cov=m2_server …
# 报错长得像"配置写错了"，实际是缺插件。**这类缺口是三道门禁的公共盲区**：
#   · `ruff`/`requires` 不管 pytest 配置；
#   · `_deps_stamp` 只管"requirements 变了就重装"，不管"该写的没写"；
#   · 全量自检跑在本机 `.venv`，而那正是唯一装了这个包的地方。
# 所以用一个**静态**对照把这条不变量钉住：addopts 里出现的每个长选项，
# 都必须在下面这张表里明说"由哪个发行包提供"。

#: addopts 选项 → 提供它的发行包（`None` = pytest 自带，无需声明）。
#: 不在表里的长选项**故意**直接失败 —— 强制改动者思考一次"这从哪来"，
#: 而不是让它默默躺在一个本机恰好装了的包里。
_ADDOPTS_OWNERS: dict[str, str | None] = {
    "--cov": "pytest-cov",
    "--cov-report": "pytest-cov",
}


def _parse_requirements_names(text: str) -> set[str]:
    """从 requirements 文本取**未被注释掉**的包名（归一化：小写、`_`→`-`）。

    注释必须真的被剥掉：`requirements-dev.txt` 里就有一行写着 "pytest-cov" 的注释
    （解释为什么不能改成去掉覆盖率），而那段注释**曾经就是这个 bug 的现场** ——
    所以这个解析器不能被自述文本骗过。
    """
    names = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[;\s]", line, maxsplit=1)[0]
        if name:
            names.add(name.lower().replace("_", "-"))
    return names


def _requirements_dev_names() -> set[str]:
    return _parse_requirements_names((ROOT / "requirements-dev.txt").read_text("utf-8"))


def _addopts_long_options() -> list[str]:
    """从 pyproject 的 `addopts` 里取长选项（去掉 `=值` 部分）。"""
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    addopts = data["tool"]["pytest"]["ini_options"]["addopts"]
    out = []
    for tok in addopts.split():
        opt = tok.split("=", 1)[0]
        if opt.startswith("--"):
            out.append(opt)
    return out


def test_requirements_parser_self_check():
    """解析器自检：注释行/行内注释不能被当成声明（否则这个门禁可以被一段注释骗绿）。"""
    names = _parse_requirements_names(
        "# pytest-cov\n"          # 整行注释：不是声明
        "pytest>=8\n"
        "pytest-asyncio  # 行内注释\n"
        "-r requirements.txt\n"   # include 行：不是包名
        "\n"
        "Pillow\n"
    )
    assert names == {"pytest", "pytest-asyncio", "pillow"}
    assert "pytest-cov" not in names, "整行注释被当成声明了"


def test_requirements_parser_reads_real_file():
    """真实文件必须解析得出东西（防"路径变了、这里默默变空集→全绿"）。"""
    names = _requirements_dev_names()
    assert {"pytest", "fastapi", "ruff"} <= names


def test_addopts_long_options_are_non_empty():
    """真实 addopts 必须解析得出长选项（防"换了 TOML 结构、这里默默变空集"）。"""
    assert _addopts_long_options(), "pyproject addopts 里没解析到任何长选项，解析器或配置变了"


def test_every_addopts_option_has_a_declared_owner():
    """addopts 的每个长选项都要在表里 —— 新增插件选项时强制做一次显式决定。"""
    unknown = [o for o in _addopts_long_options() if o not in _ADDOPTS_OWNERS]
    assert not unknown, (
        f"pyproject addopts 里有未登记的选项 {unknown}；"
        "请在 test_check_ci_fidelity._ADDOPTS_OWNERS 里写明它由哪个包提供"
    )


def test_addopts_plugin_distributions_are_declared_in_dev_requirements():
    """★ 本节的真正目的：addopts 用到的插件必须在**瘦环境**里有声明。

    否则 CI（只装 requirements-dev.txt）会在参数解析阶段就红，
    而本机因为手工装过而全绿——最骗人的那种绿。
    """
    declared = _requirements_dev_names()
    needed = sorted({d for d in _ADDOPTS_OWNERS.values() if d})
    missing = [d for d in needed if d.lower().replace("_", "-") not in declared]
    assert not missing, (
        f"pyproject addopts 需要 {missing}，但 requirements-dev.txt 没声明 —— "
        "CI 会在 `unrecognized arguments` 上红，而本机不会"
    )


def test_dev_requirements_declares_pytest_cov_specifically():
    """把具体那一项也钉住，而不是只靠上面的表（表被改空就全都没了）。"""
    assert "pytest-cov" in _requirements_dev_names()


#: `[tool.pytest.ini_options]` 里由**插件**提供的键 → 提供它的发行包。
#: addopts 那条用的是"未知就失败"，这里反过来用"已知就必须声明" —— 
#: ini 键大多是 pytest 自带的（testpaths / minversion / filterwarnings…），
#: 反向穷举会变成一份要不停维护的清单。两者合起来刚好盖住两类回归。
_INI_KEY_OWNERS: dict[str, str] = {
    "asyncio_mode": "pytest-asyncio",
    "asyncio_default_fixture_loop_scope": "pytest-asyncio",
}


def ini_options() -> dict:
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    return dict(data["tool"]["pytest"]["ini_options"])


def test_plugin_owned_ini_keys_have_their_plugin_declared():
    """★ 2026-09-19：`asyncio_mode = "auto"` 在瘦环境里只是一条
    `PytestConfigWarning: Unknown config option` —— 也就是**看似接好、实际惰性**。
    留着它就是留一份假信息，所以要么声明 `pytest-asyncio`，要么删掉这两项。

    注意不能反过来写"ini 里有未知键就红"：`filterwarnings` / `testpaths` 等
    都是 pytest 自带的，穷举它们会变成维护负担。
    """
    declared = _requirements_dev_names()
    present = [k for k in ini_options() if k in _INI_KEY_OWNERS]
    missing = sorted(
        {_INI_KEY_OWNERS[k] for k in present if _INI_KEY_OWNERS[k].lower().replace("_", "-") not in declared}
    )
    assert not missing, (
        f"pyproject 里用了 {present}，但 requirements-dev.txt 没声明 {missing}"
    )


def test_ini_key_owners_table_is_not_vacuous():
    """表本身不能变空（否则上一条永远绿）—— 变异验证：
    把 `asyncio_mode = "auto"` 加回 pyproject，这条仍绿而上一条会红。"""
    assert "asyncio_mode" in _INI_KEY_OWNERS
    assert ini_options(), "读不到 [tool.pytest.ini_options]"
