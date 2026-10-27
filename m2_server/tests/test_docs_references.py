"""`docs/` 里的 §编号引用与文件链接**必须真能解析**（2026-09-23）。

### 为什么需要

2026-09-23 把 `docs/交接-AI必读.md`（**4511 行**）拆成
`犯错指南.md`（门口，~300 行）+ `犯错档案-微信.md` + `犯错档案-工程.md`，
同时删掉 24 份"结论已进提交/门禁/现役文档"的过程稿。

麻烦在于：全仓有 **~500 处 `§N.M` 引用**（代码注释、测试 docstring、tools、`AGENTS.md`
都在指），拆分时它们必须被**分流到正确的档案**（`§2.x`/`§5.x` → 微信档案，
`§3.x`/`§8.x` → 工程档案，但 `§3.1–§3.6` 例外 —— 那几条"铁律"留在了门口）。
而这件事**没有任何东西会检查**：分流错地方的症状是"注释说见 §8.36，翻过去发现那一条不存在"，
只有人真去读才会撞见 —— 而人很少去读。

实测代价（本次拆分中真实发生过）：按"§3.x → 工程档案"的机械规则改完 71 个文件后，
`tools/check.py` 与 `tools/wechat_regression.py` 里的 **§3.3 / §3.5 被引到了工程档案**，
而那两条（"脚本说 ok 但实际没成功"、"测试/构建必须关掉 safe-delete shim"）留在了门口。
门禁一写出来就当场炸出这 4 处。

### 三类断言

| 守什么 | 用例 |
|---|---|
| `docs/犯错X.md §N.M` 的 `N.M` 在那个文件里真有对应标题 | `test_section_refs_resolve` |
| 仓库自有文件里 `docs/xxx.md` 形式的链接真存在（别指向已删的文档） | `test_no_dead_doc_links` |
| 速查表的行号是 1..N 连续（**拆分前它有 10 组重复号**，"速查表第 42 条"有歧义，而 `AGENTS.md` 与微信方案都在引用行号） | `test_cheatsheet_rows_are_sequential` |
| 门口不许再长回去（它涨到 4511 行才被拆，2026-10-27 合并后又到 1403 行） | `test_cheatsheet_has_a_ceiling` |
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DOCS = _ROOT / "docs"

#: 三份"犯错"文档 —— §编号在它们之间**不重号**（沿用拆分前的原值），所以引用带文件名才有意义。
#:
#: 2026-10-27：`犯错指南.md` 并进了 `交接-AI必读.md`（§3.1–§3.6 顺移为 §9.1–§9.6），
#: 所以这一组的第一个成员换了名字，**§编号仍然不与其他两份重号**。
ERR_DOCS = ("交接-AI必读.md", "犯错档案-微信.md", "犯错档案-工程.md")

#: `### 8.36 标题` / `### §8.36 标题` / `## 3.1 标题` 都算一条。
_HEADING = re.compile(r"^#{2,4}\s+(?:§)?(\d+\.\d+[a-z]?)\s")

#: `docs/交接-AI必读.md` §9.36` / ``docs\犯错档案-微信.md`` §2.28` 这类"带文件名的引用"。
#: 三份文档名统一由 `ERR_DOCS` 派生 —— 以后再加一份，只改上面那行就行。
_REF = re.compile(
    r"(?:(犯错档案-(?:微信|工程))|(交接-AI必读))\.md`?\s*§\s?(\d+\.\d+[a-z]?)"
)

#: 仓库自有文件里的 `docs/xxx.md` 链接。
#:
#: ⚠️ 必须排除**外部 URL 里的** `docs/xxx.md`（2026-10-14 实测踩到）：
#: 调研文档里有 `https://github.com/openvpi/DiffSinger/blob/main/docs/BestPractices.md`
#: 这类**别人仓库**的链接，正则一视同仁地把 `docs/BestPractices.md` 当成本仓引用 ⇒
#: 假红（本仓当然没有那份文件）。判据改成"**前面不是 `//` 或 `:/`**"，
#: 即只认相对路径式的 `docs/xxx.md`。
_LINK = re.compile(r"(?<![\\/])docs[\\/]([A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.\-]*\.md)")

#: 已打包进安装版 `app.asar` 的主进程文件里，注释中的文档引用**刻意冻结**（2026-10-27）。
#:
#: 为什么允许：改一个字节就会让已安装的 `app.asar` 报"陈旧"（`tools/audit_asar_freshness.cjs`
#: 逐字节对账），而清掉它必须**重打打包并重新安装**整个应用 —— 为两行注释重装一次不成比例。
#: 这两处指向的文档已并入 `docs/交接-AI必读.md`（§8.2 声板 / 附录 §1 发版）。
#:
#: 解锁方式：下一次发版重打（`npm run electron:build`）时顺手把这两行改成新档名，
#: 然后把本表清空。**表不能长期留着** —— 下面有断言要求它恰好命中两条，命中变少就会红。
FROZEN_DOC_REFS = {
    ("web/electron/sfx-window.cjs", "特效声板设计.md"),
    ("web/electron/updater.cjs", "release-sop.md"),
}

_SCAN_SUFFIXES = (".py", ".md", ".ps1", ".cjs", ".js", ".ts", ".tsx", ".yml", ".yaml")

#: 不读进内存的上限 —— package-lock.json 这类文件能到几 MB，把它读进来纯烧时间；
#: §引用与文档链接只会出现在人写的文件里。
_MAX_BYTES = 1_000_000

#: 只在这些目录里找引用与死链 —— 全是"人写代码/文档"的地方。
#: **不要换成全仓 rglob**：全仓遍历（12s 实测）的大头不是读文件（0.7s），而是被迫
#: 走过 `outputs/`、`media/`、`tts_models/`、`web/node_modules` 这些巨目录 ——
#: 就算 `_SKIP` 过滤了结果，遍历本身省不掉。收窄根目录后整套门禁 1~2s。
#: 新增「文档里会引用到」的源码目录时，加进来即可。
_SCAN_ROOTS = (
    "docs",
    "m2_server",
    "tools",
    "scripts",
    "tts_trial",
    "web/src",
    "web/electron",
    ".github",
    ".githooks",
    ".codebuddy/rules",
)

#: 不是"本仓文档"的地方：依赖、虚拟环境、外部参考库、构建产物、另一个 agent 的技能包。
_SKIP = (
    "node_modules",
    ".venv",
    "_ref",
    # tts_trial 本体要扫（2026-10-27 教训：它整个被跳过，于是 `docs/大创申报-2026-10/` 搬走后
    # `test_qwen_vs_rvc_verdict.py` 里那条路径没人发现，一直到全量 pytest 才炸），
    # 但它下面拖着自带 venv 与上游 vendored 仓库，这两个必须继续排掉。
    "tts_trial/venv312",
    "tts_trial/MeanVC2",
    "mobile",
    "htmlcov",
    ".git",
    "voice-morph-desktop",
    "release2",
    ".workbuddy",
    "outputs/",
    "agents/",
    ".agents/",
    "seed_vc_repo",
    "tts_models",
    "experiments/",
)


_INDEX: tuple[dict[str, list[str]], list[tuple[str, Path]]] | None = None


def _all_repo_files():
    """建（文件名 → 相对路径们）索引 + 可扫描文件列表，模块级缓存，只扫 `_SCAN_ROOTS`。

    逐版耗时（都实测）：死链检查每候选全盘 rglob 一次 = 2 分钟 → 每用例 rglob 一次 =
    40s → 模块缓存仍 16s（大头是全仓遍历：巨目录的**遍历**省不掉）→ **收窄扫描根** =
    1~2s，且不再需要落盘缓存。
    """
    global _INDEX
    if _INDEX is None:
        by_name: dict[str, list[str]] = {}
        scannable: list[tuple[str, Path]] = []
        seen: set[str] = set()

        def _walk(root: Path, rel_base: str) -> None:
            # 用 os.walk **就地剪枝**，不要 `root.rglob("*")`：`tts_trial` 自带
            # venv312（8.5G）+ MeanVC2（3.1G）共 6 万个条目，走进去再靠 `_SKIP` 过滤
            # = 每个文件都要 stat 一次，实测 21s；剪枝后 2s。
            for dirpath, dirnames, filenames in os.walk(root):
                d = Path(dirpath)
                sub = d.relative_to(root).as_posix()
                rel_dir = rel_base if sub == "." else (f"{rel_base}/{sub}" if rel_base else sub)
                dirnames[:] = [
                    n
                    for n in dirnames
                    if not any(x in (f"{rel_dir}/{n}" if rel_dir else n) for x in _SKIP)
                ]
                for fn in filenames:
                    p = d / fn
                    rel = f"{rel_dir}/{fn}" if rel_dir else fn
                    if rel in seen or not p.is_file():
                        continue
                    seen.add(rel)
                    if any(x in rel for x in _SKIP):
                        continue
                    by_name.setdefault(p.name, []).append(rel)
                    if p.suffix in _SCAN_SUFFIXES and p.stat().st_size <= _MAX_BYTES:
                        scannable.append((rel, p))

        for item in _SCAN_ROOTS:
            root = _ROOT / item
            if root.is_dir():
                _walk(root, item)
        # 根目录散着的 md / 配置（README、AGENTS、THIRD_PARTY_NOTICES…）
        for p in _ROOT.glob("*"):
            if p.is_file() and p.suffix in _SCAN_SUFFIXES and p.stat().st_size <= _MAX_BYTES:
                rel = p.name
                if rel not in seen:
                    seen.add(rel)
                    by_name.setdefault(p.name, []).append(rel)
                    scannable.append((rel, p))
        _INDEX = (by_name, scannable)
    return _INDEX


def _sections(path: Path) -> set[str]:
    return {m.group(1) for ln in path.read_text(encoding="utf-8").splitlines() if (m := _HEADING.match(ln))}


def _repo_files():
    """逐文件扫描用：直接给 (rel, Path)。"""
    return _all_repo_files()[1]


def test_the_three_error_docs_exist_and_are_split():
    """拆分本身也是断言：三份都在，且**速查表只在交接里有**。

    只断言"文件存在"是不够的 —— 把 §9 整块粘回档案、档案留个空壳，那样也"存在"。

    2026-10-27 合并后，"门口"变成 `交接-AI必读.md` §9：**速查表 + 六条铁律**在那儿；
    单条事故的完整复盘在另两份（`§2.x`/`§5.x` 微信、`§3.x`/`§8.x` 工程）。
    """
    for name in ERR_DOCS:
        assert (_DOCS / name).is_file(), f"缺少 {name}"

    guide_path = _DOCS / "交接-AI必读.md"
    assert "## 9 铁律与速查表" in guide_path.read_text(encoding="utf-8"), "交接里没有 §9（门口那部分）？"
    assert _sections(guide_path), "交接一条 §编号标题都没有？"

    for name in ("犯错档案-微信.md", "犯错档案-工程.md"):
        text = (_DOCS / name).read_text(encoding="utf-8")
        assert "| 症状 | 根因 | 对策 |" not in text, (
            f"{name} 里混进了速查表 —— 那是门口的（交接 §9.7），档案只放单条复盘"
        )
        assert not {s for s in _sections(_DOCS / name) if s.startswith("9.")}, (
            f"{name} 里混进了 §9.x —— 铁律只在交接里"
        )


def test_section_refs_resolve():
    """★ `docs/犯错X.md §N.M` → `N.M` 必须在**那个文件**里有对应标题。

    这是拆分那天最真实的失效模式：机械按节分流会把 §3.1–§3.6（留在门口的几条铁律）
    错引到工程档案 —— 而两者都"看得像对的"，只有去目标文件里查标题才知道。
    """
    have = {name: _sections(_DOCS / name) for name in ERR_DOCS}
    # 空集哨兵：三份都读成空的话，下面无论怎么引都"对"
    assert all(have.values()), f"有档案一条 §编号标题都没有：{ {k: len(v) for k, v in have.items()} }"

    bad: list[str] = []
    checked = 0
    for rel, path in _repo_files():
        for i, ln in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for m in _REF.finditer(ln):
                key = f"{m.group(1) or m.group(2)}.md"
                num = m.group(3)
                checked += 1
                if num not in have[key]:
                    bad.append(f"{rel}:{i} → {key} §{num} 不存在（{ln.strip()[:80]}）")
    # 实测基线 62 处（2026-09-23）。留点余量，但必须>0 —— 否则就是扫不到东西、白绿。
    assert checked >= 40, f"只找到 {checked} 处带文件名的 §引用 —— 引用格式变了？这个断言快变成空的"
    assert not bad, "引用指向了不存在的 §条目：\n  " + "\n  ".join(bad)


def test_no_dead_doc_links():
    """仓库自有文件里的 `docs/xxx.md` 必须真存在 —— 删文档时最容易留下一地死链。

    例外只有 `FROZEN_DOC_REFS` 那两条（打包进 app.asar、改一个字就要重装应用）。
    **例外必须恰好命中**：命中 0 条说明代码已经改过、表该清了；超过 2 条说明有人往表里加料。
    """
    by_name, _ = _all_repo_files()
    existing = {p.name for p in (_DOCS.rglob("*.md"))}
    assert existing, "docs/ 下一份 md 都没有？"
    bad: list[str] = []
    checked = 0
    frozen_hit: set[tuple[str, str]] = set()
    for rel, path in _repo_files():
        for i, ln in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for m in _LINK.finditer(ln):
                checked += 1
                # 同名文件只要在仓库里存在就算命中（不校验相对路径 —— 那是编辑器的职责，
                # 这里守的是「删了文档却没改引用」这一类）。
                if m.group(1) in existing or m.group(1) in by_name:
                    continue
                if (rel, m.group(1)) in FROZEN_DOC_REFS:
                    frozen_hit.add((rel, m.group(1)))
                    continue
                bad.append(f"{rel}:{i} → docs/{m.group(1)}（不存在）")
    assert checked >= 20, f"只找到 {checked} 处 docs/ 链接 —— 扫描范围是不是被 _SKIP 吃掉了？"
    assert not bad, "指向已不存在的 docs 文档：\n  " + "\n  ".join(bad)
    # 例外必须恰好命中两条：命中变少=代码已改，表该清；变多=有人往表里加料。
    assert frozen_hit == FROZEN_DOC_REFS, (
        f"豁免表与实际情况不符：命中 {sorted(frozen_hit)}，表里是 {sorted(FROZEN_DOC_REFS)}。\n"
        "  少的那条说明主进程源码已经改名了 —— 去把 FROZEN_DOC_REFS 里对应行删掉；\n"
        "  多的那条说明有人把新死链塞进了豁免表 —— 那应该改源码，不该改这张表。"
    )


def test_doc_link_scanner_bites_both_ways():
    """守卫自己要有牙：本仓相对链接要报，**外部 URL 里的同名片段不许报**。

    2026-10-14 实测踩到：调研文档引用别人仓库的
    `github.com/openvpi/DiffSinger/blob/main/docs/BestPractices.md`，
    旧正则把它当成本仓 `docs/BestPractices.md` ⇒ **假红**。
    这里把两个方向都钉住 —— 只修"假红"而让"真死链"漏报，是最坏的修法。
    """
    # 该报：本仓相对引用（不存在的文件）
    assert _LINK.search("见 docs/不存在的文档.md 那一节")
    assert _LINK.search("见 `docs/也不存在.md`")
    # 不该报：外部 URL（`//`、`:/` 前面那种）
    assert not _LINK.search("https://github.com/openvpi/DiffSinger/blob/main/docs/BestPractices.md")
    assert not _LINK.search("http://example.com/docs/foo.md")
    # 仍然该报：Windows 反斜杠形式的相对路径
    assert _LINK.search(r"见 docs\另也不存在.md")


def _cheatsheet_rows() -> list[int]:
    """`交接-AI必读.md` §9.7 那张表的行号列。

    ⚠️ 必须**只取 9.7 这一段**：9.8 检查清单与 9.9 诊断命令里的表格也有 `| 1 |` 这种首列，
    整篇扫会把它们算成"速查表第 1 条"，行号立刻不连续（2026-10-27 合并当天踩到）。
    """
    lines = (_DOCS / "交接-AI必读.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("### 9.7 "))
    end = next((i for i, ln in enumerate(lines[start + 1 :], start + 1) if ln.startswith("#")), len(lines))
    return [int(m.group(1)) for ln in lines[start:end] if (m := re.match(r"^\|\s*(\d+)\s*\|", ln))]


def test_cheatsheet_rows_are_sequential():
    """速查表的行号必须 1..N 连续。

    2026-09-23 之前它有**10 组重复号**（两个 24、两个 42…两个 48），
    而 `AGENTS.md`（"速查表第 27/32 条"）与 `docs/交接-AI必读.md`
    （一列行号）都在按行号引用 —— 有重号时"第 42 条"指向哪一条是**猜**。

    2026-10-27：`犯错指南.md` 并进 `交接-AI必读.md` §9.7，**表本身逐字搬过来、编号没动**，
    所以全仓"速查表第 N 条"的引用仍然成立。
    """
    rows = _cheatsheet_rows()
    assert rows, "速查表一行都没有？"
    assert rows == list(range(1, len(rows) + 1)), f"速查表行号不连续：{rows[:12]}…（共 {len(rows)} 行）"


def test_cheatsheet_has_a_ceiling():
    """「动代码前必读」那部分必须有上限。

    历史：`犯错指南.md` 涨到 **4511 行**才被拆（2026-09-23）；拆分后给它设了 900 行上限。
    2026-10-27 它整份并进 `交接-AI必读.md`，**上限不能跟着丢** —— 合并最容易做的事
    就是"什么都往里加"。

    设上限不是洁癖：**没有上限的"必读"文档，最终一定是没人读**。
    真踩了新坑请写进两份档案，或在这里加**一行**速查表。
    """
    rows = _cheatsheet_rows()
    assert len(rows) <= 240, (
        f"速查表已 {len(rows)} 条（上限 240）—— 它是门口，不是档案。"
        "完整复盘写 docs/犯错档案-微信.md 或 docs/犯错档案-工程.md。"
    )

    text = (_DOCS / "交接-AI必读.md").read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(text) if ln.startswith("## 9 "))
    end = next((i for i, ln in enumerate(text[start + 1 :], start + 1) if re.match(r"^## \d", ln)), len(text))
    assert end - start <= 560, (
        f"§9「铁律与速查表」已 {end - start} 行（上限 560）—— 这是每次改动都要读的部分，"
        "设计推导请写 §8，外部证据请写 §10。"
    )


@pytest.mark.parametrize("name", ERR_DOCS)
def test_each_error_doc_declares_where_to_put_new_entries(name):
    """每份都得说清"新坑写哪" —— 否则下一个人会把它们塞回门口。"""
    text = (_DOCS / name).read_text(encoding="utf-8")
    assert "犯错档案" in text, f"{name} 没提另两份档案（新坑会写错地方）"
