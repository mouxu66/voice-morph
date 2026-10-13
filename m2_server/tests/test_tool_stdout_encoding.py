"""工具的 stdout 编码不是装饰 —— 它能把门禁的「通过」变成「失败」。

2026-09-21 实测，两个方向都会炸，且都只在 Windows + **stdout 被重定向**（管道/文件：
pytest 子进程、CI 日志、`> out.txt`）时现形：

① **子进程写不出**：`tools/audit_endpoint_ownership.py --check` 的 stdout 交给管道后
   按 ANSI(cp936) 编码，报告里第 615 行的 `⚠️` 直接 `UnicodeEncodeError` → 退出码 1。
   而它判定其实是「通过」：**门禁假红**，且报告只打了一半 —— 后面的章节全丢了，
   人连「哪个模块有问题」都看不到。控制台直连不触发（走 `WriteConsoleW`），
   所以只有重定向时才现形。
② **父进程读不进**：把 `PYTHONIOENCODING=utf-8` 设上后子进程改吐 UTF-8，而
   `subprocess.run(..., text=True)` 按 locale(cp936) 解 → `UnicodeDecodeError`。
   `tools/check.py` 恰好会给子进程设这个变量（`_env()` 的 `setdefault`），
   于是 **全量 `tools/check.py` 在 Windows 上恒红，`--fast` 却一直绿**
   （`FAST_TESTS` 里没有这两个文件）—— 这个红挂了半天没人发现，就是本文件存在的理由。

两侧都得显式编码才算修好：**工具自己** reconfigure stdout（这样用户 `> out.txt`
也成立），**测试自己**钉 `encoding="utf-8"`（这样子进程怎么输出都成立）。
`tools/check.py` 的 `_console()`、`tools/verify_backend_sync.py`、
`test_githooks._run()`、`test_plugin_loader` / `test_plugin_manifest` 早就是这么写的；
但**只靠「会不会崩」会漏掉半边**：`⚠️` 这类字是「崩」的那一半；被重定向时按 cp936
写出的**纯中文**不崩，却在任何 UTF-8 消费方（Git Bash、编辑器、CI 日志、`subprocess`
捕获）里显示成乱码（`��Ƶ�豸`）。2026-10-13 起规则改成**不看待打印的字符**：
`_ENTRY_DIRS` 下有 `def main()` 的入口一律自己钉 stdout，见
`_entry_scripts_missing_utf8_stdout`。
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
TESTS = Path(__file__).resolve().parent


#: 需要「入口脚本自己钉 stdout 编码」的目录 -> 是否递归。
#: 刻意不含：`*/tests/`（测试由下面那条 AST 规则管）、`tts_trial/_archive` 与
#: `tts_trial/MeanVC2`（归档 / 第三方）、`m2_server/tools`（另一套流水线）。
_ENTRY_DIRS: dict[str, bool] = {
    "tools": True,          # 含 desktop-control / _probe
    "m2_server": False,     # 顶层入口；子目录是 tests 与 plugins
    "tts_trial": False,
    "experiments": False,
    "m1_workshop": False,
}


def _entry_scripts_missing_utf8_stdout(root: Path = ROOT) -> list[str]:
    """有 `main()` 却没把 stdout 钉成 UTF-8 的入口脚本。

    判据**故意不看「它打印了什么」**：Windows 上 Python 的 stdout 一旦被重定向
    （管道 / `> out.txt` / Git Bash 的 pty），编码就退回 `locale`（cp936）；
    写**纯中文**不抛异常，但按 UTF-8 解码的消费方（Git Bash、编辑器、CI 日志、
    `subprocess` 捕获）看到的就是 `��Ƶ�豸` —— 与「崩在半截」是两个症状、同一条根因。
    按字符类筛会整条漏掉这条路径：2026-10-13 实测，按「GBK 编不出的字」筛只查出
    33 个，按「入口必须自己钉」查出 47 个。
    """
    offenders: list[str] = []
    for name, recursive in _ENTRY_DIRS.items():
        base = root / name
        if not base.is_dir():
            continue
        found = base.rglob("*.py") if recursive else base.glob("*.py")
        for path in sorted(found):
            if "tests" in path.parts or "_archive" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if not re.search(r"(?m)^(async )?def main[(]", text):
                continue
            if "reconfigure" in text:
                continue
            offenders.append(path.relative_to(root).as_posix())
    return offenders
def test_the_detector_actually_bites(tmp_path):
    """守卫自己要先有牙：造一棵假仓库，验它分别**报出**与**放过**。

    没有这一条，下面那个「清单为空」的断言在**规则本身写坏**时也会绿
    （`docs/犯错档案-工程.md` §8.21 的病：两边都空 = 白绿）。
    """
    def put(rel: str, body: str) -> None:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    # 该报：有入口、打印中文、没钉编码
    put("tools/bad_tool.py", """def main():
    print("⚠️ 没门")
""")
    # 该报：**纯中文也算** —— 它不崩，但在任何 UTF-8 消费方那里是乱码（本规则的理由）
    put("tools/plain_tool.py", """def main():
    print("全角；中文 —— 这些在 GBK 里")
""")
    # 该报：tools 是递归的
    put("tools/desktop-control/deep.py", """def main():
    print("中文")
""")
    # 该报：其它入口目录的顶层
    put("m1_workshop/pipeline_x.py", """def main():
    print("中文")
""")
    # 放过：已经钉了编码（写法不限）
    put("tools/good_tool.py", """import sys
def main():
    sys.stdout.reconfigure(encoding="utf-8")
    print("⚠️ 有门")
""")
    # 放过：没有 main()（模块级脚本不参与；也防止把 conftest 之类卷进来）
    put("tools/no_main.py", """print("⚠️ 模块级打印，没有 main")
""")
    # 放过：测试由下面那条 AST 规则管，这里不重复报
    put("tools/tests/test_x.py", """def main():
    print("中文")
""")
    # 放过：归档 / 第三方树
    put("tts_trial/_archive/scratch/old.py", """def main():
    print("中文")
""")
    put("tts_trial/MeanVC2/run.py", """def main():
    print("中文")
""")

    found = set(_entry_scripts_missing_utf8_stdout(tmp_path))
    assert found == {
        "tools/bad_tool.py",
        "tools/plain_tool.py",
        "tools/desktop-control/deep.py",
        "m1_workshop/pipeline_x.py",
    }, found
def test_every_entry_script_pins_its_stdout():
    """真实仓库必须零缺口。

    2026-10-13：按「入口脚本自己钉 UTF-8」这条统一口径补齐 47 个（此前只按
    「会崩的字符」补了 8 个）—— 规则是「入口必须钉」，不是「钉了几个」。
    """
    offenders = _entry_scripts_missing_utf8_stdout()
    detail = chr(10).join("  " + o for o in offenders)
    hint = (
        "入口脚本（有 def main()）必须自己把 stdout 钉成 UTF-8。"
        "症状：stdout 被重定向（管道 / `> out.txt` / Git Bash / pytest 子进程捕获）时"
        "Python 按 cp936 写、读的人按 UTF-8 解，中文变乱码；"
        "若还打了 GBK 编不出的字（⚠️ ✓），更会直接 UnicodeEncodeError，"
        "报告断在半截、退出码变 1（门禁假红）。"
        "修法：在 main() 开头（docstring 之后）加 try 块，参照 tools/check.py 的 _console()："
        "try: sys.stdout.reconfigure(encoding='utf-8', errors='replace')"
        " except (AttributeError, OSError, ValueError): pass"
    )
    assert not offenders, hint + chr(10) + detail
#: check.py 当门禁跑的脚本 + 它们「报告打到一半就崩」时最有说服力的那行。
#: `audit_plugin_deps.py --ownership-only` 没有无条件打的收尾行（唯一那行在
#: 被跳过的 `_print_third_party` 里），所以它只靠「退出码 + 无 traceback」判定。
_GATE_TOOLS = [
    ("audit_endpoint_ownership.py", ["--check"], "=== 未定位的函数（"),
    ("audit_plugin_deps.py", ["--ownership-only"], None),
]


@pytest.mark.parametrize("env_mode", ["按机器默认", "PYTHONIOENCODING=utf-8"])
@pytest.mark.parametrize(("script", "argv", "marker"), _GATE_TOOLS)
def test_gate_tools_survive_a_redirected_stdout(script, argv, marker, env_mode):
    """两种编码环境各跑一遍真实入口，**stdout 交给管道** —— 复现的就是原故障。

    参数化两个方向：`按机器默认`（子进程按 cp936 写 → 原本崩在 print）与
    `PYTHONIOENCODING=utf-8`（子进程写 UTF-8 → 原本崩在父进程解码）。
    """
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    if env_mode != "按机器默认":
        env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(TOOLS / script), *argv],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # 崩溃与「判定不通过」都会给退出码 1，但只有崩溃会留 traceback。这条是主判据。
    assert "Traceback" not in proc.stderr, (
        f"{script} {' '.join(argv)} 在「{env_mode}」下崩了 —— 输出编码没兜住：\n{proc.stderr[-1500:]}"
    )
    assert "UnicodeEncodeError" not in proc.stderr, proc.stderr[-1500:]
    assert "UnicodeDecodeError" not in proc.stderr, proc.stderr[-1500:]
    assert proc.returncode in (0, 1), f"退出码 {proc.returncode} 不是判定值（脚本崩了？）"
    if marker is not None:
        # 报告必须**打完整**：这个 marker 在原来崩掉那一行的后面
        assert marker in proc.stdout, (
            f"{script} 的报告只打了一半（缺 `{marker}`）—— print 中途抛异常了：\n"
            f"stdout 尾部:\n{proc.stdout[-800:]}"
        )


#: 会解码子进程输出的几个入口（`Popen` 家族都要管）。
_POPEN_FUNCS = ("run", "Popen", "check_output", "check_call", "call")


def _tests_spawning_own_python_without_encoding(root: Path = TESTS) -> list[str]:
    """测试里起**我们自己的 Python**（`sys.executable`）却按 locale 解码的调用点。

    用 **AST** 而不是「找 `text=True` 那行再看前后几行」：文本扫法会被**字符串
    字面量**骗到 —— 本文件自己的"牙齿测试"里就嵌着 `text=True` 的样例代码，
    文本扫法把它当成真调用（2026-09-21 实测：这条守卫第一次跑就红在自己身上）。
    AST 里字符串就只是字符串，也不怕跨行参数与注释。
    """
    offenders: list[str] = []
    for path in sorted(root.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text)
        except SyntaxError:  # 语法都不过的文件交给别的门禁去骂
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr in _POPEN_FUNCS):
                continue
            if not (isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
                continue
            kwargs = {k.arg: k.value for k in node.keywords if k.arg}
            val = kwargs.get("text")
            if not (isinstance(val, ast.Constant) and val.value is True):
                continue  # bytes 模式不解码，本来就不踩这个坑
            if "encoding" in kwargs:
                continue
            if "sys.executable" not in (ast.get_source_segment(text, node) or ""):
                continue  # 系统命令（git/ffmpeg/netstat）的编码是另一条守卫的事
            offenders.append(f"{path.name}:{node.lineno}")
    return offenders


def test_python_child_detector_bites_both_ways(tmp_path):
    """这条守卫的三种形态都要判对：正常的、缺 encoding 的、**字符串里的**。

    第三种是 2026-09-21 实测的假红来源：本文件自己的牙齿测试里嵌着 `text=True`
    的样例代码，文本扫法把它当成真调用 → 守卫第一次跑就红在自己身上。
    """
    (tmp_path / "test_ok.py").write_text(
        "def test_ok():\n"
        "    subprocess.run(\n"
        "        [sys.executable, '-c', 'print(1)'],\n"
        "        text=True,\n"
        "        # 好几行注释：为什么必须钉编码（真实仓库的形态）\n"
        "        # 2\n"
        "        # 3\n"
        '        encoding="utf-8",\n'
        "    )\n",
        encoding="utf-8",
    )
    (tmp_path / "test_bad.py").write_text(
        "def test_bad():\n"
        "    subprocess.run(\n"
        "        [sys.executable, '-c', 'print(1)'],\n"
        "        text=True,\n"
        "    )\n"
        "    subprocess.run(\n"
        "        ['git', 'status'],\n"
        "        text=True,\n"
        '        encoding="utf-8",\n'
        "    )\n",
        encoding="utf-8",
    )
    (tmp_path / "test_fixture.py").write_text(
        'FIXTURE = """\n'
        "    subprocess.run([sys.executable, '-c', 'x'], text=True)\n"
        '"""\n',
        encoding="utf-8",
    )
    found = _tests_spawning_own_python_without_encoding(tmp_path)
    assert not any(f.startswith("test_ok.py") for f in found), found
    assert any(f.startswith("test_bad.py") for f in found), found
    assert not any(f.startswith("test_fixture.py") for f in found), found


def test_tests_reading_our_own_subprocesses_pin_encoding():
    """子进程的输出编码由**它自己**（或环境变量）决定，父进程必须跟着钉死。

    这条比 `test_qwen3_tts_shutdown.test_system_command_calls_specify_encoding` 管的那批
    更进一步：那批管的是 `m2_server/*.py` 调 **Windows 系统命令**（GBK 输出）；
    这条管的是**测试**调**我们自己的脚本**（UTF-8 输出）——「谁写的谁定编码」，
    两侧不一致时必有一侧炸（`docs/犯错档案-工程.md` §8.25）。
    """
    offenders = _tests_spawning_own_python_without_encoding()
    assert not offenders, (
        "测试里起了我们自己的 Python 子进程却没钉 encoding —— `text=True` 会按\n"
        "`locale.getpreferredencoding()`（Windows=cp936）解码，而 `tools/check.py`\n"
        "给子进程设了 `PYTHONIOENCODING=utf-8`，两者不一致就是 UnicodeDecodeError。\n"
        '修法：加 `encoding="utf-8", errors="replace"`（子进程是自研工具时），\n'
        '或双向钉死（`env["PYTHONIOENCODING"]="utf-8"` + 解码侧也一样）。\n  '
        + "\n  ".join(offenders)
    )
