r"""禁区守卫（`conftest._guard_offlimits_writes`）自身的安全测试。

为什么这个文件必须存在
----------------------
2026-09-26 事故：`test_cover_fetch.py::_run_env` 两行顺序写反，把**真机**
`D:\RVC\.venv\Scripts\python.exe` 截成 0 字节，整条 RVC 变声链路瘫掉
（退出码 -1073741515）。pytest 那侧**全绿**，没有一条断言暴露 ——
这类错误的形态是"测试绿 + 机器坏"，所以唯一能自动发现它的手段就是专门测守卫本身。

★ 本文件的安全约定（改动时务必保持）
-------------------------------------
1. **不碰真机文件**。所有写入尝试都指向 `VM_OFFLIMITS_EXTRA` 声明的受控目录，
   它是本模块自己建的临时目录。禁区的**真实**路径（`D:/RVC`）只做字符串级
   断言，绝不对它发起写操作。
2. **不用 `parametrize` 枚举"要拦的那几条 API"**。原因：万一守卫失灵，
   parametrize 会让每个参数都真的执行一次破坏。全部逐个显式写死，
   出事时能一眼看出祸害了哪条路径。当前目标只有 1 个受控文件。

守卫的覆盖边界（本文件逐条钉住）
--------------------------------
    io.open            ← Path.write_bytes / write_text / Path.open（★当年的元凶）
    builtins.open      ← 脚本式 open(...)（**不经过 io.open**，必须单独补）
    os.open            ← 第三方库的底层 fd 操作
    os.remove / mkdir / rmdir
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _offlimits_dir() -> Path:
    """本模块声明的受控禁区目录。

    默认落在 `tempfile.gettempdir()/vm_offlimits_probe` —— **不是**真机 `D:/RVC`，
    所以本文件可以在常规套件里跑，不会威胁真实环境。
    需要指向别处时用 `VM_OFFLIMITS_EXTRA` 覆盖。
    """
    raw = (os.environ.get("VM_OFFLIMITS_EXTRA") or "").split(";")[0].strip()
    if raw:
        return Path(raw)
    import tempfile

    return Path(tempfile.gettempdir()) / "vm_offlimits_probe"


def _outside_dir() -> Path:
    """禁区**之外**的对照目录（证明守卫不误伤正常路径）。"""
    raw = (os.environ.get("VM_OUTSIDE_PROBE") or "").strip()
    if raw:
        return Path(raw)
    import tempfile

    return Path(tempfile.gettempdir()) / "vm_offlimits_outside_probe"


#: 受控目标的**预置内容**。守卫在 `import conftest` 末尾就装好了，而测试模块
#: 由 pytest 在 conftest 之后 import —— 所以模块级的 `_prepare_targets_once()`
#: 跑时守卫已经生效，必须用 `VM_OFFLIMITS_BYPASS` 旁路才能建出"犯规现场"。
_PREPARED = False


def _prepare_targets_once() -> None:
    """在收集期准备受控目录与目标文件。

    两件事都必须做，缺一测试就是假的：
      ① **把受控目录加进禁区**（`VM_OFFLIMITS_EXTRA`）—— 否则守卫根本不拦它，
         所有用例都会"通过"却什么都没验证到。
      ② 用 `VM_OFFLIMITS_BYPASS=1` 临时旁路，把目标文件建出来 ——
         受控目录现在就在禁区里，不旁路建不出"犯规现场"。
    """
    global _PREPARED
    if _PREPARED:
        return

    d = _offlimits_dir()

    # ① 声明禁区：追加而不是覆盖，保留 D:/RVC 与 ~/.workbuddy
    extra = os.environ.get("VM_OFFLIMITS_EXTRA", "")
    parts = [p.strip() for p in extra.split(";") if p.strip()]
    if str(d) not in parts:
        parts.append(str(d))
    os.environ["VM_OFFLIMITS_EXTRA"] = ";".join(parts)

    # ② 建现场
    prev = os.environ.get("VM_OFFLIMITS_BYPASS")
    os.environ["VM_OFFLIMITS_BYPASS"] = "1"
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "python.exe").write_bytes(b"PRECIOUS-INTERPRETER-BYTES")
        _outside_dir().mkdir(parents=True, exist_ok=True)
    finally:
        if prev is None:
            os.environ.pop("VM_OFFLIMITS_BYPASS", None)
        else:
            os.environ["VM_OFFLIMITS_BYPASS"] = prev
    _PREPARED = True


def _ensure_target(alive: bool = True) -> None:
    """保证预置目标是可用的。

    `alive=True` 时内容必须是 `PRECIOUS-INTERPRETER-BYTES`；`alive=False` 时
    允许为空（`os.rmdir` 之类用例可能把它清掉）。每个用例开头调一次，
    这样**前一条用例的守卫失灵不会把后面的用例一起带红** —— 每条只报自己的账。
    """
    _prepare_targets_once()
    p = _offlimits_dir() / "python.exe"
    if alive:
        p.write_bytes(b"PRECIOUS-INTERPRETER-BYTES")


@pytest.fixture(autouse=True)
def _fresh_target():
    """每条用例前把预置目标复位（旁路守卫，直接写）。"""
    _prepare_targets_once()
    prev = os.environ.get("VM_OFFLIMITS_BYPASS")
    os.environ["VM_OFFLIMITS_BYPASS"] = "1"
    try:
        p = _offlimits_dir() / "python.exe"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PRECIOUS-INTERPRETER-BYTES")
        _outside_dir().mkdir(parents=True, exist_ok=True)
    finally:
        if prev is None:
            os.environ.pop("VM_OFFLIMITS_BYPASS", None)
        else:
            os.environ["VM_OFFLIMITS_BYPASS"] = prev
    yield


@pytest.fixture
def victim():
    """受控目录里的一个"贵重文件"替身 —— 守卫失灵时会首当其冲被清空。"""
    p = _offlimits_dir() / "python.exe"
    assert p.exists(), f"预置目标不存在：{p}"
    return p


# ---------------------------------------------------------------- io.open 层
# 这几条是当年元凶的正面钉死：它们**必须**被拦住。


def test_path_write_bytes_is_blocked(victim):
    r"""`Path.write_bytes(b"")` —— 2026-09-26 事故的元凶，必须拦。

    若这条失败，说明真解释器可能已被清空。恢复步骤：
      base = C:\Users\mouxu\AppData\Roaming\uv\python\cpython-3.12.12-windows-x86_64-none
      把 python.exe / python3.dll / python312.dll / vcruntime140.dll /
      vcruntime140_1.dll **五个**一起拷回 D:\RVC\.venv\Scripts\
      （少一个都启动不了，症状是退出码 -1073741515）。
    """
    before = victim.stat().st_size
    with pytest.raises(AssertionError, match="真实环境"):
        victim.write_bytes(b"")
    assert victim.stat().st_size == before, "守卫失灵：文件被截断了"


def test_path_write_text_is_blocked(victim):
    before = victim.stat().st_size
    with pytest.raises(AssertionError, match="真实环境"):
        victim.write_text("")
    assert victim.stat().st_size == before, "守卫失灵：文件被改写了"


def test_path_open_write_is_blocked(victim):
    with pytest.raises(AssertionError, match="真实环境"), victim.open("wb") as f:
        f.write(b"")


def test_path_touch_is_blocked(victim):
    with pytest.raises(AssertionError, match="真实环境"):
        (victim.parent / "brand_new.txt").touch()


# ------------------------------------------------------- builtins.open 层
# 注意：builtins.open **不经过 io.open**，所以上面那层补了它照样能穿透 ——
# 这两条就是用来证明"两层互不覆盖、必须都补"的。
#
# 下面两处 SIM115（"Use a context manager"）是**故意不修**的 ruff 豁免：
# 要测的正是"裸调用 open 拿句柄、不起 with"这种最常见写法能否被拦 ——
# 改成 `with` 会顺带引入 `__exit__` 的关闭路径，反而偏离被测行为。


def test_builtin_open_write_is_blocked():
    with pytest.raises(AssertionError, match="真实环境"):
        open(_offlimits_dir() / "via_builtin.txt", "w")  # noqa: SIM115


def test_builtin_open_append_is_blocked():
    with pytest.raises(AssertionError, match="真实环境"):
        open(_offlimits_dir() / "python.exe", "a")  # noqa: SIM115


# ------------------------------------------------------------- os.open 层


def test_os_open_write_is_blocked():
    with pytest.raises(AssertionError, match="真实环境"):
        os.open(_offlimits_dir() / "via_os.txt", os.O_WRONLY | os.O_CREAT)


# --------------------------------------------------------- 删除与建目录


def test_os_remove_is_blocked(victim):
    with pytest.raises(AssertionError, match="真实环境"):
        os.remove(victim)
    assert victim.exists(), "守卫失灵：文件被删了"


def test_os_unlink_is_blocked(victim):
    with pytest.raises(AssertionError, match="真实环境"):
        os.unlink(victim)
    assert victim.exists()


def test_os_mkdir_is_blocked():
    with pytest.raises(AssertionError, match="真实环境"):
        os.mkdir(_offlimits_dir() / "newdir")


def test_os_rmdir_is_blocked():
    with pytest.raises(AssertionError, match="真实环境"):
        os.rmdir(_offlimits_dir() / "some_subdir")


# ------------------------------------------------------------ 不该被误拦的


def test_reading_offlimits_is_allowed(victim):
    """**读**禁区必须放行 —— 测试探版本、看权重在不在都是正当的。"""
    assert victim.read_bytes() == b"PRECIOUS-INTERPRETER-BYTES"
    assert victim.stat().st_size > 0
    assert victim.exists()


def test_tmp_path_writes_still_work(tmp_path):
    """普通测试的 tmp_path 读写不受影响。"""
    p = tmp_path / "f.bin"
    p.write_bytes(b"ok")
    assert p.read_bytes() == b"ok"
    (tmp_path / "d").mkdir()
    assert (tmp_path / "d").is_dir()


def test_writes_outside_offlimits_are_allowed():
    """禁区**之外**的路径照常可写 —— 守卫不能把整台机器的写操作都封死。"""
    out = _outside_dir()
    out.mkdir(parents=True, exist_ok=True)
    p = out / "allowed.txt"
    p.write_bytes(b"fine")
    assert p.read_bytes() == b"fine"
    p.unlink()
