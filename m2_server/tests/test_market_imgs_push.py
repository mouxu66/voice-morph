"""market_imgs_push：市场配图推送工具（远程图库的云端侧）。

背景（2026-09-22）：远程图库 `mouxu66/voice-market-assets` 的 `imgs/` 里
躺着一个 `manbo.png`（414KB）—— 它**不在 `images.json` 清单里**，所以永远不会
被任何客户端下载到，也就永远没人发现；而本脚本此前只往 `imgs/` **拷入**、
从不删除，于是每次推送 `git add -A` 都把它原样带回仓库。

本文件锁三件事：
  1. `scan_images` / `plan_mirror` 的纯逻辑（含换扩展名这种隐蔽情况）
  2. **镜像不变量** —— 跑完镜像后 `imgs/` 的文件名集合必须**等于** assets
  3. 真实素材回归：`manbo.png` 必须被判为多余
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TOOL = _ROOT / "tools" / "market_imgs_push.py"


def _load_tool():
    """按路径加载 tools/market_imgs_push.py（tools 不是包，不能直接 import）。"""
    if not _TOOL.exists():
        pytest.skip(f"未找到 {_TOOL}")
    spec = importlib.util.spec_from_file_location("market_imgs_push", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["market_imgs_push"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


# ---------------------------------------------------------------- scan_images


def test_scan_images_only_images_and_lowercases(tool, tmp_path):
    """只收图片扩展名；voice_id 取 stem 小写（清单键是 voice_id 形态）。"""
    (tmp_path / "Kiki.PNG").write_bytes(b"x")
    (tmp_path / "b.jpg").write_bytes(b"x")
    (tmp_path / "c.webp").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")   # 非图片 → 不收
    (tmp_path / "sub").mkdir()                   # 目录 → 不收
    assert tool.scan_images(tmp_path) == {"kiki": "Kiki.PNG", "b": "b.jpg", "c": "c.webp"}


# ---------------------------------------------------------------- plan_mirror


def test_plan_mirror_keeps_everything_when_identical(tool):
    """两边一致 → 一个都不删（镜像不该有副作用）。"""
    desired = {"a": "a.png", "b": "b.jpg"}
    assert tool.plan_mirror(desired, ["a.png", "b.jpg"]) == []


def test_plan_mirror_prunes_stale(tool):
    """远程多出来的死文件必须被删 —— 这就是 manbo.png 那种情况。"""
    desired = {"a": "a.png"}
    assert tool.plan_mirror(desired, ["a.png", "manbo.png", "zzz.png"]) == ["manbo.png", "zzz.png"]


def test_plan_mirror_prunes_renamed_extension(tool):
    """★ 变异测试守卫：同一 voice_id 换了扩展名（a.png → a.jpg），旧的 a.png 必须删。

    否则本地会同时存在两份，而 `market_images._find_local()` 按 `_IMG_EXTS`
    顺序取（png 在 jpg **之前**）→ 会一直取到那张**旧的**，
    "换图"看起来完全没生效。
    把 `plan_mirror` 的 `keep` 改成按 voice_id 比对（即 `{n.rsplit('.',1)[0]}`）
    本用例会红（返回 [] 而不是 ["a.png"]）。
    """
    desired = {"a": "a.jpg"}
    assert tool.plan_mirror(desired, ["a.png", "a.jpg"]) == ["a.png"]


def test_plan_mirror_leaves_non_image_files_alone(tool):
    """`imgs/` 下的非配图文件（.gitkeep 之类）不动 —— 本工具是镜像配图，不是清空目录。"""
    desired = {"a": "a.png"}
    assert tool.plan_mirror(desired, ["a.png", ".gitkeep", "LICENSE.txt"]) == []


# ---------------------------------------------------------------- 镜像不变量


def test_mirror_invariant_remote_equals_src(tool, tmp_path):
    """★ 不变量（比"删了谁"更强的判据）：镜像跑完后，远端文件名集合 == assets 的文件名集合。

    这条不针对某一个具体文件，而是钉住"镜像"这个语义本身 ——
    以后无论怎么改 plan_mirror，只要远端还会残留 assets 没有的文件就会红。
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.png").write_bytes(b"a")
    (src / "b.jpg").write_bytes(b"b")

    remote = tmp_path / "imgs"
    remote.mkdir()
    for name in ("a.png", "b.jpg", "manbo.png", "legacy_old.png", "b.png"):
        (remote / name).write_bytes(b"x")

    desired = tool.scan_images(src)
    for name in tool.plan_mirror(desired, [f.name for f in remote.iterdir()]):
        (remote / name).unlink()

    assert {f.name for f in remote.iterdir()} == set(desired.values()) == {"a.png", "b.jpg"}


# ---------------------------------------------------------------- 真实素材回归


def test_real_assets_do_not_contain_manbo(tool):
    """真实回归：`manbo.png` 是早期命名遗留（清单里早已只有 `katoong_manbo`），
    不在打包 assets 里 → 任何含它的远程 `imgs/` 都会被镜像清理掉。

    这条同时给 `plan_mirror` 一个真实输入（不是合成 fixture），
    防止"夹具与实现一起错、自洽地绿"。
    """
    src = tool.SRC
    if not src.is_dir():
        pytest.skip(f"未找到 {src}")
    desired = tool.scan_images(src)
    assert desired, "打包 assets 为空，测试前置不成立"
    assert "manbo.png" not in desired.values()
    assert "katoong_manbo.png" in desired.values()
    stale = tool.plan_mirror(desired, ["manbo.png", "katoong_manbo.png", "diyin.png"])
    assert stale == ["manbo.png"]
