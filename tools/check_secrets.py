"""check_secrets — 拦「把凭据字面量写进代码/脚本/文档」。

背景（2026-09-13 开源前审计）：自签证书私钥密码曾被明文写进 3 处入库文件，
其中 `scripts/release.ps1` 那处还是 Warn 文案 —— 等于每次发版都把这个密码
打印进控制台/CI 日志。而 `.githooks/pre-commit` 原本只按**文件名**拦密钥
（.env / *.pfx / id_rsa …），对"内容里的密码"完全无感，所以它一路进了仓库。

本模块补的就是这一层：按**内容**拦。两条门禁都接它：
  · `m2_server/tests/test_check_secrets.py` —— pytest 里跑，CI 与 pre-commit --fast 都覆盖
  · 命令行 —— 人肉排查用：`python tools/check_secrets.py`

判定规则（宁可漏报也不误报：噪音会让人开始无视这个入口）：

    标识符**以**敏感词结尾（password/certpassword/secret/token/apiKey/…），
    且等号或冒号右边是**字符串字面量**（不是 env 读取、不是变量）
    且这个字面量不是占位符 → 命中。

    `secretName = "x"` / `tokenExpiry = "1h"` 这类**普通配置**不会命中：敏感词必须
    落在标识符末尾，靠这条把噪音压住（否则没人会继续看这个入口的输出）。

占位符放行清单见 `_is_placeholder()`：`<你的密码>` / `填你的密码` / `xxx` /
`CHANGE_ME` / `your-password` / `REDACTED` 等写法都不会被拦。

用法：

    python tools/check_secrets.py                # 扫「入库范围」（含未跟踪但未忽略的新文件）
    python tools/check_secrets.py --staged       # 只扫 git 暂存区里新增/修改的文件

退出码：0 = 无命中；1 = 有命中（逐条打印 file:line:列）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 敏感词的 token 集合。**不含裸 `key`**——它太容易撞上 `key = "Enter"` 这类噪音。
SENSITIVE_TOKENS = {
    "password", "passwd", "passphrase", "pwd",
    "certpassword", "certpass",
    "secret", "clientsecret",
    "token", "apikey", "apisecret", "accesskey", "privatekey",
}

# 明显是占位符/提示的写法，一律放行。大小写不敏感。
PLACEHOLDER_HINTS = (
    "<", ">",              # <你的 pfx 密码>
    "你的", "你 pfx", "填", "改为", "换成",
    "xxx", "yyy", "zzz",
    "change_me", "change-me", "changeme",
    "your-", "your_", "yourpassword",
    "example", "sample", "dummy", "placeholder", "redacted", "todo",
    "***", "已移除",
)

# 右边是「求值」而不是字面量 → 交给别的机制（env / 密钥管理器）管，不算硬编码。
DYNAMIC_HINTS = ("$env:", "${", "os.environ", "process.env", "getenv", "env::var")

# `ident = 'literal'` / `"ident": "literal"` / `$env:IDENT = 'literal'`
ASSIGN_RE = re.compile(
    r"""["']?(?P<ident>[A-Za-z_$][\w$:.\-\[\]]*)["']?\s*[:=]\s*(?P<q>['"])(?P<val>[^'"]*)(?P=q)"""
)

TEXT_SUFFIXES = {
    ".py", ".ps1", ".psm1", ".sh", ".cjs", ".mjs", ".js", ".jsx", ".ts", ".tsx",
    ".md", ".txt", ".json", ".yml", ".yaml", ".toml", ".cfg", ".ini", ".example",
}

# 不扫的目录/文件：依赖、产物、二进制、以及"本条规则自己的用例"。
SKIP_DIR_PARTS = {
    ".git", "node_modules", "__pycache__", ".venv", ".venv-ci", ".ruff_cache",
    ".pytest_cache", "media", "outputs", "tts_models", "pretrained_models",
    "models", "voice-morph-desktop", ".asar_tmp", ".workbuddy", ".freebuff",
    ".agents", ".codebuddy", ".trae", "agents", "experiments", "tts_trial",
    "seed_vc", "seed_vc_repo", "_ref", "_trash_20260831", "web/dist",
    "m2_server/assets",
}
SKIP_NAMES = {"package-lock.json"}


def _tokens(ident: str) -> list[str]:
    """把标识符切成小写 token，同时兼顾 snake_case / camelCase / 点号路径。"""
    name = ident.lstrip("$").replace("$env:", "")
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)   # apiKey → api Key
    parts = re.split(r"[^A-Za-z0-9]+", name)
    return [p.lower() for p in parts if p]


def _is_sensitive_ident(ident: str) -> bool:
    """标识符是不是在表达"这是一个凭据"。

    只认两种情况，不多认——**首个 token** 命中不算，否则 `secretName = "x"`、
    `tokenExpiry = "1h"` 这类普通配置会全被拦成噪音：
      · 末尾 token 是敏感词：`password`、`$CertPassword`、`CSC_KEY_PASSWORD`、`authToken`
      · 整个标识符拼起来是敏感词（吃 camelCase）：`apiKey`、`clientSecret`
    """
    toks = _tokens(ident)
    if not toks:
        return False
    return toks[-1] in SENSITIVE_TOKENS or "".join(toks) in SENSITIVE_TOKENS


def _is_placeholder(val: str) -> bool:
    low = val.strip().lower()
    if len(low) < 3:                       # 空值 / 单字符：不是凭据
        return True
    if any(h in low for h in DYNAMIC_HINTS):
        return True
    return any(h in low for h in PLACEHOLDER_HINTS)


def scan_text(text: str, label: str = "<text>") -> list[str]:
    """扫一段文本，返回 `label:行号:列:说明` 形式的命中列表。"""
    hits: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "*")):
            continue                       # 纯注释行不拦（否则本文件自己的说明会命中）
        for m in ASSIGN_RE.finditer(line):
            ident, val = m.group("ident"), m.group("val")
            if not _is_sensitive_ident(ident) or _is_placeholder(val):
                continue
            hits.append(
                f"{label}:{lineno}:{m.start('val') + 1}: 疑似硬编码凭据 "
                f"{ident.strip()} = {'*' * min(len(val), 8)}（值已打码）"
            )
    return hits


def _git(*args: str) -> str:
    """跑 git 并取 stdout。

    必须显式 `encoding="utf-8"`：`text=True` 会用**本机 locale**（本机是 GBK）解码，
    而 git 输出里的中文文件名是 UTF-8 → 直接 UnicodeDecodeError（2026-09-13 实测踩到，
    同类坑见 docs/犯错指南.md）。errors="replace" 只求不炸，文件名里的乱码不影响判定。
    """
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, check=True,
        encoding="utf-8", errors="replace",
    ).stdout


def repo_files() -> list[Path]:
    """入库范围：已跟踪 + 未跟踪但未被忽略的文件（新文件在提交前也能被拦）。"""
    out = _git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return [ROOT / p for p in out.split("\0") if p]


def staged_files() -> list[Path]:
    """暂存区里新增/修改的文件（pre-commit 场景）。"""
    out = _git("diff", "--cached", "--name-only", "--diff-filter=ACM", "-z")
    return [ROOT / p for p in out.split("\0") if p and (ROOT / p).is_file()]


def scan_paths(paths) -> list[str]:
    hits: list[str] = []
    for path in sorted(set(paths)):
        if path.name in SKIP_NAMES or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in SKIP_DIR_PARTS for part in path.relative_to(ROOT).parts[:-1]):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits.extend(scan_text(text, path.relative_to(ROOT).as_posix()))
    return hits


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    staged = "--staged" in argv
    try:
        targets = staged_files() if staged else repo_files()
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("[check_secrets] 取文件列表失败（不在 git 仓库里？）", file=sys.stderr)
        return 2
    hits = scan_paths(targets)
    where = "暂存区" if staged else "入库范围"
    if not hits:
        print(f"[check_secrets] {where} {len(targets)} 个文件，未发现硬编码凭据。")
        return 0
    print(f"[check_secrets] {where}发现 {len(hits)} 处疑似硬编码凭据：\n", file=sys.stderr)
    for h in hits:
        print(f"  {h}", file=sys.stderr)
    print(
        "\n对策：改成从环境变量读（如 `$env:CSC_KEY_PASSWORD` / `os.environ[...]`），"
        "文档里一律写 `<你的 xxx>` 这类占位符。\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
