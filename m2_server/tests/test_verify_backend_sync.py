"""`tools/verify_backend_sync.py` 的守卫测试。

为什么值得测：
    「核验工具」本身如果失灵，比没有更危险 —— 它会给出**假的安心感**。
    本项目的直接教训是 `md5sum` 那类"看起来在比对、其实全假红/全假绿"的手段
    （`docs/犯错指南.md` §2.28），以及 `diff --include` 把缺失文件过滤掉造成
    "零差异"假象（§2.30）。所以这个脚本必须被证明**真的能咬住漂移**。

测试策略：全部在 `tmp_path` 里构造 src/dst，不碰真实副本。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_verify_module():
    """按**路径**加载 `tools/verify_backend_sync.py`。

    ⚠️ 不能靠 `import verify_backend_sync`：`tools/` 不是包，且 pytest 的
    prepend 导入模式只会把测试文件所在目录插到 `sys.path` 前面 ——
    裸 import 找不到它。（同 §2.24 那个"裸 import conftest 被本目录遮蔽"的坑同源。）
    """
    path = _TOOLS_DIR / "verify_backend_sync.py"
    spec = importlib.util.spec_from_file_location("verify_backend_sync", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["verify_backend_sync"] = mod
    spec.loader.exec_module(mod)
    return mod


vbs = _load_verify_module()


# ---------------------------------------------------------------- _ignore_reason


@pytest.mark.parametrize(
    "rel, should_ignore",
    [
        ("m2_server/rvc_live.py", False),
        ("m2_server/server.py", False),
        ("tools/doctor.py", False),
        ("web_dist/index.html", False),
        ("m2_server/tests/test_foo.py", True),  # 测试目录
        ("m2_server/.pytest_cache/v/cache/nodeids", True),  # pytest 缓存
        ("tools/desktop-control/out/shot-1.png", True),  # 运行产物
        ("tools/desktop-control/cdp.py", True),  # 开发期工具
        ("tools/wx_green_judge_check.py", True),  # 开发期工具
        ("web/electron/pet/pet.html.bak-20260917-195316", True),  # 备份
    ],
)
def test_ignore_reason_classification(rel, should_ignore):
    got = vbs._ignore_reason(rel) is not None
    assert got is should_ignore, f"{rel!r} 的归类不符合预期（拿到 {vbs._ignore_reason(rel)!r}）"


def test_production_file_is_not_ignored():
    """生产代码绝不能被误归为可忽略 —— 那等于把真漂移静默掉（§2.30）。"""
    for rel in (
        "m2_server/rvc_live.py",
        "m2_server/qwen3_tts.py",
        "m2_server/openai_compat.py",
        "m2_server/data/rvc_texts.txt",
        "tools/doctor.py",
        "tools/sync_backend.ps1",
        "web_dist/index.html",
    ):
        assert vbs._ignore_reason(rel) is None, f"{rel!r} 不该被忽略"


# ---------------------------------------------------------------- _diff_pair


def _make(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def test_diff_pair_identical_is_clean(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make(src, {"a.py": "x", "sub/b.py": "y"})
    _make(dst, {"a.py": "x", "sub/b.py": "y"})
    res = vbs._diff_pair(tmp_path, "src", tmp_path, "dst")
    assert vbs._prod_bad(res) == 0
    assert res["prod_total"] == 2


def test_diff_pair_detects_changed(tmp_path):
    _make(tmp_path / "src", {"a.py": "new content"})
    _make(tmp_path / "dst", {"a.py": "old content"})
    res = vbs._diff_pair(tmp_path, "src", tmp_path, "dst")
    assert res["changed"] == ["a.py"]
    assert vbs._prod_bad(res) == 1


def test_diff_pair_detects_missing(tmp_path):
    """目标缺文件必须报出来 —— 这正是 §2.29「半新半旧混装」的形态。"""
    _make(tmp_path / "src", {"a.py": "x", "b.py": "y"})
    _make(tmp_path / "dst", {"a.py": "x"})
    res = vbs._diff_pair(tmp_path, "src", tmp_path, "dst")
    assert res["missing"] == ["b.py"]
    assert vbs._prod_bad(res) == 1


def test_diff_pair_detects_extra(tmp_path):
    _make(tmp_path / "src", {"a.py": "x"})
    _make(tmp_path / "dst", {"a.py": "x", "legacy.py": "z"})
    res = vbs._diff_pair(tmp_path, "src", tmp_path, "dst")
    assert res["extra"] == ["legacy.py"]
    assert vbs._prod_bad(res) == 1


def test_diff_pair_absent_dst_dir(tmp_path):
    """副本整个目录不存在 → 全部文件算缺失，不能静默返回"干净"。"""
    _make(tmp_path / "src", {"a.py": "x", "b.py": "y"})
    res = vbs._diff_pair(tmp_path, "src", tmp_path, "dst")
    assert res["dst_absent"] is True
    assert res["missing"] == ["a.py", "b.py"]
    assert vbs._prod_bad(res) == 2


def test_ignorable_diffs_do_not_count_as_prod(tmp_path):
    """「被遍历到、但归类为可忽略」的差异只能进 ign_*，不能污染生产代码计数。

    ⚠️ 2026-09-19 换过例子，别改回去：这里原本用 `tests/test_t.py` 与
    `.pytest_cache/v/cache/nodeids`。但同一天把 `tests` / `.pytest_cache` / `desktop-control`
    加进了 `backend_autosync._EXCLUDE_DIRS`（它们不该被镜像进副本），而本核验脚本
    的遍历**直接复用** `backend_autosync._walk` —— 于是那两个目录连遍历都进不来了，
    `ign_*` 恒为空，这条断言就变成了在测一个不存在的分类。

    现在的例子换成仍然会被遍历、仍然由 `_IGNORE_RULES` 归为可忽略的两类：
    运行产物目录（`outputs/`）与开发期脚本。注意不能用 `.bak-*`——
    它是**后缀级**排除，`_walk` 就抦掉了，同样进不了 ign_*。
    """
    _make(
        tmp_path / "src",
        {
            "a.py": "x",
            "outputs/run.txt": "v1",
            "tools/wx_green_judge_check.py": "v1",
        },
    )
    _make(
        tmp_path / "dst",
        {
            "a.py": "x",
            "outputs/run.txt": "v2",  # 内容不同（可忽略）
            # tools/wx_green_judge_check.py 整个缺失（可忽略）
        },
    )
    res = vbs._diff_pair(tmp_path, "src", tmp_path, "dst")
    assert vbs._prod_bad(res) == 0, "可忽略项的差异不该计入生产代码"
    assert res["ign_changed"] == ["outputs/run.txt"]
    assert res["ign_missing"] == ["tools/wx_green_judge_check.py"]
    assert vbs._ign_bad(res) == 2


def test_real_project_vs_itself_is_clean():
    """自洽性守卫：拿真实项目根自己跟自己比，必须零漂移。

    这条能同时验证 `_walk` / `_diff_pair` 在**真实目录结构**（含中文路径、
    深层子目录、各类产物）下不会误报。

    ⚠️ 两边都用 `src_rel`，**不能用 `dst_rel`** —— `_SYNC_PAIRS` 里
    `("web/dist", "web_dist")` 是**改名**映射（源码叫 `web/dist`、副本内叫
    `web_dist`），拿 `web_dist` 去源码根下找必然不存在，会误报 33 个缺失。
    """
    for src_rel, _dst_rel in vbs.backend_autosync._SYNC_PAIRS:
        res = vbs._diff_pair(_PROJECT_ROOT, src_rel, _PROJECT_ROOT, src_rel)
        assert res["src_absent"] is False, f"源码根下找不到 {src_rel}"
        assert vbs._prod_bad(res) == 0, f"{src_rel} 自己跟自己比竟报漂移: {res}"


def test_sync_pairs_rename_mapping_is_covered():
    """`web/dist` → `web_dist` 这对**改名**映射必须仍在 `_SYNC_PAIRS` 里。

    改名映射是本脚本最容易写错的地方（见上一条测试的注释）：一旦有人把它
    简化成"同名"，前端副本就再也核不到，混装会重新变得不可见。
    """
    pairs = dict(vbs.backend_autosync._SYNC_PAIRS)
    assert pairs.get("web/dist") == "web_dist", (
        "_SYNC_PAIRS 里 web/dist → web_dist 的改名映射丢了，"
        "前端副本将不再被核验（见 docs/犯错指南.md §2.29）"
    )
    assert pairs.get("m2_server") == "m2_server"
    assert pairs.get("tools") == "tools"
