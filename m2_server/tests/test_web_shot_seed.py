"""守卫：`tools/web_shot.py` 播种的 localStorage 键**必须与前端源码一致**（2026-10-14）。

### 为什么需要

`web_shot.py` 是"别让用户当眼睛"的无头走查工具（`docs/犯错档案-工程.md` §3.22 / §3.26），
它靠**预先播种 localStorage** 把首启拦截层顶掉，否则 `Page.captureScreenshot` 截到的是
弹窗而不是页面 —— 而**弹窗同样有尺寸、同样不横向溢出、同样能截图**，于是工具会报
「全部通过」。这是本项目最危险的那类假绿：**它让「今天没验」看起来像「验过了」**
（同族：`_assert_app_loaded` 里那句"预览服务没起来时会渲染错误页"）。

### 踩坑经过（2026-10-08 实测）

漏了用途问答（`vm-use-asked` / `vm-use-profile`）时，`/preserve` 截出来是「你打算用它做什么？」；
**补上之后又暴露出每次启动的模式选择**（`vm.modechooser.dontask`）—— 因为它的条件是
`hasBeenAsked() && dontask !== '1'`，**播了用途问答反而触发了它**。

⇒ 拦截层是**会增生的**：每加一个新问答，这个工具就静默退化成"截图工具照常出图、
但出的是弹窗"。而它退化时**没有任何东西会报错** —— 所以键名必须在门禁里钉住。

### 口径

不解析 TypeScript，只做**存在性核对**：播种的每个键必须在 `web/src` 里出现
（去掉 `.test.ts`，因为测试里的字面量不算"前端真的读它"）。
反过来只查"种子键都有归属"，不查"有几个拦截层" —— 后者是在给"人怎么写弹窗"建规则。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB_SRC = ROOT / "web" / "src"
WEB_SHOT = ROOT / "tools" / "web_shot.py"

# 前端源码不在（只装后端的部署形态）时跳过，而不是误判为通过
if not WEB_SRC.is_dir():
    pytest.skip(f"未找到前端源码目录：{WEB_SRC}", allow_module_level=True)

#: `seed = (...)` 里 `localStorage.setItem('key','value')` / `sessionStorage.setItem(...)`。
_SETITEM = re.compile(r"(?:local|session)Storage\.setItem\(\s*'([^']+)'\s*,\s*'([^']*)'\s*\)")


def _seeded_keys(text: str) -> dict[str, str]:
    """从 `_shoot()` 的播种串里取出 `{键: 值}`。"""
    # 只看 `seed = (...)` 那一段：文件里别处也可能有 setItem（比如文档字符串举例）
    m = re.search(r"seed = \((.*?)\)\n", text, re.S)
    assert m, "web_shot.py 里找不到 `seed = (...)` 播种串 —— 结构变了，改守卫前先确认工具还对"
    body = m.group(1)
    return {k: v for k, v in _SETITEM.findall(body)}


def _frontend_literal(key: str) -> str | None:
    """前端源码里定义/使用这个键的文件（相对 `web/src`），找不到返回 None。"""
    for p in WEB_SRC.rglob("*"):
        if p.suffix not in (".ts", ".tsx") or not p.is_file():
            continue
        if p.name.endswith((".test.ts", ".test.tsx")):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if f'"{key}"' in text or f"'{key}'" in text:
            return p.relative_to(WEB_SRC).as_posix()
    return None


def test_seed_string_is_not_empty():
    """守卫自己先要有牙：播种串读不出来时，下面那条会**空对空地绿**。

    同族病：`docs/犯错档案-工程.md` §8.21（两边都空 = 白绿）。
    """
    keys = _seeded_keys(WEB_SHOT.read_text(encoding="utf-8"))
    assert keys, "播种串解析出 0 个键 —— 解析器或工具结构变了，别让它假装通过"
    # 键名形态：`vm-*` 或 `vm.*`（前端两套命名并存，别在这里强求统一）
    assert all(k.startswith("vm") for k in keys), keys


def test_every_seeded_key_exists_in_frontend():
    """**核心断言**：每个播种的键都必须真在前端源码里。

    拼错一个字母（`vm-use-ask`）不会报错，只会**静默失效**：该拦截层照弹，
    截出来是弹窗，而工具报「全部通过」。
    """
    keys = _seeded_keys(WEB_SHOT.read_text(encoding="utf-8"))
    missing = {k: v for k, v in keys.items() if _frontend_literal(k) is None}
    assert not missing, (
        "web_shot.py 播种了前端源码里不存在的键 —— 该键静默失效，"
        "对应的拦截层会照弹，截图里是弹窗而不是页面（而工具会报「全部通过」）：\n  "
        + "\n  ".join(f"{k} = {v!r}" for k, v in missing.items())
        + "\n修法：在 web/src 里搜真实键名，改 web_shot.py 的 seed 串。"
    )


def test_known_overlays_are_all_seeded():
    """已确认的拦截层**必须都在播种清单里** —— 防止将来有人把某一行删掉。

    名单是"实测撞见过的"（2026-10-08）：首启引导 / 用途问答 / 每次启动的模式选择。
    新撞见一个就往这里加一行，并同时加进 `web_shot.py` 的 seed 串。
    """
    keys = _seeded_keys(WEB_SHOT.read_text(encoding="utf-8"))
    required = {
        "vm_first_launch_done": "首启引导 FirstLaunchGuide",
        "vm-use-asked": "用途问答 UseCasePicker（不播它 → /preserve 截出「你打算用它做什么？」）",
        "vm-use-profile": "同上，配套的值",
        "vm.modechooser.dontask": "每次启动的模式选择 ModeChooser",
        "vm-theme": "主题",
    }
    absent = {k: why for k, why in required.items() if k not in keys}
    assert not absent, (
        "已知拦截层没有被播种 —— 它弹出来就会挡住页面：\n  "
        + "\n  ".join(f"{k}（{why}）" for k, why in absent.items())
    )
