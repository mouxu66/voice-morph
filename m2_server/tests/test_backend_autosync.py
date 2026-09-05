"""backend_autosync 自动同步模块测试。

覆盖：首次镜像复制（含排除规则）、无变化零操作、改动重拷、镜像清理
（源里删除的文件/目录同步清掉）、无桌面端结构跳过、安装版形态跳过、
VM_BACKEND_AUTOSYNC=0 总开关。全部跑在 tmp_path 上，不碰真实目录。
"""
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

import backend_autosync  # noqa: E402


def _make_src(root: Path) -> None:
    """构造最小源码树：m2_server / tools / web/dist（含应被排除的杂质）。"""
    (root / "m2_server").mkdir(parents=True)
    (root / "m2_server" / "server.py").write_text("print('server')\n", encoding="utf-8")
    (root / "m2_server" / "config.py").write_text("PORT = 8000\n", encoding="utf-8")
    (root / "m2_server" / "__pycache__").mkdir()
    (root / "m2_server" / "__pycache__" / "server.cpython-312.pyc").write_bytes(b"\x00")
    (root / "m2_server" / "server.log").write_text("log\n", encoding="utf-8")
    (root / "tools").mkdir()
    (root / "tools" / "doctor.py").write_text("print('doctor')\n", encoding="utf-8")
    (root / "web" / "dist").mkdir(parents=True)
    (root / "web" / "dist" / "index.html").write_text("<html></html>\n", encoding="utf-8")


@pytest.fixture()
def dev_root(tmp_path):
    """带桌面端结构的临时工程根，返回 (工程根, 兜底副本根)。"""
    root = tmp_path / "proj"
    (root / "voice-morph-desktop" / "resources").mkdir(parents=True)
    _make_src(root)
    return root, root / "voice-morph-desktop" / "resources" / "backend"


def test_first_sync_copies_and_excludes(dev_root):
    root, tgt = dev_root
    r = backend_autosync.sync_backend_copy(root)
    assert r["status"] == "synced"
    assert r["pairs"]["m2_server"]["copied"] == 2  # server.py + config.py；pyc/log 被排除
    assert r["pairs"]["tools"]["copied"] == 1
    assert r["pairs"]["web_dist"]["copied"] == 1
    assert (tgt / "m2_server" / "server.py").read_text(encoding="utf-8").startswith("print")
    assert not (tgt / "m2_server" / "__pycache__").exists()
    assert not list(tgt.rglob("*.log"))


def test_no_change_is_zero_op(dev_root):
    root, _tgt = dev_root
    backend_autosync.sync_backend_copy(root)
    r2 = backend_autosync.sync_backend_copy(root)
    assert all(v["copied"] == 0 and v["deleted"] == 0 for v in r2["pairs"].values())


def test_modified_file_recopied(dev_root):
    root, tgt = dev_root
    backend_autosync.sync_backend_copy(root)
    (root / "m2_server" / "server.py").write_text("print('server v2')\n", encoding="utf-8")
    r = backend_autosync.sync_backend_copy(root)
    assert r["pairs"]["m2_server"]["copied"] == 1
    assert "server v2" in (tgt / "m2_server" / "server.py").read_text(encoding="utf-8")


def test_mirror_deletes_stale_files_and_dirs(dev_root):
    root, tgt = dev_root
    backend_autosync.sync_backend_copy(root)
    stale = tgt / "m2_server" / "legacy_removed.py"
    stale.write_text("old\n", encoding="utf-8")
    stale_dir = tgt / "tools" / "removed_pkg"
    stale_dir.mkdir()
    (stale_dir / "x.py").write_text("x\n", encoding="utf-8")  # 非空目录：删文件后同轮 prune

    r = backend_autosync.sync_backend_copy(root)
    assert r["pairs"]["m2_server"]["deleted"] == 1
    assert not stale.exists()
    assert r["pairs"]["tools"]["deleted"] == 1
    assert r["pairs"]["tools"]["pruned_dirs"] == 1
    assert not stale_dir.exists()


def test_skip_without_desktop_dir(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    _make_src(root)
    r = backend_autosync.sync_backend_copy(root)
    assert r["status"] == "skipped"
    assert "voice-morph-desktop" in r["reason"]


def test_skip_installed_form(tmp_path):
    """安装版运行形态：源码根本身就是 resources/backend，必须跳过。"""
    root = tmp_path / "app" / "resources" / "backend"
    root.mkdir(parents=True)
    _make_src(root)
    r = backend_autosync.sync_backend_copy(root)
    assert r["status"] == "skipped"
    assert "安装版" in r["reason"]


def test_kill_switch(monkeypatch, tmp_path):
    root = tmp_path / "proj"
    (root / "voice-morph-desktop" / "resources").mkdir(parents=True)
    _make_src(root)
    called = []
    monkeypatch.setattr(backend_autosync, "_run_safe", lambda p: called.append(p))

    monkeypatch.setenv("VM_BACKEND_AUTOSYNC", "0")
    backend_autosync.autostart_sync(root)
    assert called == []  # 关闭时不启动任何线程

    monkeypatch.setenv("VM_BACKEND_AUTOSYNC", "1")

    class FakeThread:
        def __init__(self, target, args, name=None, daemon=False):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(
        backend_autosync, "threading", types.SimpleNamespace(Thread=FakeThread)
    )
    backend_autosync.autostart_sync(root)
    assert called == [root]
