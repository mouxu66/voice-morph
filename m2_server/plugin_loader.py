"""路由模块的容错加载：一个能力坏掉，不能让整个后端起不来。

为什么需要它
------------
`server.py` 原本是 **26 个模块级** `from xxx import router`。这意味着任意一个模块
导入失败 —— 缺 torch、缺模型权重、拼写错误、可选依赖没装 —— `server.py` 直接
`ImportError`，**整个后端无法启动**。前端只看得到一个空界面，而真实原因
（比如"这台机器没装 demucs"）根本没有机会被报出来。

而这些模块的"重"是设计上就承认的，README 的「可选能力各自独立」一节列得很清楚：

    TTS       → 4.9G 权重
    实时变声  → RVC 整合包 + VB-Audio CABLE
    微信发送  → 微信 PC 版

**用户没装其中任何一项都是正常状态**，不该表现为"软件打不开"。

做什么 / 不做什么
-----------------
这里只做**失败隔离 + 记账**，不做插件清单、不做开关（那是 `docs/插件化设计.md`
第 2 步起的事）。这样它不依赖插件化的任何其它部分，可以单独合入。

判据与顺序
----------
只吞 `Exception`，**不吞** `BaseException` —— 后者会把启动期的 `KeyboardInterrupt`
一起按住，后端变得无法中断，比导入失败更难查。

`server.py` 里各模块的**注册顺序必须与改写前逐条一致**：FastAPI 按注册顺序匹配路由，
调顺序会让重叠路径换一个 handler 接（`test_server.py` 用真实请求，能抓到，
但没必要去踩）。
"""

from __future__ import annotations

import importlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter

ROUTER_PURPOSE = "router"


@dataclass(frozen=True)
class LoadResult:
    """一条加载结果。

    记账粒度是 **(模块, 用途)** 而不是模块：同一个模块可能既提供 `router`
    又提供启动钩子（`audio_api` 就是这样），共用一个 key 的话，
    router 成功了就会把钩子的失败悄悄盖掉。
    """

    module: str
    purpose: str
    ok: bool
    reason: str = ""

    @property
    def label(self) -> str:
        """日志里的显示名：路由就是模块名，钩子带上属性名。"""
        return self.module if self.purpose == ROUTER_PURPOSE else f"{self.module}.{self.purpose}"


_LOCK = threading.Lock()
_RESULTS: dict[tuple[str, str], LoadResult] = {}


def _record(module: str, purpose: str, ok: bool, reason: str = "") -> LoadResult:
    res = LoadResult(module=module, purpose=purpose, ok=ok, reason=reason)
    with _LOCK:
        _RESULTS[(module, purpose)] = res
    return res


def load_router(module_name: str) -> APIRouter | None:
    """导入 `module_name` 并取出它的 `router`；任何失败都记进 registry 并返回 `None`。

    失败**不抛**：调用方（`server.py`）据此跳过这一个模块，其余照常注册。
    """
    try:
        mod = importlib.import_module(module_name)
        router = getattr(mod, ROUTER_PURPOSE, None)
        if router is None:
            raise AttributeError(f"{module_name} 里没有 router 对象")
    except Exception as exc:  # noqa: BLE001 —— 隔离就是本模块的**目的**，见模块注释
        _record(module_name, ROUTER_PURPOSE, False, f"{type(exc).__name__}: {exc}")
        return None
    _record(module_name, ROUTER_PURPOSE, True)
    return router  # type: ignore[no-any-return]  # 动态取属性，类型由 include_router 在校验


def call_hook(module_name: str, attr: str, *args: Any) -> bool:
    """调用一个**可选**启动钩子（如 `audio_api._start_audio_audit`）。

    等价于原来那几处 `try: from x import y; y() except Exception: pass`，
    但统一走同一个入口，registry 里也能看到它为什么没跑起来。
    """
    try:
        mod = importlib.import_module(module_name)
        fn = getattr(mod, attr, None)
        if fn is None:
            raise AttributeError(f"{module_name} 里没有 {attr}")
        fn(*args)
    except Exception as exc:  # noqa: BLE001 —— 可选钩子失败不该拖垮启动
        _record(module_name, attr, False, f"{type(exc).__name__}: {exc}")
        return False
    _record(module_name, attr, True)
    return True


def results() -> list[LoadResult]:
    """registry 快照，按 (模块, 用途) 排序。"""
    with _LOCK:
        return sorted(_RESULTS.values(), key=lambda r: (r.module, r.purpose))


def broken() -> list[LoadResult]:
    """失败清单；健康树上是空列表。"""
    return [r for r in results() if not r.ok]


def is_loaded(module_name: str) -> bool:
    """该模块的 `router` 是否加载成功（没试过 = False）。"""
    with _LOCK:
        res = _RESULTS.get((module_name, ROUTER_PURPOSE))
    return bool(res and res.ok)


def reset() -> None:
    """清空 registry —— **只给测试用**（真实启动进程里每个模块只加载一次）。"""
    with _LOCK:
        _RESULTS.clear()


def report(router_modules: Sequence[str]) -> str:
    """拼启动横幅：`路由模块 26/26 个已加载`，坏掉的逐条列出原因。

    刻意用 `print` 而不是 `logging`：本项目的后端日志**就是 stdout**
    （`backend.cjs` 捕获子进程 stdout 落到 `backend.log`），全仓没有
    `logging.basicConfig`，见 README「日志与排障」。
    """
    ok = sum(1 for m in router_modules if is_loaded(m))
    lines = [f"[plugin_loader] 路由模块 {ok}/{len(router_modules)} 个已加载"]
    for res in broken():
        lines.append(f"[plugin_loader]   不可用: {res.label} —— {res.reason}")
    return "\n".join(lines)
