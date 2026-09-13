"""tools/check_secrets.py 的单元测试 —— 2026-09-13 那个明文密码事故的机器门禁。

两个职责：

1. **规则自检**：正例必须命中、反例必须放行。规则一旦退化（比如放开成"标识符里
   出现敏感词就报"），这里先红。`scripts/test-ps1-lint.ps1` 对自己的合并行正则做
   同样的事，理由相同：**规则本身没人守，就会悄悄失效**。
2. **仓库自检**：扫一遍入库范围（含未跟踪但未忽略的新文件），发现硬编码凭据当场失败。
   `.githooks/pre-commit` 只按*文件名*拦密钥，内容里的密码正是从那个缺口进的仓库。

⚠️ 本文件里的"样本行"一律用 `_lit()` 拼出来，**不直接写出完整样本**——否则仓库自检
会命中本文件自己，规则就只得靠白名单绕开自己，那等于没有规则。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"


def _load(name: str):
    """按路径加载 tools/ 下的脚本（它们不是包，不能 `import`）。"""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cs():
    return _load("check_secrets")


def _lit(ident: str, value: str, sep: str = "=") -> str:
    """拼出 `ident = 'value'` 形式的一行样本（见模块 docstring 的白名单说明）。"""
    return f"{ident} {sep} {value!r}"


def _json(ident: str, value: str) -> str:
    """JSON 形态的样本（同样必须拼，不能写成字面量——否则被仓库自检命中）。"""
    return "{" + f"{ident!r}: {value!r}" + "}"


def _pv(*parts: str) -> str:
    """拼一个样本值。

    单独抽成函数不是洁癖：一个敏感标识符后面直接跟字符串字面量，这种行**本身就是**
    本规则要抓的模式——哪怕右边是拼接（两个字面量相加）也一样命中（等号后紧跟引号）。
    所以样本值必须从函数里出来，不能出现在赋值行上。

    （注意：写文档解释本规则时也别把那个模式原样敲出来，否则仓库自检会命中自己
    ——这条注释就是第一版被自己拦下来之后改的。）
    """
    return "".join(parts)


# ---------------- 正例：必须命中 ----------------

@pytest.mark.parametrize("line", [
    _lit("password", "hunter2xyz"),
    _lit("$CertPassword", "s3cret-pass"),
    _lit("$env:CSC_KEY_PASSWORD", "certpass123"),
    _lit("client_secret", "topsecretvalue"),
    _lit("authToken", "t0ken-value-9"),
    _lit("apiKey", "abcdef123456", sep=":"),
    _json("apikey", "k-abcdef12345"),
])
def test_flags_hardcoded_credentials(cs, line):
    hits = cs.scan_text(line, "sample.ps1")
    assert hits, f"应命中却放行：{line}"


def test_report_masks_the_value(cs):
    """命中信息里不得回显凭据原文——否则扫描日志自己成了泄漏源。"""
    secret = _pv("super", "-secret", "-value")
    (hit,) = cs.scan_text(_lit("password", secret), "s.ps1")
    assert secret not in hit
    assert "****" in hit


def test_reports_file_and_line(cs):
    (hit,) = cs.scan_text("\n\n" + _lit("password", "abc12345"), "sample.ps1")
    assert hit.startswith("sample.ps1:3:")


# ---------------- 反例：必须放行 ----------------

@pytest.mark.parametrize("line,why", [
    ("$env:CSC_KEY_PASSWORD = '<你的 pfx 密码>'", "尖括号占位符"),
    ('$CertPassword = "填你的 pfx 密码"', "中文占位符"),
    ('password = "CHANGE_ME"', "约定俗成的占位符"),
    ('clientSecret = "your-client-secret"', "your- 占位符"),
    ('apiKey = ""', "空值（前端未配置）"),
    ('password = os.environ["VM_PW"]', "从环境变量读，不是字面量"),
    ("token = process.env.API_TOKEN", "从环境变量读（TS）"),
    ('$env:PASSWORD = $secret', "变量赋值"),
    ('key = "Enter"', "裸 key 不是敏感词（否则全是噪音）"),
    ('bypass = "true"', "bypass 里含 pass，但不是密码"),
    ('secretName = "my-secret"', "敏感词不在末尾 → 普通配置名"),
    ('tokenExpiry = "3600s"', "敏感词不在末尾 → 普通配置名"),
    ('timeout = 30', "数字不是字符串字面量"),
    ('password = "ab"', "太短，当占位符放过"),
    ("# " + _lit("password", "hunter2xyz"), "Python/sh 注释行不拦"),
    ("// " + _lit("apiKey", "abcdef123456", sep=":"), "JS/TS 注释行不拦"),
    ('', "空行"),
])
def test_allows_placeholders_and_non_secrets(cs, line, why):
    hits = cs.scan_text(line, "sample.ps1")
    assert not hits, f"应放行却命中（{why}）：{line} → {hits}"


# ---------------- 仓库自检：真门禁 ----------------

def test_repo_has_no_hardcoded_credentials(cs):
    """整个入库范围不得有硬编码凭据。这是本测试存在的主要理由。"""
    try:
        files = cs.repo_files()
    except Exception as exc:                      # 没有 git（源码包解压等）→ 跳过，别假红
        pytest.skip(f"无法取入库文件列表：{exc}")
    hits = cs.scan_paths(files)
    assert not hits, (
        "入库范围发现硬编码凭据（规则与对策见 tools/check_secrets.py）：\n  "
        + "\n  ".join(hits)
    )
