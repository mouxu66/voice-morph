"""tools/audit_git_history.py 的单元测试 —— 「转公开前最后一道检查」的结论必须可信。

为什么这个工具值得单独钉住：它的输出直接决定**要不要重写 git 历史**，而重写历史是
不可逆的（force push + 已公开的提交哈希全变）。它自己判错，代价就是白改一遍历史，
或者更糟 —— 让人以为"改工作区没用"就干脆不改了。

三个职责：

1. **HEAD 判定按内容、不按 blob sha**（2026-09-14 修的语义缺陷）：
   文件重命名、或同一次提交里顺带改了别的行，blob 就换了，但命中的片段可能一个字
   都没动、还躺在 HEAD 里。老实现 `head[path] == sha` 会把这种误报成「仅历史」。
2. **素材名规则的分层**：内建只认机械痕迹（来源标记），**具体标题绝不写进仓库** ——
   否则"防泄漏的工具"自己成了泄漏源；标题只从**不入库**的 `.audit-materials.txt` 读。
3. **非对称降级**：`--allow-history-only` 只放行"仅历史 + 非凭据"，凭据类与
   「HEAD 仍含」不接受降级。

⚠️ 本文件里的"样本"一律用 `_pv()` 拼出来，**不写出完整样本** —— 否则审计工具会命中
本文件自己，规则就只得靠白名单绕开自己，那等于没有规则（跟 `test_check_secrets.py`
同一个坑）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"

# 一条只为测试存在的规则：命中片段好认、不依赖任何真实规则的正则
RULE: list[tuple[str, str]] = [(r"TOKEN-9", "测试规则")]


def _load(name: str):
    """按路径加载 tools/ 下的脚本（它们不是包，不能 `import`）。"""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ah():
    return _load("audit_git_history")


def _pv(*parts: str) -> str:
    """拼一个样本值（见模块 docstring：不能写成字面量）。"""
    return "".join(parts)


def _local_tokens(ah) -> list[str]:
    """本机素材清单里的条目（`#` 与空行除外）。新机器/CI 上可能为空。"""
    path = ROOT / ah.MATERIAL_LIST_FILE
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


# ---------------- _iter_hits：放行口径 ----------------

def test_iter_hits_finds_hardcoded_credential(ah):
    line = _pv("password = ", '"', "hunter2xyz", '"')
    hits = [m.group(0) for _desc, m in ah._iter_hits(line, ah.SECRET_RULES)]
    assert hits, f"应命中却放行：{line}"


@pytest.mark.parametrize("sample,why", [
    ('password = "<你的密码>"', "尖括号占位符"),
    ('api_key = "CHANGE_ME"', "约定俗成的占位符"),
    ("password = os.environ['VM_PW']", "从环境变量读，不是字面量"),
    ("token = process.env.API_TOKEN", "从环境变量读（TS）"),
    ("", "空行"),
])
def test_iter_hits_lets_placeholders_through(ah, sample, why):
    hits = list(ah._iter_hits(sample, ah.SECRET_RULES))
    assert not hits, f"应放行却命中（{why}）：{sample} → {hits}"


# ---------------- _material_rules：分层与字面匹配 ----------------

def test_builtin_material_rules_are_only_source_markers(ah):
    """内建规则里不得出现任何具体标题 —— 它是防泄漏的工具，自己不能是泄漏源。"""
    src = (TOOLS / "audit_git_history.py").read_text(encoding="utf-8")
    leaked = [t for t in _local_tokens(ah) if t in src]
    assert not leaked, f"audit_git_history.py 源码里出现了素材标题：{leaked}"


def test_test_file_itself_has_no_material_title(ah):
    """本测试文件同理：样本必须拼出来（这条测试就是那个纪律的机器版）。"""
    src = Path(__file__).read_text(encoding="utf-8")
    leaked = [t for t in _local_tokens(ah) if t in src]
    assert not leaked, f"本测试文件里出现了素材标题：{leaked}"


def test_tool_source_is_clean_under_its_own_rules(ah):
    """工具不能命中它自己 —— 否则历史清干净了它还是红的，那道门就废了。

    所以规则里的"机械痕迹"也得拼出来（同 `test_check_secrets.py` 的 `_pv()` 纪律）。
    """
    rules = [*ah.SECRET_RULES, *ah.PRIVACY_RULES, *ah.MATERIAL_RULES]
    src = (TOOLS / "audit_git_history.py").read_text(encoding="utf-8")
    hits = [(d, m.group(0)) for d, m in ah._iter_hits(src, rules)]
    assert not hits, f"审计工具源码命中了它自己的规则：{hits}"


def test_material_rules_loads_local_list(ah, tmp_path):
    (tmp_path / "m.txt").write_text("# 注释行\n\n  甲乙丙  \n", encoding="utf-8")
    rules = ah._material_rules(tmp_path / "m.txt")
    assert len(rules) == len(ah.MATERIAL_RULES) + 1
    assert [m.group(0) for _d, m in ah._iter_hits("xx甲乙丙xx", rules)] == ["甲乙丙"]


def test_material_rules_without_local_list_is_just_builtin(ah, tmp_path):
    rules = ah._material_rules(tmp_path / "不存在.txt")
    assert rules == list(ah.MATERIAL_RULES)


def test_material_list_matches_literally_not_as_regex(ah, tmp_path):
    """片名里带 `.` `(` `）` 很常见；按正则会静默失配，那还不如不查。"""
    (tmp_path / "m.txt").write_text("a.b\n", encoding="utf-8")
    rules = ah._material_rules(tmp_path / "m.txt")
    assert [m.group(0) for _d, m in ah._iter_hits("a.b", rules)] == ["a.b"]
    assert list(ah._iter_hits("axb", rules)) == []


# ---------------- _head_index / _tag：按内容判「HEAD 仍含」 ----------------

def test_head_index_maps_fragment_to_head_paths(ah):
    idx = ah._head_index({"keep.py": "s1", "clean.py": "s2"},
                         {"s1": "a TOKEN-9 b", "s2": "干净"}, RULE)
    assert idx == {"TOKEN-9": ["keep.py"]}


def test_head_index_tolerates_head_entries_without_content(ah):
    """HEAD 里的非 blob 条目（子模块）在 contents 里没有内容，不能因此崩。"""
    assert ah._head_index({"sub": "sX"}, {}, RULE) == {}


def test_tag_head_still_contains_when_same_file(ah):
    assert "HEAD 仍含" in ah._tag("a.py", "TOKEN-9", {"TOKEN-9": ["a.py"]})


def test_tag_points_at_the_other_head_file_when_renamed(ah):
    """核心回归：blob 换了、片段还在 HEAD 的**别的**文件里 → 不能报「仅历史」。"""
    tag = ah._tag("old_name.py", "TOKEN-9", {"TOKEN-9": ["new_name.py"]})
    assert "HEAD 另处仍含" in tag
    assert "new_name.py" in tag
    assert "仅历史" not in tag


def test_tag_history_only(ah):
    assert "仅历史" in ah._tag("a.py", "TOKEN-9", {})


def test_tag_marks_noise_dirs(ah):
    assert "第三方/依赖目录" in ah._tag("web/node_modules/x.js", "TOKEN-9", {})


def test_old_sha_equality_judgement_would_have_misjudged(ah):
    """把老判据写成反例钉住，免得有人"顺手改回去"。

    场景：`foo.py` 里有一行敏感内容，后来文件被重命名成 `bar.py`（内容没动）。
    命中所在的 blob 与 `head['foo.py']` 不同（那个路径压根没了），
    老判据 `head.get(path) == sha` 于是报「仅历史」—— 而它明明还挂在 HEAD 上。
    """
    hit_path, hit_sha = "foo.py", "sha_old"
    head = {"bar.py": "sha_new"}
    assert head.get(hit_path) != hit_sha                    # 老判据：→「仅历史」（错）
    idx = ah._head_index(head, {"sha_new": "a TOKEN-9 b"}, RULE)
    assert "HEAD 另处仍含" in ah._tag(hit_path, "TOKEN-9", idx)   # 新判据：→「仍含」（对）


# ---------------- 非对称降级 ----------------

@pytest.mark.parametrize("n_secret,n_head,expected", [
    (0, 0, True),      # 只有"仅历史 + 非凭据" → 可以降级
    (1, 0, False),     # 有凭据命中 → 不接受降级
    (0, 3, False),     # 「HEAD 仍含」→ 不接受降级（改文件就能解决）
    (1, 3, False),
])
def test_allow_history_only_is_asymmetric(ah, n_secret, n_head, expected):
    assert ah._waivable(n_secret, n_head) is expected


# ---------------- 未提交改动提示 ----------------

def test_dirty_note_silent_when_clean(ah):
    assert ah._dirty_note("") is None
    assert ah._dirty_note("\n  \n") is None


def test_dirty_note_counts_uncommitted_entries(ah):
    """刚改完工作区就复跑时，报告其实是旧的 —— 得有人提醒。"""
    note = ah._dirty_note(" M tools/audit_git_history.py\n?? 新文件.py\n")
    assert note and "2 处未提交改动" in note
    assert "已提交" in note
