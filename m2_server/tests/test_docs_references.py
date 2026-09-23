"""`docs/` 里的 §编号引用与文件链接**必须真能解析**（2026-09-23）。

### 为什么需要

2026-09-23 把 `docs/犯错指南.md`（**4511 行**）拆成
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
| 门口不许再长回去（它涨到 4511 行才被拆） | `test_guide_stays_a_door` |
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DOCS = _ROOT / "docs"

#: 三份"犯错"文档 —— §编号在它们之间**不重号**（沿用拆分前的原值），所以引用带文件名才有意义。
ERR_DOCS = ("犯错指南.md", "犯错档案-微信.md", "犯错档案-工程.md")

#: `### 8.36 标题` / `### §8.36 标题` / `## 3.1 标题` 都算一条。
_HEADING = re.compile(r"^#{2,4}\s+(?:§)?(\d+\.\d+[a-z]?)\s")

#: `docs/犯错指南.md` §8.36` / ``docs\犯错档案-微信.md`` §2.28` 这类"带文件名的引用"。
_REF = re.compile(r"犯错档案-(微信|工程)\.md`?\s*§\s?(\d+\.\d+[a-z]?)|犯错指南\.md`?\s*§\s?(\d+\.\d+[a-z]?)")

#: 仓库自有文件里的 `docs/xxx.md` 链接。
_LINK = re.compile(r"docs[\\/]([A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.\-]*\.md)")

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
    "tts_trial",
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
            for p in root.rglob("*"):
                rel = f"{rel_base}/{p.relative_to(root).as_posix()}" if rel_base else p.relative_to(root).as_posix()
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
    """拆分本身也是断言：三份都在，`犯错指南.md` **不含** §2.x/§8.x（那些在档案里）。

    只断言"文件存在"是不够的 —— 把内容整块粘回指南、档案留个空壳，那样也"存在"。
    """
    for name in ERR_DOCS:
        assert (_DOCS / name).is_file(), f"缺少 {name}"
    guide = _sections(_DOCS / "犯错指南.md")
    assert guide, "犯错指南.md 一条 §编号标题都没有？"
    assert not {s for s in guide if s.split(".")[0] in ("2", "8")}, (
        f"犯错指南.md 里混进了 §2.x/§8.x：{sorted(s for s in guide if s.split('.')[0] in ('2','8'))}"
        " —— 那些属于两份档案"
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
                key = f"犯错档案-{m.group(1)}.md" if m.group(1) else "犯错指南.md"
                num = m.group(2) or m.group(3)
                checked += 1
                if num not in have[key]:
                    bad.append(f"{rel}:{i} → {key} §{num} 不存在（{ln.strip()[:80]}）")
    # 实测基线 62 处（2026-09-23）。留点余量，但必须>0 —— 否则就是扫不到东西、白绿。
    assert checked >= 40, f"只找到 {checked} 处带文件名的 §引用 —— 引用格式变了？这个断言快变成空的"
    assert not bad, "引用指向了不存在的 §条目：\n  " + "\n  ".join(bad)


def test_no_dead_doc_links():
    """仓库自有文件里的 `docs/xxx.md` 必须真存在 —— 删文档时最容易留下一地死链。"""
    by_name, _ = _all_repo_files()
    existing = {p.name for p in (_DOCS.rglob("*.md"))}
    assert existing, "docs/ 下一份 md 都没有？"
    bad: list[str] = []
    checked = 0
    for rel, path in _repo_files():
        for i, ln in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for m in _LINK.finditer(ln):
                checked += 1
                # 同名文件只要在仓库里存在就算命中（不校验相对路径 —— 那是编辑器的职责，
                # 这里守的是「删了文档却没改引用」这一类）。
                if m.group(1) not in existing and m.group(1) not in by_name:
                    bad.append(f"{rel}:{i} → docs/{m.group(1)}（不存在）")
    assert checked >= 20, f"只找到 {checked} 处 docs/ 链接 —— 扫描范围是不是被 _SKIP 吃掉了？"
    assert not bad, "指向已不存在的 docs 文档：\n  " + "\n  ".join(bad)


def test_cheatsheet_rows_are_sequential():
    """速查表的行号必须 1..N 连续。

    2026-09-23 之前它有**10 组重复号**（两个 24、两个 42…两个 48），
    而 `AGENTS.md`（"速查表第 27/32 条"）与 `docs/微信语音-长文分段发送方案.md`
    （一列行号）都在按行号引用 —— 有重号时"第 42 条"指向哪一条是**猜**。
    """
    rows = [
        int(m.group(1))
        for ln in (_DOCS / "犯错指南.md").read_text(encoding="utf-8").splitlines()
        if (m := re.match(r"^\|\s*(\d+)\s*\|", ln))
    ]
    assert rows, "速查表一行都没有？"
    assert rows == list(range(1, len(rows) + 1)), f"速查表行号不连续：{rows[:12]}…（共 {len(rows)} 行）"


def test_guide_stays_a_door():
    """`犯错指南.md` 是"动代码前读这一份"，所以给它一个上限。

    它涨到 **4511 行**才被拆（2026-09-23），而"动代码前必读"的东西不该有 4511 行。
    设上限不是洁癖：**没有上限的"必读"文档，最终一定是没人读**。
    真踩了新坑请写进两份档案，或在这里加**一行**速查表。
    """
    lines = (_DOCS / "犯错指南.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 900, (
        f"犯错指南.md 已经 {len(lines)} 行 —— 它是门口，不是档案。"
        "完整复盘写 docs/犯错档案-微信.md 或 docs/犯错档案-工程.md，这里只留速查表那一行。"
    )


@pytest.mark.parametrize("name", ERR_DOCS)
def test_each_error_doc_declares_where_to_put_new_entries(name):
    """每份都得说清"新坑写哪" —— 否则下一个人会把它们塞回门口。"""
    text = (_DOCS / name).read_text(encoding="utf-8")
    assert "犯错档案" in text, f"{name} 没提另两份档案（新坑会写错地方）"
