"""桌面安装包「塞了什么 / 漏了什么」的机器门禁。

两条都是 2026-09-19 打包实测出来的真事故，而且**都是静默的**——不报错、不崩，
只是把体积和效果悄悄改掉。所以必须由测试守，不能靠注释。

1. **多塞（体积）**：electron-builder 会把 `dependencies` 整棵树拷进 `resources/app.asar`，
   **`files` 里的 `dist/**/*` / `electron/**/*` 白名单管不住它**；而 vite 早已把前端代码
   打进 `dist/`。实测：app.asar 47.1MB / 5305 个条目，其中 `node_modules` 37.4MB /
   5236 个条目（lucide-react 19MB、react-dom 7MB、@fontsource 6.2MB…），
   而主进程 `web/electron/**/*.cjs` 对第三方包的 require 数是 **0**
   （只有 fs/path/child_process/electron 等内置模块）。
   对策是 `build.files` 里加一条 `!node_modules/**/*`：app.asar 47.1MB → 6.74MB，
   安装包 101.5MB → 93.4MB。

   > 也曾试过"把前端库全挪进 `devDependencies`"——效果一样，**但被否决了**：
   > `tools/audit_licenses.py` 的登记口径只看 `dependencies`（它的假定是
   > "devDependencies 不进发行物"），而 `@fontsource/*` 的 woff2 **真的随 dist/ 发出去**，
   > 挪走会让许可门禁当场判定"载荷里的字体已不是声明依赖"并漏掉 OFL 署名义务。
   > 而这只能靠 **`files` 排除**解决：依赖写在哪个分区是 npm 语义，
   > 什么进发行物是打包器的事，两者本就不该混为一谈。

2. **漏发（功能静默退化）**：`extraResources` 里 m2_server 那条 filter 带了
   `"!**/data/**"`，把 `m2_server/data/rvc_texts.txt`（4KB，**生产文件**）挡在安装包外。
   `config.load_rvc_texts()` 有内置 20 句兜底 → 不报错，只是把 RVC 训练语料从
   100+ 句场景句悄悄退回 20 句通用句。反之 `tools/` 那条**没有任何 filter** →
   `tools/desktop-control/out/`（87 张调试截图 / 95MB，2026-09-19 入库）会整体进包。

判定口径与 `tools/verify_backend_sync.py` 的 `_IGNORE_RULES` 对齐（那边是唯一权威口径：
"副本里本来就不该有的东西"）。改那边记得同步这里。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "web" / "package.json"


@pytest.fixture(scope="module")
def pkg() -> dict:
    return json.loads(PKG.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def extras(pkg) -> dict[str, dict]:
    """`extraResources` 按 `to` 建索引。"""
    return {e["to"]: e for e in pkg["build"]["extraResources"]}


# ---------------------------------------------------------------- 1. 多塞


def _main_process_requires() -> set[str]:
    """主进程**真正** require 的第三方包名（内置模块与相对路径不算）。"""
    builtin = {
        "assert", "child_process", "crypto", "fs", "http", "https", "module",
        "net", "os", "path", "url", "util", "zlib", "electron",
    }
    found: set[str] = set()
    for js in (ROOT / "web" / "electron").rglob("*.cjs"):
        for m in re.finditer(r"""require\(\s*["']([^"']+)["']\s*\)""", js.read_text(encoding="utf-8")):
            name = m.group(1)
            if name.startswith(".") or name.startswith("node:"):
                continue
            pkgname = "/".join(name.split("/")[:2]) if name.startswith("@") else name.split("/")[0]
            if pkgname not in builtin:
                found.add(pkgname)
    return found


def test_packaging_excludes_node_modules(pkg):
    """`build.files` 必须排除 `node_modules` —— 这是 asar 不重复存前端库的唯一防线。

    electron-builder 默认把 `dependencies` **整棵树**拷进 `resources/app.asar`，
    **`files` 里的 `dist/**/*` / `electron/**/*` 白名单管不住它**，只有显式的
    `!node_modules/**/*` 能拦住。而这些库 vite 早已打进 `dist/`。

    2026-09-19 实测：不排除时 asar 47.1MB / 5305 条目（`node_modules` 占 37.4MB /
    5236 条目，lucide-react 单独 19MB）；加上这条后 6.74MB / 71 条目，
    安装包 101.5MB → 93.4MB。
    """
    files = pkg["build"]["files"]
    assert "!node_modules/**/*" in files, (
        "web/package.json 的 build.files 里丢了 `!node_modules/**/*` —— "
        "app.asar 会重新变大 ~40MB。实测把依赖从 dependencies 挪走也能达到同样效果，"
        "但那会让许可门禁（tools/audit_licenses.py 只看 dependencies）漏掉随发行物"
        "一起走的字体，所以选定的是这条排除。"
    )


def test_main_process_requires_no_third_party_package():
    """主进程不能 require 任何第三方包 —— 这是上面那条全局排除能成立的**前提**。

    排除是"一刀切"的（整棵 node_modules 都不进包）。所以只要主进程 require 了
    任何一个外部包，安装版就会在运行期 `MODULE_NOT_FOUND`。
    真要引，就把排除改成白名单（只放行那个包），**别直接删断言**。
    """
    used = sorted(_main_process_requires())
    assert not used, (
        f"web/electron 下的主进程代码 require 了第三方包：{used}\n"
        f"而 build.files 里那条 `!node_modules/**/*` 会把它们全部排除在安装包外"
        f" → 安装版运行期会 MODULE_NOT_FOUND。\n"
        f"改法：把排除换成白名单（只放行这几个包），并同步这条断言。"
    )


# ---------------------------------------------------------------- 2. 漏发


def _glob_match(pattern: str, rel: str) -> bool:
    """minimatch 语义的**近似**实现，只覆盖本项目 filter 用到的 `**` / `*` / `?`。

    ⚠️ 这是近似，不是 minimatch 复刻。真正的判定由下面
    `test_packaged_artifact_*` 在 `win-unpacked` 存在时逐项对照真实产物
    （`!**/desktop-control/**` 能命中根下的 `desktop-control/` 就是实测确认过的：
    `**/` 允许匹配零层目录）。

    ⚠️ 必须**单趟**扫描，别写成"先 replace(\"**\") 再 replace(\"*\")"那种链式替换：
    第一步产出的 `.*` 里带着 `*`，会被第二步再吃一遍变成 `.[^/]*`
    （实测后果：`desktop-control/out/x.png` 这类**跨一层目录**的路径全部漏判，
    而 `desktop-control/cdp.py` 正常 —— 长得像"规则没问题"，最难查）。
    """
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*" and pattern[i : i + 2] == "**":
            if pattern[i + 2 : i + 3] == "/":
                out.append("(?:.*/)?")  # `**/` 允许匹配零层目录
                i += 3
            else:
                out.append(".*")
                i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.fullmatch("".join(out), rel) is not None


def _excluded(entry: dict, rel: str) -> bool:
    """按 electron-builder 口径判断 `rel` 是否被该 extraResources 条目排除。

    规则表一律"先全纳入 + 若干 `!` 排除"，所以只看得有任一条 negation 命中。
    """
    for pat in entry.get("filter", []):
        if pat.startswith("!") and _glob_match(pat[1:], rel):
            return True
    return False


@pytest.mark.parametrize(
    "pattern, rel, expected",
    [
        # 规则自检：匹配器自己没人守也会悄悄失效（链式 replace 那个 bug 就是这样
        # 漏掉全部跨目录路径的）。这里钉住几条**本 filter 实际在用**的方言。
        ("**/*", "data/rvc_texts.txt", True),
        ("**/*", "doctor.py", True),
        ("**/data/**", "data/rvc_texts.txt", True),  # 旧的 bug 规则
        ("**/desktop-control/**", "desktop-control/out/shot.png", True),
        ("**/desktop-control/**", "desktop-control/cdp.py", True),
        ("**/desktop-control/**", "natscore_local/__init__.py", False),
        ("**/__pycache__/**", "__pycache__/x.pyc", True),
        ("**/__pycache__/**", "a/b/__pycache__/x.pyc", True),
        ("**/*.pyc", "doctor.py", False),
        ("**/*.pyc", "sub/x.pyc", True),
        ("**/*.bak-*", "doctor.py.bak-20260907", True),
        ("**/outputs/**", "outputs/run.log", True),
        ("**/.pytest_cache/**", ".pytest_cache/v/cache/nodeids", True),
    ],
)
def test_glob_match_handles_the_dialects_in_use(pattern, rel, expected):
    assert _glob_match(pattern, rel) is expected, f"{pattern!r} vs {rel!r}"


def test_m2_server_filter_keeps_bundled_rvc_texts(extras):
    """`m2_server/data/rvc_texts.txt` 必须进包。

    它只占 4KB，却是**生产文件**：`config.load_rvc_texts()` 读它当 RVC 训练语料模板。
    被排除**不会报错**（有内置 20 句兜底），只会让训练语料从 100+ 句悄悄退回 20 句
    —— 用户只会觉得"训出来不太像"，永远查不到是打包漏了一个文件。
    `tools/verify_backend_sync.py` 的 `_IGNORE_RULES` 同样把它归为"不可忽略"。
    """
    entry = extras["backend/m2_server"]
    assert not _excluded(entry, "data/rvc_texts.txt"), (
        "m2_server 的 extraResources filter 把 data/rvc_texts.txt 排除掉了 —— "
        "安装版会缺这份生产语料（静默退化，不报错）。"
    )
    # 真源文件必须在仓库里，否则上面这条断言只是"漏了个不存在的文件"。
    assert (ROOT / "m2_server" / "data" / "rvc_texts.txt").is_file()


@pytest.mark.parametrize(
    "rel",
    [
        "desktop-control/out/shot-145157-085.png",  # 95MB 调试截图（2026-09-19 入库）
        "desktop-control/out/_clip.txt",
        "desktop-control/cdp.py",  # 开发期桌面控制工具
        "desktop-control/dc.ps1",
        "__pycache__/doctor.cpython-311.pyc",
        ".pytest_cache/v/cache/nodeids",
        "wx_green_judge_check.py",  # 开发期离线测量
        "verify_backend_sync.py",  # 开发期核验脚本自身
        "outputs/run.log",
        "doctor.py.bak-20260907",
        "setup_env.ps1.orig",
    ],
)
def test_tools_filter_excludes_dev_only_paths(extras, rel):
    """`tools/` 必须显式排除开发期产物 —— 它原本**一条 filter 都没有**。

    后果实测：2026-09-19 那批 87 张调试截图（95MB）会整体进安装包，
    外加 `__pycache__`、`.pytest_cache`、备份文件。
    """
    assert _excluded(extras["backend/tools"], rel), f"tools/ 的 filter 没排除 {rel!r}"


@pytest.mark.parametrize(
    "rel",
    [
        "doctor.py",  # 体检脚本，被 tools/ 下别的脚本 import
        "setup_env.ps1",  # 新机器部署入口，README 直接引用
        "sync_backend.ps1",
        "natscore_local/__init__.py",  # 运行时被 ab_chain / audition_score import
        "check.py",
        "cleanup.ps1",
    ],
)
def test_tools_filter_keeps_production_paths(extras, rel):
    """反向钉子：这些是安装版要用的，排除它们就是真丢功能。

    尤其是 `natscore_local/`（152KB）—— `ab_chain.py` / `audition_score.py` 会在
    运行时 `sys.path` 里找它来算自然度分。只按"名字像个工具"去筛会误伤。
    """
    assert not _excluded(extras["backend/tools"], rel), f"tools/ 的 filter 误伤了 {rel!r}"


# ---------------------------------------------------------------- 3. 真实产物（有则验）


_UNPACKED = ROOT / "web" / "release2" / "win-unpacked" / "resources"
_HAS_ARTIFACT = _UNPACKED.is_dir()


@pytest.mark.skipif(not _HAS_ARTIFACT, reason="本机没有 win-unpacked（未打包过）")
def test_packaged_artifact_carries_rvc_texts():
    """真实产物里必须有 data/rvc_texts.txt —— 上面那些是规则推演，这条是实测。"""
    assert (_UNPACKED / "backend" / "m2_server" / "data" / "rvc_texts.txt").is_file()


@pytest.mark.skipif(not _HAS_ARTIFACT, reason="本机没有 win-unpacked（未打包过）")
def test_packaged_artifact_excludes_desktop_control():
    assert not (_UNPACKED / "backend" / "tools" / "desktop-control").exists()


@pytest.mark.skipif(not _HAS_ARTIFACT, reason="本机没有 win-unpacked（未打包过）")
def test_packaged_asar_has_no_node_modules():
    """app.asar 上限 13MB —— 只有"又把前端库塞回 dependencies"才会突破。

    实测：dist 3.8MB + electron 2.8MB + package.json ≈ 6.7MB；
    塞了 13 个前端库时是 47.1MB。13MB 留了很宽的余量，正常改动不会误报。
    """
    asar = _UNPACKED / "app.asar"
    assert asar.is_file()
    size_mb = asar.stat().st_size / 1048576
    assert size_mb < 13, (
        f"app.asar 有 {size_mb:.1f}MB（预期 <13MB）—— "
        f"多半是 web/package.json 的 dependencies 里又出现了前端库。"
        f"跑一下 test_production_dependencies_are_required_by_main_process 定位。"
    )
