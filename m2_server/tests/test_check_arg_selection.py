"""`tools/check.py` 的**检查集选择**单测（--only / --fast 的交叉）。

为什么值得单独钉一条：这里的失败模式是**空集绿灯**。

2026-09-29 实测：想单独跑一下 nodetest，敲了
`python tools/check.py --fast --only nodetest` —— 输出是

    自检汇总
    结果：全部通过

而它**一项都没跑**：`--fast` 会把 web / nodetest / ps1lint 从名单里剔掉，
所以那是空集。空集绿灯比报错危险得多：人会把「全部通过」当成
「门禁过了」写进结论里，而实际上什么都没验（本仓 §8.63 同一类教训：
判据看不见的实现改动 = 没有判据）。

这些用例只碰**参数选择**，不真跑任何检查：用 `--list` 让 main() 在
执行前返回，并直接断言返回码与输出。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def check():
    """按路径加载 tools/check.py（它是脚本不是包；导入期只有常量与函数定义）。"""
    spec = importlib.util.spec_from_file_location("tools_check_args", ROOT / "tools" / "check.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["tools_check_args"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_fast_plus_nonfast_only_is_an_error_not_a_vacuous_pass(check, capsys):
    """★ `--fast --only nodetest` 必须报错 —— 它一项都跑不了。"""
    rc = check.main(["--fast", "--only", "nodetest", "--list"])
    out = capsys.readouterr().out
    assert rc == 2, "空检查集应以非 0 退出（返回 0 会被上层当成“通过”）：" + out
    assert "检查集为空" in out, out
    assert "全部通过" not in out, "空集不能出现「通过」字样：" + out


def test_all_only_entries_filtered_is_still_an_error(check, capsys):
    """整批 --only 都被剔掉（web,ps1lint）时同样是空集，不是“都通过”。"""
    rc = check.main(["--fast", "--only", "web,ps1lint", "--list"])
    out = capsys.readouterr().out
    assert rc == 2, out
    assert "检查集为空" in out, out


def test_fast_plus_fast_only_still_works(check, capsys):
    """合法组合不能被误伤：`--fast --only ruff` 照旧列出来。"""
    rc = check.main(["--fast", "--only", "ruff", "--list"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "ruff" in out, out


def test_nonfast_only_without_fast_is_allowed(check, capsys):
    """不带 --fast 时 nodetest 本来就是名单里的项，必须照常可用。"""
    rc = check.main(["--only", "nodetest", "--list"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "nodetest" in out, out


def test_unknown_only_name_still_reports_unknown(check, capsys):
    """不认识的项仍然是「未知检查项」，别被新加的守卫抢了先。"""
    rc = check.main(["--only", "nope", "--list"])
    out = capsys.readouterr().out
    assert rc == 2, out
    assert "未知检查项" in out, out
