#!/usr/bin/env python
"""audit_git_history — 扫「整个 git 历史」里的凭据与机器指纹，而不只是当前工作区。

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
    python tools/audit_git_history.py            # 扫全部可达对象（= 会被推送的内容）
    python tools/audit_git_history.py --verbose  # 打印每条命中的上下文

退出码：0 = 干净；1 = 有命中。

判定口径（宁可漏报也不误报，与 check_secrets.py 同源）：
    · 占位符 / 示例 / 求值表达式一律放行（`<你的密码>` / `os.environ` / `change_me` …）
    · 测试夹具（如 `certpass123`）靠"已知安全清单"放行，而不是靠正则放宽
    · 第三方依赖与参考仓库目录（node_modules / _ref / seed_vc …）单独标注，不静默跳过
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict

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


def _head_blobs() -> dict[str, str]:
    """HEAD 里 path -> blob sha。用来区分命中是「当前工作区还在」还是「仅历史残留」——
    前者改文件即可，后者只有 git filter-repo 能清。"""
    out: dict[str, str] = {}
    for line in _git("ls-tree", "-r", "HEAD").splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            out[path] = parts[2]      # 格式：<mode> blob <sha>\t<path>，sha 是第 3 段
    return out


def _snippet(text: str, start: int, end: int, verbose: bool) -> str:
    """命中片段。--verbose 时给出整行上下文，便于判断是真泄漏还是误报。"""
    if not verbose:
        return text[start:end][:100]
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    return text[line_start:line_end].strip()[:200]


def _tag(path: str, sha: str, head: dict[str, str]) -> str:
    marks = []
    if head.get(path) == sha:
        marks.append("HEAD 仍含")      # 命中内容就在 HEAD 里 → 改文件 + 提交即可
    else:
        marks.append("仅历史")         # 只在旧提交里 → 只有 filter-repo 能清
    if NOISE_PATH.search(path):
        marks.append("第三方/依赖目录")
    return "  [" + " · ".join(marks) + "]"


def main() -> int:
    ap = argparse.ArgumentParser(description="扫 git 全历史的凭据与机器指纹")
    ap.add_argument("--verbose", action="store_true", help="打印命中的上下文片段")
    args = ap.parse_args()

    blobs = _collect_blobs()
    contents = _read_blobs(blobs)
    head = _head_blobs()
    print(f"[审计] 可达对象 {len(blobs)} 个，其中 blob {len(contents)} 个"
          f"（范围 = 所有 ref 可达 = 推送后会公开的内容）")

    secrets: dict[str, list[str]] = defaultdict(list)
    privacy: dict[str, list[str]] = defaultdict(list)
    big: list[tuple[str, int]] = []

    for sha, text in contents.items():
        path = blobs.get(sha, "?")
        if len(text) > BIG_BLOB_BYTES:
            big.append((path, len(text)))
        for rule, desc in SECRET_RULES:
            for m in re.finditer(rule, text):
                frag = m.group(0)
                if PLACEHOLDER.search(frag) or KNOWN_SAFE.match(frag):
                    continue
                secrets[desc].append(
                    f"{path}{_tag(path, sha, head)} :: "
                    f"{_snippet(text, m.start(), m.end(), args.verbose)}"
                )
        for rule, desc in PRIVACY_RULES:
            for m in re.finditer(rule, text):
                frag = m.group(0)
                if PLACEHOLDER.search(frag) or KNOWN_SAFE.match(frag):
                    continue
                privacy[desc].append(
                    f"{path}{_tag(path, sha, head)} :: "
                    f"{_snippet(text, m.start(), m.end(), args.verbose)}"
                )

    def dump(title: str, table: dict[str, list[str]]) -> int:
        print(f"\n========== {title} ==========")
        if not table:
            print("  ✅ 无")
            return 0
        total = 0
        for desc, hits in table.items():
            uniq = sorted(set(hits))
            total += len(uniq)
            print(f"\n  [{desc}] {len(uniq)} 处")
            for h in uniq[:15]:
                print(f"    - {h}")
            if len(uniq) > 15:
                print(f"    … 另有 {len(uniq) - 15} 处")
        return total

    n_secret = dump("1. 凭据类命中", secrets)
    n_privacy = dump("2. 机器指纹 / 隐私命中", privacy)

    print("\n========== 3. 历史大文件（>1MB） ==========")
    if not big:
        print("  ✅ 无")
    else:
        for path, size in sorted(set(big), key=lambda x: -x[1])[:15]:
            print(f"  {size / 1048576:8.2f} MB  {path}")

    print()
    if n_secret or n_privacy:
        print(f"RESULT: FAIL（凭据 {n_secret} 处 / 隐私 {n_privacy} 处）")
        print("处置：先清工作区（tools/check_secrets.py 会拦新的），"
              "历史里的需 git filter-repo 重写 —— 见 docs/开源前待办清单-2026-09-12.md §3.1")
        return 1
    print("RESULT: PASS（历史干净）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
