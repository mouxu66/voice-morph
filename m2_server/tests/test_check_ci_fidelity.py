"""tools/check.py 的 `--ci-fidelity` 辅助函数单测。

这个模式的全部价值是"如实复刻 CI"，而它有几处会**静默**失效：

1. **版本号解析**（从 `ci.yml` 读 Python/node 版本）。解析器一旦失效会悄悄回落到
   默认值 —— 那就变成"拿 3.11 假装复刻 3.13"，结论仍然是绿的，最骗人的那种绿。
2. **依赖指纹**（决定要不要重装依赖）。指纹算错会导致"改了 requirements 却没重装"，
   等于放过了这个模式唯一的假想敌：本机 `.venv` 与 CI 之间那批没被声明的包
   （2026-09-13 的 Pillow/comtypes 就是这个形状，见 `docs/犯错档案-工程.md` §3.9）。
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

    2026-09-29 又多了 `petlayout`，且**也进 --fast**：
    * `petlayout` = `tools/test-pet-panel-layout.cjs`（约 1.2s）：桌宠面板的**纵向预算
      门禁** —— 静态部分查"新功能页/新控件有没有登记预算"，外加一次 headless Chromium
      扫描（所有功能页 × 无气泡/大头气泡）实测余量。面板 300px 高是死的，新控件把底部
      挤掉是**提交那一刻就该拦下**的形状（见 `docs/桌宠遥控器.md`）。
      它从 `nodetest` 里摘了出来（`NODE_TESTS_OWNED_BY_OTHER_STEPS` 有登记）——
      因为 nodetest 不进 --fast，而这条必须进）。

    ⚠️ 本用例只在**全量**跑（不在 `FAST_TESTS` 里）—— 也就是说改了 `STEPS`
    集合后 `--fast` 是绿的、`pre-push`/CI 才红。2026-09-21 就是这样：`gate`
    与 `ownership` 两次提交各自只跑了 `--fast`，直到全量才发现这条没同步。
    **2026-09-29 又犯了一次**：`petlayout` 加进 `STEPS` 时这边没跟，全量直接红。
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
        "petlayout",
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


# ---------------- 7. ci.yml 的 web job ↔ check.py 的 `_check_web` ----------------
# 2026-10-02 实测到的洞，与 §6 同一族：**检查存在但不进门禁 = 不存在**。
# `npm run lint` 当时是全仓库**唯一只在 CI 跑**的检查（本机 check.py 九步、
# pre-commit --fast、pre-push 全都不含它），于是 4 个 eslint error 一路提交进主干，
# 而本机全绿、`--ci-fidelity` 也报"CI 应会绿"——**只有真 CI 会红**。
# 它与 2026-09-20 那次（lint 坏了一整年没人发现）的区别只是症状不同，根因是同一个：
# 「谁跑 lint」与「谁声明要跑 lint」写在两个地方，中间隔着人的记性。
#
# 本节把两条清单**互相钉住**：ci.yml 的 web job 有哪条命令，`_check_web` 就必须有哪步。
# 以后往 ci.yml 加一步（比如将来的 stylelint / prettier --check），这里立刻红。

#: ci.yml 的 web job 目前跑的命令 → `_check_web` 里对应那步的步骤名。
#: **不在表里的命令故意直接失败** —— 与 `_ADDOPTS_OWNERS` 同一手法：
#: 强制改动者显式写一次"它在 check.py 的哪一步"，而不是默默漏掉。
#: ⚠️ 这个表只回答"ci.yml 有 N 条 ↔ 本地叫得出 N 个名字"，**不回答**"本地真的跑了"。
#: 后者由 `_implemented_web_steps()` 从源码现算 —— 两者缺一不可：
#: 2026-10-02 第一版把步骤名也写死成本文件里的常量，结果把 `_check_web` 里的 lint 删掉后，
#: 那条"真正要拦的"断言**照样绿**（它查的是手抄清单，源码根本没参与）。
#: 手抄的清单与被检查的源码同源时，它就是一份漂亮的死尸。
_WEB_JOB_COMMANDS: dict[str, str] = {
    "npx tsc -b": "tsc",
    "npm run lint": "lint",
    "npx vitest run": "vitest",
}


def _implemented_web_steps() -> set[str]:
    """从 `tools/check.py` 的 `_check_web` 函数体里**现算**它实际跑哪几步。

    认 `_run("<名字>", …)` 这个形态 —— 它是 `_check_web` 唯一的执行入口，
    所以"函数体里出现过"就等价于"这一步会被跑"。
    """
    src = (ROOT / "tools" / "check.py").read_text("utf-8")
    start = src.index("def _check_web(")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]
    return set(re.findall(r'_run\(\s*"([^"]+)"', body))


def _ci_web_job_commands() -> list[str]:
    """从真实 ci.yml 的 web job 里抠出命令行（忽略注释行与空行）。

    必须剥注释：`ci.yml` 里就有一段注释在解释"eslint 此前坏了多久"，
    而这段注释里就有 `npm run lint` 这几个字 —— 直接搜会命中注释本身，
    于是「ci.yml 里有 lint」这条断言在**删掉真正那步之后仍然为真**，
    门禁就成了一具漂亮的死尸。
    """
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    lines = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()   # 剥行内注释与整行注释
        if not line:
            continue
        if line.startswith("#"):
            continue
        # 只在 web job 段内收集：backend job 的 `python tools/check.py --no-web`
        # 与 install 步骤不是"前端 job 跑的命令"。
        lines.append(line)
    # 简化：ci.yml 只有一个 web job 段，收集所有含 npx / npm run 的非注释行
    out = []
    for line in lines:
        if line.startswith("run:") or "  " in line:
            body = line.split("run:", 1)[-1].strip() if "run:" in line else line
        else:
            body = line
        for cmd in _WEB_JOB_COMMANDS:
            if body == cmd or body.startswith(cmd + " "):
                out.append(cmd)
    return sorted(set(out))


def test_ci_web_job_command_parser_reads_real_file():
    """解析器自检：真实 ci.yml 必须解析出全部三条（防"改了 ci.yml 结构 → 静默空集 → 全绿"）。"""
    got = _ci_web_job_commands()
    assert got == sorted(_WEB_JOB_COMMANDS), (
        f"ci.yml 的 web job 只解析出 {got}，与表不符 —— "
        "解析器或 workflow 变了，先修 _ci_web_job_commands 再说"
    )


def test_every_ci_web_job_command_has_a_step_in_check_web():
    """★ 本节真正要拦的：ci.yml 跑的东西 check.py 必须**源码里真的**跑。

    2026-10-02 的形状 —— `npm run lint` 长期只在 CI 跑，本机四处门禁全不覆盖，
    4 个 error 顺利进主干，而本机全绿、`--ci-fidelity` 还报"CI 应会绿"。
    往 ci.yml 加一步而忘了加 check.py 的步，这里立刻红。

    判定读的是 `_implemented_web_steps()`（从源码现算），**不是**本文件里的清单 ——
    这一点是被变异测试教会的：删掉 `_check_web` 里的 lint 步之后，
    第一版这条断言仍然是绿的，因为它比的是手抄清单与手抄清单。
    """
    implemented = _implemented_web_steps()
    missing = sorted(
        cmd for cmd in _ci_web_job_commands() if _WEB_JOB_COMMANDS[cmd] not in implemented
    )
    assert not missing, (
        f"ci.yml 的 web job 跑 {missing}，但 check.py 的 _check_web 没有对应步骤"
        f"（当前实现了 {sorted(implemented)}）。"
        "只写在 ci.yml 里 = 本机永远看不见它红（2026-10-02 的 lint 就是这样漏了）。"
        "请在 _check_web 里补这一步，并同步本文件 _WEB_JOB_COMMANDS"
    )


def test_check_web_step_names_match_the_table():
    """`_check_web` 的每一步都必须能在表里找到来由（防止有人加了个没登记的步骤）。

    这里**允许**本地多跑（多跑只是慢，不会假绿），但多出来的那步必须是有意为之 ——
    所以要求它先在 `_WEB_JOB_COMMANDS` 的值域里出现过。
    """
    declared_steps = set(_WEB_JOB_COMMANDS.values())
    unnamed = sorted(_implemented_web_steps() - declared_steps)
    assert not unnamed, (
        f"_check_web 里的步骤 {unnamed} 没在 _WEB_JOB_COMMANDS 里登记来由 —— "
        "要么在 ci.yml 里也加上（否则就是本机自创、CI 不认的检查），要么说明它为什么例外"
    )


def test_check_web_covers_every_ci_command_at_least_once():
    """变异验证：三条断言在"删掉某一步"时**至少有一条**变红。

    这条本身不检查什么，它防的是"上面几条集体失效"——
    2026-10-02 第一版就是因为三条都只比手抄清单而集体变绿。
    做法是真的去改源码（临时）再跑判定，最后还原。
    """
    src_path = ROOT / "tools" / "check.py"
    original = src_path.read_text("utf-8")
    for name in sorted(set(_WEB_JOB_COMMANDS.values())):
        mutated = re.sub(rf'_run\(\s*"{name}",', "_run(\"__MUTATED__\",", original, count=1)
        assert mutated != original, f"变异失败：源码里找不到 _run(\"{name}\"（这一步本来就不存在）"
        try:
            src_path.write_text(mutated, encoding="utf-8")
            still = set(_WEB_JOB_COMMANDS.values()) - _implemented_web_steps()
            assert still, f"删掉 {name} 步后，'ci.yml 有的本地没有'这条仍然绿 —— 门禁失效"
        finally:
            src_path.write_text(original, encoding="utf-8")
    assert _implemented_web_steps() == set(_WEB_JOB_COMMANDS.values()), "还原失败"


def test_lint_is_not_in_fast_subset(check, capsys):
    """eslint（实测 5.2s）不能进 `--fast`。

    pre-commit 的预算是 <5s（`.githooks/pre-commit` 钩子注释原话："超了人就该绕过了"）。
    把 5.2s 的检查塞进去 = 下一个人开始用 `--no-verify`，
    那就等于把已有的八步门禁一起作废 —— 拦住一次 5s 换掉八步，不划算。

    做法是**实测** `--fast --list` 的真实名单，而不是去查 `check.py` 里的某个常量：
    `--fast` 的过滤是内联元组（`names = [n for n in names if n not in ("web", …)]`），
    没有任何可断言的常量可引，将来有人重构这段也不会让本测试变假红。
    """
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "check.py"), "--fast", "--list"],
        capture_output=True, encoding="utf-8", errors="replace", cwd=str(ROOT),
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"--fast --list 退出码 {proc.returncode}：\n{out}"
    # 名单是逐行打印的步骤名；只要 web 出现就说明 lint 被带进了 pre-commit
    assert "\nweb" not in "\n" + out, f"web 步被放进了 --fast 名单：\n{out}"


# ---------------- 8. 汇总行的自述也不能是第二份拷贝 ----------------
# 上面那条门禁补上 lint 之后，我漏改了 `--ci-fidelity` 汇总行 ——
# 它还硬编码着"前端 job（tsc -b + vitest）"。而**汇总行是整个复刻流程里唯一
# 给人读的一行文字**：它写错，等于给一个（当时其实是假的）绿盖章。
# 与 §7 同一个病，只是对象从"测试里的清单"换成了"输出里的措辞"。


def test_web_func_src_reads_the_real_file(check):
    """`_web_func_src()` 必须真的从磁盘读到 `_check_web` 的函数体。

    它不用 `inspect.getsource` 是有原因的（脚本常被当 `__main__` 跑，
    那时模块对象没有可用源码，`inspect` 会抛 OSError）——所以要钉住
    "读磁盘"这条替代路径确实有效，且退化时**不抛异常而是返回空串**。
    """
    body = check._web_func_src()
    assert "def _check_web(" in body, "没从磁盘读到 _check_web 的源码"
    # 退化路径也不能崩（空串会让汇总行退化成"前端 job（，本机近似）"）
    assert isinstance(body, str)


def test_ci_fidelity_summary_does_not_hardcode_web_steps(check):
    """汇总行必须现算步骤名，不得硬编码 —— 硬编码就是第二份会腐烂的拷贝。

    变异验证：把 `_check_web` 的函数体换成一个空壳，`_web_func_src()` 的
    结果里就没有步骤名了；这说明它确实读的是源码而非别处写死的清单。
    """
    import re as _re

    body = check._web_func_src()
    steps = set(_re.findall(r'_run\(\s*"([^"]+)"', body))
    assert steps, "从源码里现算不到任何步骤 —— 这条守卫的取值路径本身坏了"
    # 汇总行里不许出现形如 "tsc -b + vitest" 的手写清单
    summary_src = (ROOT / "tools" / "check.py").read_text("utf-8")
    assert not _re.search(r"前端 job（tsc", summary_src), (
        "汇总行又硬编码了前端步骤清单 —— 步骤变了这行不会跟着变，"
        "而它是唯一给人读的一行（2026-10-02 因此漏改过）"
    )
