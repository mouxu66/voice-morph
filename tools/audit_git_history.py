#!/usr/bin/env python
"""audit_git_history — 扫「整个 git 历史」里的凭据、机器指纹与第三方素材名，而不只是当前工作区。

为什么需要它（2026-09-13 开源前审计）：
    `tools/check_secrets.py` 扫的是**工作区**（含未跟踪的新文件），它能防"新写进去的
    明文"，但防不住"**曾经**写进去、后来删掉"的东西 —— 那些值仍然躺在 `.git` 里，
    一旦仓库转公开，任何人 `git log -p` 就能捞出来。

    本项目已经踩过一次：自签证书密码明文曾进过 `scripts/release.ps1` 与
    `docs/release-sop.md`，工作区清干净了，历史里还在，最后靠 `git filter-repo`
    重写才彻底解决（详见 `docs/开源前待办清单-2026-09-12.md` §3.1）。
    本工具就是那次审计沉淀下来的，用来在**转公开前 / 加远端前**做最后一道检查。

与 check_secrets.py 的分工：
    check_secrets.py  → 入库内容门禁，接在 pre-commit 与 pytest 上，管"现在"
    本工具            → 历史体检，人工在关键节点跑，管"过去"

用法：
    python tools/audit_git_history.py                    # 扫全部可达对象（= 会被推送的内容）
    python tools/audit_git_history.py --verbose          # 打印每条命中的上下文
    python tools/audit_git_history.py --allow-history-only
                                                          # 「改工作区但不再重写历史」时用（见下）

退出码：0 = 干净；1 = 有命中（--allow-history-only 下仅当"HEAD 仍含"或凭据类仍有命中才 1）。

判定口径（宁可漏报也不误报，与 check_secrets.py 同源）：
    · 占位符 / 示例 / 求值表达式一律放行（`<你的密码>` / `os.environ` / `change_me` …）
    · 测试夹具（如 `certpass123`）靠"已知安全清单"放行，而不是靠正则放宽
    · 第三方依赖与参考仓库目录（node_modules / _ref / seed_vc …）单独标注，不静默跳过
    · 命中按「HEAD 仍含 / 仅历史」分类，判据是**内容**而不是 blob sha 相等（见 _head_index）——
      两者的处置完全不同：前者改文件 + 提交即可，后者只有 filter-repo 能清
    · 第三方素材名（B 站片名等）单列一类：内建规则只认**机械痕迹**（来源标记），
      一条具体标题都不写进本文件 —— 否则"防泄漏的工具"自己成了泄漏源；
      标题改从**不入库**的 `.audit-materials.txt` 读（见 .gitignore 与 _material_rules）

⚠️ 本报告里的命中片段是**原文**，别把输出直接贴到公开 issue / PR 里 —— 那等于二次泄漏。

--allow-history-only 的**非对称**语义（与 conftest 的资源探测同一套思路）：
    · 「仅历史」的隐私 / 素材名 → 降级为警告，不改退出码（历史里只剩片名，风险有限）
    · 凭据类（任何位置）与「HEAD 仍含」（任何类别）→ **照样失败**，不接受降级
    理由是"能降级的东西"必须一眼看得出边界；把凭据也一起降级，这道门就废了。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

# ---------------------------------------------------------------------------
# 凭据类：命中即高危
# ---------------------------------------------------------------------------
SECRET_RULES: list[tuple[str, str]] = [
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥文件头"),
    (r"\bghp_[A-Za-z0-9]{20,}", "GitHub PAT (classic)"),
    (r"\bgithub_pat_[A-Za-z0-9_]{20,}", "GitHub PAT (fine-grained)"),
    (r"\b(?:gho|ghs|ghu)_[A-Za-z0-9]{20,}", "GitHub OAuth/App token"),
    (r"\bsk-[A-Za-z0-9]{20,}", "OpenAI 风格 API key"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS Access Key ID"),
    (r"\bAIza[0-9A-Za-z_\-]{30,}", "Google API key"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"\b\d{9,10}:AA[A-Za-z0-9_\-]{30,}", "Telegram bot token"),
    (
        r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key)"
        r"\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']",
        "凭据字面量赋值",
    ),
    (r"密码\s*[:=]\s*[\"'][^\"'\s]{4,}[\"']", "密码字面量（中文）"),
]

# ---------------------------------------------------------------------------
# 机器指纹 / 隐私类
# ---------------------------------------------------------------------------
PRIVACY_RULES: list[tuple[str, str]] = [
    (r"[A-Za-z]:\\+Users\\+[A-Za-z0-9_.\-]+", "Windows 家目录含用户名"),
    (r"/Users/[A-Za-z0-9_.\-]+/", "macOS 家目录路径"),
    (r"/home/[A-Za-z0-9_.\-]+/", "Linux 家目录路径"),
    (r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", "疑似 MAC 地址"),
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "疑似手机号"),
    (
        r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
        r"(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)",
        "疑似身份证号",
    ),
]

# ---------------------------------------------------------------------------
# 第三方素材名（issue #3）：实验脚本里曾写死"从 B 站下载的片名"。
# 这类字符串既不是凭据也不是机器指纹，但同样不该随开源仓库公开。
#
# 内建规则**只认机械痕迹**（来源标记），一条具体标题都不含 —— 理由见模块 docstring。
# 具体标题从**不入库**的 `.audit-materials.txt` 读，见 _material_rules()。
#
# 2026-09-14 收窄：原来"出现平台名即命中"，于是一次真实误报 —— IndexTTS2 的
# **许可名**与"B 站 Index 团队"这类**归因**全被报成"第三方素材名泄漏"。噪音之外还有
# 更坏的后果：它会逼着人把许可名从合规文档里删掉，而那是**唯一必须写出它的地方**
# （`THIRD_PARTY_NOTICES.md` / 选型报告 §6.2）。
# 真正的目标是**下载文件名**里的来源标记，因此要求标记处于文件名语境：
#   · 紧邻媒体扩展名（`…_<标记>_<标记>.mp4` 这种下载命名）
#   · 或被 `_` / `-` 包夹（`…_<标记>-20260827-ne4zlou33r…`）
# 光提平台名不算泄漏 —— 平台是公开信息，敏感的从来是**片名**；片名由本地清单按字面兜底。
#
# 标记本身也得**拼出来**：直接写成完整字符串，规则源码会命中它自己 ——
# 于是历史清干净了这个工具还是红的，那道门就废了。
# （跟 `test_check_secrets.py` 里 `_pv()` 那条纪律是同一件事。）
_MEDIA_EXT = r"(?:mp4|mkv|flv|webm|mov|avi|mp3|wav|m4a|aac|srt|ass|danmaku|xml)"
_BILI_CJK = "哔哩" + "哔"
_BILI_LATIN = "bili" + "bili"
_MARK = rf"(?:{_BILI_CJK}|{_BILI_LATIN})"
MATERIAL_RULES: list[tuple[str, str]] = [
    (rf"(?i){_MARK}[^\s]{{0,40}}\.{_MEDIA_EXT}", "下载文件名里的来源标记（标记后跟媒体扩展名）"),
    (rf"(?i)[_\-]{_MARK}[_\-]", "下载文件名里的来源标记（标记被下划线或连字符包夹）"),
]

MATERIAL_LIST_FILE = ".audit-materials.txt"

# 占位符 / 示例 / 求值表达式 —— 命中片段里出现任一即放行
PLACEHOLDER = re.compile(
    r"(?i)<[^>]*>|你的|填|改为|换成|xxx|yyy|zzz|change[_-]?me|your[_-]|"
    r"example|sample|dummy|placeholder|redacted|todo|\*\*\*|已移除|"
    r"os\.environ|process\.env|getenv|\$env:|\$\{|"
    r"Users[/\\]+(?:Public|user|username|yourname|me|%USERNAME%|<|\.\.\.|…)"
)

# 已知安全的字面量（测试夹具 / 协议常量）—— 用白名单而非放宽正则，
# 否则"能拦真密码"的能力会被一起削掉
KNOWN_SAFE = re.compile(
    r"(?i)^(?:certpass123|s3cret-pass|null|file://|undefined|true|false|"
    r"127\.0\.0\.1|localhost|0\.0\.0\.0)$"
)

# 第三方依赖 / 参考仓库 / 技能副本：命中照样报，但标注来源，方便判断
NOISE_PATH = re.compile(
    r"(^|/)(node_modules|\.venv|venv|venv312|site-packages|_ref|seed_vc|"
    r"seed_vc_repo|tts_trial/Qwen3-TTS|agents/skills|\.codebuddy/skills|"
    r"\.workbuddy/skills|web/release2?)/"
)

BIG_BLOB_BYTES = 1024 * 1024  # GitHub 建议单文件 < 50MB，>1MB 就该问一句


def _git(*args: str, input_text: str | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        errors="replace",
        input=input_text,
    )
    if proc.returncode != 0 and args[0] != "cat-file":
        print(f"[git {' '.join(args)}] {proc.stderr.strip()}", file=sys.stderr)
    return proc.stdout


def _git_bytes(*args: str, input_bytes: bytes) -> bytes:
    """二进制版：`cat-file --batch` 的输出头里 size 是**字节**数，
    必须按字节切片，否则遇到中文等多字节内容就会错位、后面全部解析失败
    （2026-09-13 踩过：按字符切片时 1187 个 blob 只解析出 1 个，
    于是"历史干净"这个结论其实建立在空样本上）。"""
    proc = subprocess.run(
        ["git", *args], capture_output=True, input=input_bytes
    )
    if proc.returncode != 0:
        print(f"[git {' '.join(args)}] {proc.stderr.decode('utf-8', 'replace').strip()}",
              file=sys.stderr)
    return proc.stdout


def _collect_blobs() -> dict[str, str]:
    """返回 {blob_sha: 首次出现的路径}，范围 = 所有 ref 可达的对象（= 会被推送的）。"""
    blobs: dict[str, str] = {}
    for line in _git("rev-list", "--objects", "--all").splitlines():
        parts = line.split(" ", 1)
        sha = parts[0]
        path = parts[1] if len(parts) > 1 else ""
        if path and sha not in blobs:
            blobs[sha] = path
    return blobs


def _read_blobs(blobs: dict[str, str]) -> dict[str, str]:
    shas = list(blobs)
    check = _git("cat-file", "--batch-check", input_text="\n".join(shas))
    only_blobs = [ln.split()[0] for ln in check.splitlines() if len(ln.split()) == 3
                  and ln.split()[1] == "blob"]

    raw = _git_bytes("cat-file", "--batch",
                     input_bytes="\n".join(only_blobs).encode())
    out: dict[str, str] = {}
    idx = 0
    parsed = 0
    while idx < len(raw):
        nl = raw.find(b"\n", idx)
        if nl < 0:
            break
        header = raw[idx:nl].decode("utf-8", "replace").split()
        if len(header) != 3 or header[1] != "blob":
            break
        size = int(header[2])                      # 字节数 —— 必须按字节切片
        body = raw[nl + 1: nl + 1 + size]
        out[header[0]] = body.decode("utf-8", "replace")
        parsed += 1
        idx = nl + 1 + size + 1

    # 解析完整性自检：漏解析会让"无命中"变成假绿，这里直接报出来
    if parsed != len(only_blobs):
        print(f"[审计] ⚠️ 解析不完整：{len(only_blobs)} 个 blob 只解析出 {parsed} 个，"
              f"结论不可信", file=sys.stderr)
    return out


def _iter_hits(text: str, rules: list[tuple[str, str]]) -> Iterator[tuple[str, re.Match]]:
    """按规则扫一段文本，产出 (规则描述, 匹配对象)。

    放行口径（占位符 / 已知安全值）集中在这里，是为了保证**历史里扫**与**HEAD 里扫**
    用的是同一把尺子 —— 两边判据只要差一点，「HEAD 仍含」这个标签就不可信
    （要么把已经清掉的报成仍在，要么反过来漏报）。
    """
    for rule, desc in rules:
        for m in re.finditer(rule, text):
            frag = m.group(0)
            if PLACEHOLDER.search(frag) or KNOWN_SAFE.match(frag):
                continue
            yield desc, m


def _material_rules(list_path: Path | None = None) -> list[tuple[str, str]]:
    """第三方素材名规则 = 内建（只认来源标记）+ 本地清单 `.audit-materials.txt`。

    为什么标题不进仓库：本工具是**防泄漏**用的，把"某个片名"写进它自己，
    等于泄漏源换了个位置、还更隐蔽。所以标题只从**不入库**的本地清单读（一行一个子串）；
    新机器 / CI 上没有这个文件，也就自然没有这些规则 —— 那里本来也没有这些素材。

    清单里的条目按**字面**匹配（`re.escape`），不是正则：片名里带 `.` `(` `）` 很常见，
    当正则会静默失配，那还不如不查。

    `list_path` 只为测试留的口子（默认仓库根下的 `MATERIAL_LIST_FILE`）。
    """
    rules = list(MATERIAL_RULES)
    local = list_path or (Path(__file__).resolve().parents[1] / MATERIAL_LIST_FILE)
    if not local.exists():
        return rules
    for raw in local.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        rules.append((re.escape(line), f"本地素材清单（{MATERIAL_LIST_FILE}）"))
    return rules


def _head_blobs() -> dict[str, str]:
    """HEAD 里 path -> blob sha。"""
    out: dict[str, str] = {}
    for line in _git("ls-tree", "-r", "HEAD").splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            out[path] = parts[2]      # 格式：<mode> blob <sha>\t<path>，sha 是第 3 段
    return out


def _head_index(head: dict[str, str], contents: dict[str, str],
                all_rules: list[tuple[str, str]]) -> dict[str, list[str]]:
    """扫一遍 HEAD 的内容 → {命中片段: [含它的 HEAD 路径]}。

    为什么不能拿 `head[path] == blob_sha` 判「HEAD 仍含」（2026-09-14 修）：
    blob sha 是**整份文件**的哈希，文件一被重命名、或在同一次提交里顺带改了别的行，
    sha 就变了 —— 可那个片段可能一个字都没动、还躺在 HEAD 里。
    按 sha 相等判会把它误报成「仅历史」，而「仅历史」的处置是 filter-repo 重写历史：
    结论错了，代价是白改一遍历史，或者更糟 —— 以为"改工作区没用"就干脆不改了。
    片段是**内容**层面的东西，就用内容层面判。
    """
    index: dict[str, list[str]] = defaultdict(list)
    for path, sha in head.items():
        text = contents.get(sha)
        if text is None:                 # HEAD 里的非 blob 条目（子模块等）没有内容可扫
            continue
        for _desc, m in _iter_hits(text, all_rules):
            frag = m.group(0)
            if path not in index[frag]:
                index[frag].append(path)
    return index


def _snippet(text: str, start: int, end: int, verbose: bool) -> str:
    """命中片段。--verbose 时给出整行上下文，便于判断是真泄漏还是误报。"""
    if not verbose:
        return text[start:end][:100]
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    return text[line_start:line_end].strip()[:200]


def _tag(path: str, frag: str, head_index: dict[str, list[str]]) -> str:
    """标注这条命中是「HEAD 里还看得到」还是「只在历史里」——
    前者改文件 + 提交即可，后者只有 git filter-repo 能清。

    注意第二档：命中所在的那个**历史 blob** 已经不在了，但同样的片段还在 HEAD 的
    **别的文件**里（重命名、或那段文字被复制到别处）。它照样是"公开可见"，
    所以归到"改工作区"一类，只是要改的文件不是它自己。
    """
    marks = []
    where = head_index.get(frag)
    if where:
        if path in where:
            marks.append("HEAD 仍含")
        else:
            more = f" 等 {len(where)} 处" if len(where) > 1 else ""
            marks.append(f"HEAD 另处仍含 → {where[0]}{more}")
    else:
        marks.append("仅历史")
    if NOISE_PATH.search(path):
        marks.append("第三方/依赖目录")
    return "  [" + " · ".join(marks) + "]"


def _section_rules(mat_rules: list[tuple[str, str]]) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """(章节键, 章节标题, 规则表)。扫与打印共用这一份 —— 两边各写一遍就会漂。"""
    return [
        ("secret", "1. 凭据类命中", SECRET_RULES),
        ("privacy", "2. 机器指纹 / 隐私命中", PRIVACY_RULES),
        ("material", "3. 第三方素材名命中", mat_rules),
    ]


def _collect_hits(contents: dict[str, str], blobs: dict[str, str],
                  head_index: dict[str, list[str]],
                  sections: list[tuple[str, str, list[tuple[str, str]]]],
                  verbose: bool) -> dict[tuple[str, str, str, str], tuple[str, bool]]:
    """扫全部可达 blob → `{(章节键, 规则描述, 路径, 命中片段): (展示行, 是否 HEAD 仍含)}`。

    **去重键刻意不含展示行**：早先是拿整行文本去重，于是 `--verbose` 一开、上下文变长、
    条数就跟着变 —— 同一份仓库能给出 110 处 / 74 处两个数，文档里引用哪个都对不上
    （2026-09-14 修）。计数必须与显示模式无关，否则"基线"这种东西没法写。

    计数单位是「**文件 × 命中片段**」，不是出现次数：同一文件里同一片段出现多次算一处。
    这样"要动的文件"清单才精确，也不受"某行里出现几次"影响。
    """
    hits: dict[tuple[str, str, str, str], tuple[str, bool]] = {}
    for sha, text in contents.items():
        path = blobs.get(sha, "?")
        for key, _title, rules in sections:
            for desc, m in _iter_hits(text, rules):
                frag = m.group(0)
                ident = (key, desc, path, frag)
                line = (f"{path}{_tag(path, frag, head_index)} :: "
                        f"{_snippet(text, m.start(), m.end(), verbose)}")
                in_head = frag in head_index
                if ident in hits:               # 同一片段可能出现在多个 blob 里
                    in_head = in_head or hits[ident][1]
                hits[ident] = (line, in_head)
    return hits


def _dirty_note(status_porcelain: str) -> str | None:
    """工作区有未提交改动时给一句提示 —— 本工具扫的是**已提交**的内容。

    2026-09-14 踩到：刚改完工作区就复跑，报告仍是旧结论（满屏「HEAD 仍含」），
    差点据此以为"改了没用"。一行提示就能省掉这次困惑。
    """
    lines = [ln for ln in status_porcelain.splitlines() if ln.strip()]
    if not lines:
        return None
    return (f"[审计] ⚠️ 工作区有 {len(lines)} 处未提交改动 —— 本工具扫的是**已提交**的内容，"
            f"「HEAD 仍含」可能落后于你刚改的文件；提交后再跑一次结论才准。")


def _waivable(n_secret: int, n_head: int) -> bool:
    """`--allow-history-only` 能不能放行 —— 这套**非对称**规则值得单独钉住：

    · 凭据类（`n_secret`）无论落在 HEAD 还是历史里，都不接受降级；
    · 「HEAD 仍含」（`n_head`）也不接受 —— 那本来改个文件就能解决。
    只有"仅历史 + 非凭据"才降级。抽成函数是为了让测试能直接断言这三条边界。
    """
    return n_secret == 0 and n_head == 0


def main() -> int:
    ap = argparse.ArgumentParser(description="扫 git 全历史的凭据、机器指纹与第三方素材名")
    ap.add_argument("--verbose", action="store_true", help="打印命中的上下文片段")
    ap.add_argument("--allow-history-only", action="store_true",
                    help="「仅历史」的隐私/素材名命中降级为警告（凭据类与 HEAD 仍含的照样失败）")
    args = ap.parse_args()

    blobs = _collect_blobs()
    contents = _read_blobs(blobs)
    head = _head_blobs()
    mat_rules = _material_rules()
    all_rules = [*SECRET_RULES, *PRIVACY_RULES, *mat_rules]
    head_index = _head_index(head, contents, all_rules)
    print(f"[审计] 可达对象 {len(blobs)} 个，其中 blob {len(contents)} 个"
          f"（范围 = 所有 ref 可达 = 推送后会公开的内容）")
    print(f"[审计] HEAD 文件 {len(head)} 个；素材名规则 {len(mat_rules)} 条"
          f"（内建 {len(MATERIAL_RULES)} + 本地清单 {len(mat_rules) - len(MATERIAL_RULES)}）")
    note = _dirty_note(_git("status", "--porcelain"))
    if note:
        print(note)

    sections = _section_rules(mat_rules)
    hits = _collect_hits(contents, blobs, head_index, sections, args.verbose)
    key_of = {title: key for key, title, _rules in sections}

    big: list[tuple[str, int]] = []
    for sha, text in contents.items():
        if len(text) > BIG_BLOB_BYTES:
            big.append((blobs.get(sha, "?"), len(text)))
    need_edit: set[str] = set()        # HEAD 里还含命中的文件 —— 这才是"要动手改"的清单
    for (_key, _desc, _path, frag), (_line, in_head) in hits.items():
        if in_head:
            need_edit.update(head_index.get(frag, ()))

    def dump(title: str) -> tuple[int, int]:
        """打印一节，返回（唯一命中数, 其中「HEAD 仍含」的处数）。

        计数按**去重键**（与 `--verbose` 无关）；打印时对展示行再取一次 `set` ——
        只有"同一行里命中同一规则多次"才会让两者不等，那种情况展示行本来也一样。
        """
        rows = {k: v for k, v in hits.items() if k[0] == key_of[title]}
        print(f"\n========== {title} ==========")
        if not rows:
            print("  ✅ 无")
            return 0, 0
        by_desc: dict[str, list[tuple[str, bool]]] = defaultdict(list)
        for (_key, desc, _path, _frag), val in rows.items():
            by_desc[desc].append(val)
        total = head_total = 0
        for desc, items in by_desc.items():
            total += len(items)
            head_total += sum(1 for _line, in_head in items if in_head)
            uniq = sorted(set(items))
            print(f"\n  [{desc}] {len(items)} 处")
            for line, _in_head in uniq[:15]:
                print(f"    - {line}")
            if len(uniq) > 15:
                print(f"    … 另有 {len(uniq) - 15} 处")
        return total, head_total

    n_secret, h_secret = dump("1. 凭据类命中")
    n_privacy, h_privacy = dump("2. 机器指纹 / 隐私命中")
    n_material, h_material = dump("3. 第三方素材名命中")

    print("\n========== 4. 历史大文件（>1MB） ==========")
    if not big:
        print("  ✅ 无")
    else:
        for path, size in sorted(set(big), key=lambda x: -x[1])[:15]:
            print(f"  {size / 1048576:8.2f} MB  {path}")

    n_total = n_secret + n_privacy + n_material
    n_head = h_secret + h_privacy + h_material
    n_hist = n_total - n_head

    print()
    if not n_total:
        print("RESULT: PASS（历史干净）")
        return 0

    print(f"RESULT: FAIL（凭据 {n_secret} 处 / 隐私 {n_privacy} 处 / 素材名 {n_material} 处）")
    print(f"  · HEAD 仍含 {n_head} 处 → 改工作区 + 提交即可。要动的文件：")
    for p in sorted(need_edit)[:15]:
        print(f"      {p}")
    if len(need_edit) > 15:
        print(f"      … 另有 {len(need_edit) - 15} 个")
    print(f"  · 仅历史   {n_hist} 处 → 只有 git filter-repo 重写能清，"
          f"见 docs/开源前待办清单-2026-09-12.md §3.1")

    if args.allow_history_only:
        # 非对称降级：凭据类不接受降级，HEAD 仍含的也不接受（见模块 docstring）
        if _waivable(n_secret, n_head):
            print(f"RESULT: PASS --allow-history-only（{n_hist} 处仅历史命中已降级为警告；"
                  f"凭据 0 处、HEAD 仍含 0 处）")
            return 0
        print("RESULT: FAIL（--allow-history-only 不覆盖："
              f"凭据 {n_secret} 处 / HEAD 仍含 {n_head} 处）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
