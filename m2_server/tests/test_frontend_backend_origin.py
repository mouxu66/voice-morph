"""守卫：前端后端地址只允许在 web/src/api/client.ts 里出现一次。

背景（2026-09-13 开源前审计 P2-5.5）：
    `http://127.0.0.1:8000` 原本硬编码在 4 处（client.ts ×2、useEffects.ts、
    useWorkshop.ts），各自还复制了一份"DEV ? 相对 : 绝对"的判定。复制出来的判定
    已经走偏：`mediaUrl` 写的是 `import.meta.env.DEV ? path : 绝对地址`，而局域网
    用户（手机打开 http://<PC-IP>:8000）跑的是构建产物、DEV 为 false —— 于是手机拿到
    `http://127.0.0.1:8000/...`，指向手机自己，必然失败。判定依据应该是"页面协议"。

    收敛到 `client.ts` 的 `backendPrefix()` 之后，这条测试负责防止它再长回去。

口径：只查字面量主机地址，不管注释（注释里出现地址是正常的，比如说明文字）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "web" / "src"
OWNER = "api/client.ts"

# 后端主机字面量的几种写法（局域网默认 8000 端口）
PATTERNS = ("127.0.0.1:8000", "localhost:8000")

# 前端源码目录不在（例如只装了后端做部署）时跳过，而不是误判为通过
if not SRC.is_dir():
    pytest.skip(f"未找到前端源码目录：{SRC}", allow_module_level=True)


def _iter_source_files():
    for p in SRC.rglob("*"):
        if p.suffix in (".ts", ".tsx") and p.is_file():
            yield p


def _strip_comments(text: str) -> str:
    """粗暴剥掉行注释与块注释 —— 只用来判断"代码里"有没有硬编码。

    不追求解析器级正确：注释里出现主机地址是允许的（说明文字），
    而字符串字面量里的地址必须留下。误剥（例如 URL 里的 //）会漏报，
    所以下面还有一条"必须存在该常量"的反向断言兜底。
    """
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
            continue
        out.append(line)
    return "\n".join(out)


def test_backend_host_literal_only_in_client():
    offenders: list[str] = []
    for p in _iter_source_files():
        rel = p.relative_to(SRC).as_posix()
        if rel == OWNER:
            continue
        code = _strip_comments(p.read_text(encoding="utf-8", errors="replace"))
        for pat in PATTERNS:
            if pat in code:
                offenders.append(f"{rel}: {pat}")
    assert (
        not offenders
    ), "后端地址字面量应只存在于 web/src/api/client.ts（用 backendPrefix()）：\n  " + "\n  ".join(
        sorted(set(offenders))
    )


def test_client_still_defines_the_single_origin_constant():
    """反向断言：防止上面那条测试因为"文件被改名/常量被删"而变成空转。"""
    text = (SRC / OWNER).read_text(encoding="utf-8")
    assert "BACKEND_ORIGIN" in text
    assert "function backendPrefix" in text
    assert "127.0.0.1:8000" in text, "client.ts 应保留唯一的主机字面量定义"


def test_backend_prefix_prefers_relative_on_http_pages():
    """判定依据必须是"页面协议"而非构建模式 —— 这是局域网场景的正确性所在。

    源码级断言（前端无单测框架，CI 只跑 tsc）：backendPrefix 必须先看
    import.meta.env.DEV / location.protocol，且**不得**出现
    `DEV ? ... : BACKEND_ORIGIN` 这种把"构建模式"当判据的旧写法。
    """
    text = (SRC / OWNER).read_text(encoding="utf-8")
    start = text.index("export function backendPrefix")
    body = text[start : text.index("\n}", start)]
    assert "location.protocol" in body, "backendPrefix 必须按页面协议判定（局域网场景）"
    assert 'location.protocol === "http:"' in body
    assert 'location.protocol === "https:"' in body
