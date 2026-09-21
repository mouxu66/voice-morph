"""仓库卫生：桌面控制工具的运行时产物不得落回仓库工作树。

## 背景（2026-09-21，真事故）

`tools/desktop-control/` 是给 agent 用的「眼睛 + 手」：截图看屏幕、按 UIA 控件定位点击。
它的产物（全分辨率 png、agent 读的 jpg 预览、剪贴板 dump `_clip.txt`）**是你真实桌面的
截图**，可能含聊天记录、密钥窗口、别人的消息 —— 而本仓库是公开的。

原设计把产物写在 `tools/desktop-control/out/`，且**故意只 ignore `*.png`/`*.json`**，
留下 `*.jpg` 不 ignore（理由是读取工具对被 ignore 的路径一律 `[BLOCKED]`，agent 必须
能读到那张 jpg 才"看得见屏幕"）。代价是任何 `git add .` 都会把它们收进库 —— 这个代价
真实兑现了：2026-09-19 一条**改第三方许可的 docs 提交**顺手带进了 38 张截图 + `_clip.txt`
（commit `4af0a33`），09-21 用 `git-filter-repo` 重写未推送的 69 条提交才剔除干净。

修复分两层，本文件锁死第 2、3 层（第 1 层是历史重写，无法用测试表达）：

1. **`dc.ps1` 默认输出移到 `%TEMP%\\desktop-control`** —— 产物不在工作树内，
   `git add .` 天然收不到，同时 agent 照常可读，看图能力不变。
2. **`.gitignore` 整目录兜底 `tools/desktop-control/out/`** —— 覆盖"显式 -Out 指回仓库"
   和"磁盘上还有历史遗留产物"两种情况。
3. **README 与 `.gitignore` 注释必须与上述行为一致** —— 文档说"故意留 jpg 可见"而代码
   已搬走，会让后来者照着文档再犯一次。
"""

import re
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DC = _ROOT / "tools" / "desktop-control" / "dc.ps1"
_GITIGNORE = _ROOT / ".gitignore"
_README = _ROOT / "tools" / "desktop-control" / "README.md"

# 产物目录在仓库内的历史位置。它必须**永远**处在 gitignore 覆盖之下。
_OUT_IN_REPO = "tools/desktop-control/out/"


pytestmark = pytest.mark.skipif(
    not _DC.exists(), reason=f"未找到 {_DC}（仓库布局变化时请同步更新本测试）"
)


def _read_ps1() -> str:
    return _DC.read_text(encoding="utf-8-sig")


def _get_outdir_body() -> str:
    """抽出 dc.ps1 里 Get-OutDir 的函数体，供各用例断言。"""
    m = re.search(r"function Get-OutDir \{(.+?)\n\}", _read_ps1(), re.S)
    assert m, "dc.ps1 里没找到 Get-OutDir 函数 —— 函数被改名/删除时需同步更新本测试"
    return m.group(1)


# ------------------------------------------------- 第 1 层：默认输出在工作树外 ----

def test_default_outdir_is_outside_repo():
    """默认输出目录必须解析到仓库工作树之外。

    这是防"截图再次入库"的第一道锁：不在工作树里，`git add .` 就拿不到。
    """
    body = _get_outdir_body()

    assert "GetTempPath" in body, (
        "Get-OutDir 默认分支没走 [IO.Path]::GetTempPath() —— "
        "产物可能又回到仓库内了"
    )
    assert "Join-Path $PSScriptRoot 'out'" not in body, (
        "Get-OutDir 又出现了 'Join-Path $PSScriptRoot \"out\"' —— "
        "这正是 2026-09-19 截图入库的根因，别改回去"
    )

    out_dir = Path(tempfile.gettempdir()) / "desktop-control"
    assert not str(out_dir.resolve()).lower().startswith(str(_ROOT.resolve()).lower()), (
        f"复算出的输出目录 {out_dir} 落在仓库 {_ROOT} 内部"
    )


def test_out_override_still_available():
    """`-Out` 覆盖入口必须保留：有人确实需要把产物放进仓库时得有条明路。

    留着它也是为了让第 2 层的 .gitignore 兜底规则有意义（否则那条规则永远用不上）。
    """
    body = _get_outdir_body()
    assert "if ($Out) { $d = $Out }" in body, (
        "-Out 覆盖入口被破坏了：用户没法显式指定输出目录"
    )


# ------------------------------------------- 第 2 层：gitignore 整目录兜底 ----

def test_gitignore_covers_whole_out_dir():
    """.gitignore 必须整目录覆盖 out/，而不是只挑后缀。

    历史教训：只忽略 `*.png`/`*.json` 而留下 `*.jpg` 和 `_clip.txt` 可见，
    导致 38 张截图 + 剪贴板 dump 被一条无关的 docs 提交带进历史。
    """
    gi = _GITIGNORE.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in gi.splitlines()]

    assert _OUT_IN_REPO in lines, (
        f".gitignore 缺少整目录规则 `{_OUT_IN_REPO}` —— "
        "兜底网破了，有人 -Out 指回仓库时产物会被提交"
    )

    # 不能存在"只是看起来更像网"的窄规则把整目录规则取代掉的假象
    narrow = [
        ln for ln in lines
        if ln.startswith(_OUT_IN_REPO) and ln != _OUT_IN_REPO
    ]
    assert not narrow, (
        f"gitignore 里出现了 out/ 下的窄规则 {narrow}；"
        f"整目录规则 `{_OUT_IN_REPO}` 已足够，窄规则会给人'jpg 仍可见'的错误暗示"
    )


# ------------------------------------ 第 3 层：文档不得与行为互相矛盾 ----

def test_gitignore_comment_has_no_stale_jpg_note():
    """.gitignore 注释不得再说"故意留下 *.jpg 可见"。

    这条注释曾准确描述了当时的行为；dc.ps1 搬走产物后它就成了误导 ——
    后来者读到"jpg 故意可见"会以为可以安心 `git add .`。
    """
    gi = _GITIGNORE.read_text(encoding="utf-8")
    assert "故意留下" not in gi and "故意保留" not in gi, (
        ".gitignore 仍在说 jpg『故意可见/保留』—— 产物已搬到仓库外，"
        "这句注释现在与行为矛盾，会诱导后来者照旧 add"
    )


def test_readme_documents_temp_outdir():
    """README 的『产物落在哪』必须写明新路径，且不再教人 add 整个目录。"""
    assert _README.exists(), f"未找到 {_README}"
    readme = _README.read_text(encoding="utf-8")

    assert "desktop-control/out" not in readme.replace(_OUT_IN_REPO, ""), (
        "README 仍在用仓库内的 tools/desktop-control/out 作为产物路径"
    )
    assert "TEMP" in readme or "Temp" in readme, (
        "README 没说明产物现在落在 %TEMP%\\desktop-control"
    )


# ------------------------------------------ 第 4 层：残留不能回来（2026-09-21） ----
#
# 前三层管的是「新产物不再落进工作树」。但工作树里还**留着**一份历史遗留产物
# （`tools/desktop-control/out/`，48 个全分辨率截图，90MB）。2026-09-21 已把它
# 移到库外 `D:/tmp/dc-out-residue-20260921/`（另有全量备份 `D:/tmp/dc-out-backup/out/`，
# 多出那 39 个早先从工作树移除的 jpg 与 `_clip.txt`）；两份都逐字节核对过一致。
#
# 于是 `tools/desktop-control/` 现在只有源码。下面把「源码目录里不该有产物」
# 钉成结构判据 —— 这才是真正的修法（按犯错指南 §8.22：靠纪律不如靠结构）。
# 光有 gitignore 是「纪律型」防护：它只在有人正确使用 git 时才生效。


def _tracked_under(rel: str) -> list[str]:
    """git 跟踪的、位于 rel 目录下的文件（相对仓库根）。"""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "ls-files", "--", rel],
            cwd=str(_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover —— 没 git 就跳过判断
        pytest.skip("跑不了 git ls-files（非 git 工作树？）")
    if out.returncode != 0:
        pytest.skip(f"git ls-files 失败：{out.stderr.strip()[:200]}")
    return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]


def test_no_screenshots_tracked_in_desktop_control():
    """★ 工作树里不许**跟踪**任何桌面控制产物。

    这条比 gitignore 更强：ignore 只影响 `git add .`，而 `git add -f`、
    改过的 ignore 规则、或别的工具都可能把截图塞进来。这里是直接问 git
    「你到底跟踪了哪些」—— 与 ignore 规则是否写对无关。
    """
    tracked = _tracked_under("tools/desktop-control")
    assert tracked, "git ls-files 一个文件都没返回 —— 路径写错了吗？"
    bad = [
        f
        for f in tracked
        if not f.endswith((".md", ".ps1", ".py", ".cjs"))
    ]
    assert bad == [], (
        f"tools/desktop-control/ 下跟踪了产物类文件（截图/JSON/文本 dump）：{bad}\n"
        f"这些是真实桌面截图，仓库是公开的。产物应落在工作树外（默认 %TEMP%），"
        f"历史残留见本文件末段注释。"
    )


def test_out_dir_absent_from_work_tree():
    """★ `out/` 目录不该存在 —— 它存在就等于工作树里躺着一堆桌面截图。

    红了的修法：把它移出工作树（`mv tools/desktop-control/out <库外路径>`），
    别就地删 —— 那是证据/备份，先移走再决定。若你**故意**用 `-Out` 指回了仓库，
    用完请一并移走：README 已写明这条口子的代价。
    """
    out = _ROOT / "tools" / "desktop-control" / "out"
    if not out.exists():
        return
    residue = sorted(p.name for p in out.iterdir())
    assert not residue, (
        f"工作树里又出现了桌面截图产物：tools/desktop-control/out/ 有 {len(residue)} 个文件"
        f"（如 {residue[:3]}）。\n"
        f"请移出工作树：mv tools/desktop-control/out D:/tmp/dc-out-residue-<日期>/"
    )


def test_readme_records_the_residue_is_out_of_work_tree():
    """README 必须记下「残留已移出工作树」这件事与库外位置。

    没有这条，下一个人看到 `out/` 不见了会以为被误删，或者重新把它建回来。
    """
    text = _README.read_text(encoding="utf-8")
    assert "收尾（2026-09-21 已完成）" in text, "README 没记这次收尾"
    for token in ("dc-out-residue-20260921", "dc-out-backup"):
        assert token in text, f"README 没写明库外路径 {token!r} —— 归档找不到等于丢了"
