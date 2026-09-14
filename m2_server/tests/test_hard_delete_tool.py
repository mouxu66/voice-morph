"""hard_delete.py 入口参数解析测试。

背景（2026-09-12 实测踩坑）：手工调用 `hard_delete.py --help` 会崩在
`shutil.disk_usage(str(Path(args[0]).anchor))` —— `--help` 未被过滤，被当成路径，
而 `Path("--help").anchor` 是空串 → FileNotFoundError。相对路径同理。

本文件锁死这些入口分支，避免以后再退化：工具本身是"删大文件释放空间"的唯一可靠
手段，它一崩就没法用了。
"""
import importlib.util
import stat
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TOOL = _ROOT / "tools" / "hard_delete.py"


def _load_tool():
    """按路径加载 tools/hard_delete.py（tools 不是包，不能直接 import）。"""
    if not _TOOL.exists():
        pytest.skip(f"未找到 {_TOOL}")
    spec = importlib.util.spec_from_file_location("hard_delete_tool", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def tool(monkeypatch):
    mod = _load_tool()
    monkeypatch.setattr(sys, "argv", ["hard_delete.py"])
    return mod


def test_help_does_not_crash(tool, monkeypatch, capsys):
    """--help 应打印用法并返回 0，而不是把 '--help' 当路径去 disk_usage 崩掉。

    注意：main() 本身只 return，SystemExit 由 `__main__` 的 raise 触发；
    这里直接调 main() 故断言返回值而非异常。
    """
    monkeypatch.setattr(sys, "argv", ["hard_delete.py", "--help"])
    assert tool.main() == 0
    out = capsys.readouterr().out
    assert "用法" in out


def test_no_args_prints_usage_and_fails(tool):
    """无参数返回 1（提示用法），不应崩。"""
    assert tool.main() == 1


def test_relative_path_gives_clear_error_not_crash(tool, monkeypatch, capsys):
    """相对路径无法确定盘符 → 明确报错并返回 1，而不是 FileNotFoundError。"""
    monkeypatch.setattr(sys, "argv", ["hard_delete.py", "some_relative_file.txt"])
    assert tool.main() == 1
    err = capsys.readouterr().err
    assert "盘符" in err or "绝对路径" in err


def test_dry_run_does_not_delete(tool, monkeypatch, tmp_path, capsys):
    """--dry-run 只统计不删除。"""
    victim = tmp_path / "keep_me.bin"
    victim.write_bytes(b"x" * 1024)
    monkeypatch.setattr(sys, "argv", ["hard_delete.py", "--dry-run", str(victim)])
    assert tool.main() == 0
    assert victim.exists(), "dry-run 不得真删"
    assert victim.stat().st_size == 1024, "dry-run 不得截断"


def test_dry_run_flag_is_not_treated_as_path(tool, monkeypatch, tmp_path):
    """--dry-run 必须从路径列表里剔除（否则会被当路径去 stat）。"""
    victim = tmp_path / "f.bin"
    victim.write_bytes(b"y" * 10)
    monkeypatch.setattr(sys, "argv", ["hard_delete.py", "--dry-run", str(victim)])
    tool.main()
    assert victim.exists()


def test_hard_delete_truncates_then_removes(tool, tmp_path):
    """核心行为：先截断（空间立刻归还）再删除。"""
    victim = tmp_path / "big.bin"
    victim.write_bytes(b"z" * (1024 * 1024))
    tool.hard_delete(str(victim), dry_run=False)
    assert not victim.exists(), "应已被删除"


def test_hard_delete_handles_readonly_file(tool, tmp_path):
    """只读文件必须先清写保护再截断，否则被跳过 → 空间不释放。

    背景（2026-09-14 实测）：删 D:\\meanvc2_exp 时，`MeanVC2/.git/objects/pack/`
    下的 `*.pack` / `*.idx` / `*.rev` 是只读的，`open(fp, "r+b")` 直接
    `[Errno 13] Permission denied`，这几个文件被跳过。git 仓库、部分安装器的
    产物都是只读的，所以这条路径一定会再被走到。
    """
    victim = tmp_path / "readonly.bin"
    victim.write_bytes(b"r" * (512 * 1024))
    victim.chmod(stat.S_IREAD)
    try:
        assert not (victim.stat().st_mode & stat.S_IWRITE), "前置条件：文件应为只读"
        tool.hard_delete(str(victim), dry_run=False)
        assert not victim.exists(), "只读文件也必须被删除"
    finally:
        if victim.exists():  # 断言失败时别把只读文件留在 tmp 里
            victim.chmod(stat.S_IWRITE)


def test_truncate_tree_clears_readonly_without_removing(tool, tmp_path):
    """_truncate_tree 对只读文件也应截断成功（返回非零释放字节）。"""
    victim = tmp_path / "ro.bin"
    victim.write_bytes(b"q" * 4096)
    victim.chmod(stat.S_IREAD)
    try:
        n, freed = tool._truncate_tree(tmp_path)
        assert n >= 1 and freed >= 4096, "只读文件的内容应被截断并计入释放量"
        assert victim.stat().st_size == 0, "文件应已被截断为 0 字节"
    finally:
        victim.chmod(stat.S_IWRITE)
