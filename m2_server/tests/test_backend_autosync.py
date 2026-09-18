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


# --------------------------------------------------------------- 文档守卫

_SYNC_PS1 = Path(__file__).resolve().parents[2] / "tools" / "sync_backend.ps1"


def test_sync_ps1_documents_the_real_install_target():
    """`sync_backend.ps1` 必须写清「默认目标是 staging 目录，不是已安装的那份」。

    为什么值得用测试钉住（2026-09-18 实测踩到）：脚本与 autosync 的默认 `-TargetRoot`
    都是**源码根下的 staging 目录** `<项目根>\\voice-morph-desktop\\resources\\backend`，
    而**已安装**的桌面端读的是
    `%LOCALAPPDATA%\\Programs\\voice-morph-desktop\\resources\\backend` ——
    两者互不相干，autosync 也**不写**后者，所以装好的那份会悄悄过期。

    原先脚本注释还写着「本机开发不必重打包：resolveProjectRoot() 优先命中 D:\\变声 源码根」，
    那只对**源码模式**成立（`app.isPackaged === false`）。照着它做就会以为"已经同步了"，
    实际装好的那份一直没动。

    实测后果：`resources/backend/m2_server/rvc_live.py` 已是新版（会调
    `qwen3_tts.worker_alive()`），而 `qwen3_tts.py` 还是旧版（**没有这个函数**）
    → 运行期 `AttributeError`，实时变声的状态/启动路径直接崩。
    **半新半旧的混装比全旧更危险。**
    """
    text = _SYNC_PS1.read_text("utf-8")
    # 断言落在**注释块**（.SYNOPSIS/.DESCRIPTION，`param(` 之前）而不是全文：
    # 全文里 `staging` 还会出现在脚本体内的 Write-Host 提示里，只查全文的话
    # 注释块被改坏也不会转红（实测过 —— 变异验证时这条守卫没咬住）。
    head = text.split("param(")[0]
    assert "%LOCALAPPDATA%\\Programs\\voice-morph-desktop\\resources\\backend" in head, \
        "注释块里必须写出已安装副本的真实路径，否则下次还会拷错地方"
    assert "staging" in head, "必须点明默认目标是源码根下的 staging 目录"
    assert "app.isPackaged=false" in text, \
        "必须说清「优先命中源码根」只对源码模式（app.isPackaged=false）成立"
    assert "本机开发不必重打包" not in text, "旧的误导性说法必须删掉"


def test_sync_ps1_exclude_rule_matches_autosync():
    """脚本与 `backend_autosync.py` 的排除规则必须覆盖同一批杂质。

    `backend_autosync.py` 的注释明写"与 tools/sync_backend.ps1 的排除正则保持一致"，
    但两边是**各写一份**的常量 —— 改动一边忘了另一边，就会出现"autosync 拷了、
    手动脚本没拷"的诡异差异。这里把两者都钉在同一个集合上。
    """
    ps1 = _SYNC_PS1.read_text("utf-8")
    for token in ("__pycache__", "\\.pyc$", "\\.pyo$", "\\.log$", "\\.bak$", "\\.tmp$"):
        assert token in ps1, f"sync_backend.ps1 的排除正则里缺 {token!r}"
    for token in backend_autosync._EXCLUDE_SUFFIX:
        assert token.lstrip(".") in ps1, f"autosync 排除了 {token!r}，脚本的排除正则里却没有"


def test_sync_ps1_is_copy_only_and_says_so():
    """`sync_backend.ps1` 是「只拷不删」的，注释里不许声称与 autosync 语义一致。

    为什么钉住（2026-09-18 实测踩到）：ps1 的 `Sync-Dir` 只有 MD5 比对 + `Copy-Item`，
    **没有任何删除**；而 `backend_autosync.py` 有完整的 `f.unlink()` 镜像清理
    （第 139-147 行）加 `_prune_empty_dirs()`。原注释却写着「语义与本脚本一致
    （MD5 比对 + 镜像清理多余文件）」—— 照着信就会以为"源里删过的文件副本里也没了"，
    实际会**留在副本里**，又变成 §2.29 那种半新半旧的混装。

    反过来也成立：哪天给 ps1 真加了删除动作，这条守卫会转红，逼着作者同时更新注释 ——
    文档和行为必须一致，不能只改一边。
    """
    text = _SYNC_PS1.read_text("utf-8")
    head = text.split("param(")[0]

    assert "语义与本脚本一致" not in text, \
        "不许再声称与 autosync 语义一致 —— ps1 不做镜像清理"
    assert "只拷不删" in head, "注释块必须点明本脚本不删多余文件"
    assert "verify_backend_sync.py" in head, \
        "必须告诉读者用只读核验脚本去查「多余」文件"

    body = text.split("param(", 1)[1]
    assert "Remove-Item" not in body, (
        "ps1 出现了删除动作，注释却说「只拷不删」——"
        "要么撤掉删除，要么同步更新注释块与这条守卫"
    )
