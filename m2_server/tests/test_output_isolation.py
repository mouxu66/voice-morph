"""★ 守卫：测试会话里，**导入时刻**的 `cfg.OUTPUTS_DIR` / `cfg.MEDIA_DIR` 不许是真实目录。

为什么需要（2026-09-22，`docs/犯错指南.md` §8.36 / §8.37）：
`config.OUTPUTS_DIR` / `MEDIA_DIR` 被一批模块在**导入时**取走变成模块级常量
（`market_preview.MARKET_DIR`、`market_images.CACHE_DIR`、`finetune.FT_DIR`、
`cascade.STATE_FILE` …，全仓 40+ 处）。导入之后再
`monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp)` 对它们**无效、且不报错** ——
于是跑全量时真实 `outputs/market/` 里累积了 20+ 个**夹具名**的 sidecar、
`outputs/market/imgs_cache/.revision` 被覆写成远端版本号、
`media/ft/<voice>/status.json`（用户真实微调任务的状态）被改写成测试数据。

对策在 `m2_server/conftest.py`：在**任何 app 模块被导入之前**把 `VM_OUTPUTS_DIR` /
`VM_MEDIA_DIR` 指向临时目录，让 `config` 自己算出正确的值（早绑定也就跟着对了）。

★ 本文件是那条对策的机器判据。**钉的是「导入时刻的值」，不是「此刻各模块属性」** ——
后者会假绿：`tests/conftest.py` 的 autouse 夹具把 `cfg.OUTPUTS_DIR` 换成了本次用例的
`tmp_path`，而测试文件里 `importlib.import_module(...)` 是**在用例内**才导入的，
于是那些常量一律绑定到 tmp_path、看起来"永远正确"（实测：漏掉 VM_OUTPUTS_DIR
整行、按模块属性判断仍然全绿）。这类「**探针被运行时满足**」是本项目的老朋友，
所以这里用 **session 作用域**取值：它跑在任何 function 作用域 monkeypatch **之前**，
拿到的正是「模块级早绑定会捕获到的那个值」。

★ 变异测试：把 `m2_server/conftest.py` 里 `os.environ["VM_OUTPUTS_DIR"] = ...`
那行注释掉，`test_import_time_data_dirs_are_isolated` 与 `test_env_vars_are_the_mechanism`
必须**双双变红**。
"""

import ast
import os
import sys
from pathlib import Path

import pytest

_M2 = Path(__file__).resolve().parents[1]  # m2_server/
_REPO = _M2.parent  # 仓库根
#: ★ 真实的数据根（与 VM_*_DIR 无关）—— 测试绝不许碰。
_REAL_ROOTS = {"OUTPUTS_DIR": _REPO / "outputs", "MEDIA_DIR": _REPO / "media"}

if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

#: session 起始时刻的 `cfg.<根>` —— 即「模块级早绑定会捕获到的那些值」。
_IMPORT_TIME: dict[str, Path] = {}


@pytest.fixture(scope="session", autouse=True)
def _capture_import_time_dirs():
    """在任何 function 作用域 monkeypatch 之前，记下 `cfg.OUTPUTS_DIR` / `cfg.MEDIA_DIR` 的初值。"""
    import config as cfg

    for name in _REAL_ROOTS:
        _IMPORT_TIME[name] = Path(getattr(cfg, name))
    yield


def _under_real(p: Path, real: Path) -> bool:
    try:
        Path(p).relative_to(real)
    except (TypeError, ValueError):
        return False
    return True


def test_import_time_data_dirs_are_isolated():
    """★ 核心不变量：模块级早绑定捕获到的 outputs/ 与 media/ 路径，绝不是真实目录。

    这是**确定性**判据：不依赖"跑完之后的快照对比"，因此不会被
    「开发机上后端正在跑 / 线程晚到」之类的外部因素弄成假红。
    """
    assert _IMPORT_TIME, "session 夹具没跑到（守卫本身失效）"
    bad = [
        f"cfg.{name} = {value}"
        for name, value in _IMPORT_TIME.items()
        if _under_real(value, _REAL_ROOTS[name])
    ]
    assert not bad, (
        "导入时刻的数据目录指向了**真实**目录：\n  "
        + "\n  ".join(bad)
        + "\n这会让 market_preview.MARKET_DIR / market_images.CACHE_DIR / finetune.FT_DIR\n"
        "等早绑定常量在测试里写到用户真实数据上（§8.36 / §8.37）。\n"
        '多半是 m2_server/conftest.py 里 `os.environ["VM_OUTPUTS_DIR"]` /\n'
        '`os.environ["VM_MEDIA_DIR"]` 那几行被去掉、或挪到了 import config 之后。'
    )


def test_env_vars_are_the_mechanism():
    """把「靠什么做到的」也钉住：机制是两个 VM_*_DIR 环境变量，且必须先于 config 导入生效。"""
    import config as cfg

    for name in _REAL_ROOTS:
        assert os.environ.get(f"VM_{name}"), f"conftest 没设 VM_{name}"
        assert Path(getattr(cfg, name)) != _REAL_ROOTS[name], (
            f"cfg.{name} 仍是真实目录 —— VM_{name} 设晚了（必须在 import config 之前）"
        )


# ---------------- 扫描器（顺带钉住"清单不会漏"）----------------


def _is_outputs_ref(node: ast.AST) -> bool:
    """这个表达式里有没有引用 OUTPUTS_DIR？（`cfg.OUTPUTS_DIR` 或 `from config import OUTPUTS_DIR`）"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == "OUTPUTS_DIR":
            return True
        if isinstance(sub, ast.Name) and sub.id == "OUTPUTS_DIR":
            return True
    return False


def _scan_early_bound() -> list[tuple[str, str]]:
    """扫 m2_server/*.py：模块级 `NAME = … OUTPUTS_DIR …` 的 (模块名, 常量名)。

    用 AST 而不是 grep：必须是**模块级**赋值 —— 缩进里的函数局部变量不受导入时序影响。
    """
    found: list[tuple[str, str]] = []
    for py in sorted(_M2.glob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover —— 语法错误会由 ruff/pytest 更早报
            continue
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if not all(isinstance(t, ast.Name) for t in targets):
                continue
            if _is_outputs_ref(node.value):
                found.extend((py.stem, t.id) for t in targets)
    return found


def test_scan_finds_the_known_hotspots():
    """扫描器本身要**真的扫到东西**，否则"清单类"判据会退化成空集上的假绿。

    钉住几个已知热点：扫描逻辑一坏（比如 AST 判断写错），这条先红。
    """
    found = set(_scan_early_bound())
    for expect in (
        ("market_preview", "MARKET_DIR"),  # §8.36 的元凶
        ("market_images", "CACHE_DIR"),  # `from config import OUTPUTS_DIR` 写法
        ("market_install", "OLD_DIR"),
    ):
        assert expect in found, (
            f"扫描器没扫到 {expect} —— 扫描逻辑坏了（实扫到 {len(found)} 处）。"
        )
    assert len(found) >= 15, f"只扫到 {len(found)} 处，远少于预期（全仓 40+ 处 OUTPUTS_DIR 派生）"
