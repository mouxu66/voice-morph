"""`plugin_loader` 的容错隔离 + `server.py` 路由注册表的机器门禁。

守的是什么
----------
改写前 `server.py` 是 **26 处模块级** `from xxx import router`。任意一处失败
（缺 torch / 缺权重 / 缺可选依赖 / 一个笔误）→ `server.py` 直接 `ImportError`
→ **整个后端起不来**，前端只看得到一个空界面，而真原因（"这台机器没装 Seed-VC"）
根本没有机会被报出来。而这些能力"用户没装"本来就是正常状态
（README「可选能力各自独立」那节列得很清楚）。

三条独立的判据，缺一条都不够：

1. **隔离真的生效** —— 坏一个模块，其余照常注册（`test_broken_module_...`，
   跑在**子进程**里毒掉一个真模块，是端到端的，不是 mock）。
2. **容错没变成静默** —— 坏掉的要在启动横幅里逐条说明原因。容错只把"崩溃"
   换成"静默"的话，反而比崩溃更难查（本项目同类事故见 `docs/犯错指南.md` §2.36）。
3. **顺序由清单决定、且对行为无影响** —— 挂载顺序 = 清单的插件 `order`
   （`test_mount_order_follows_the_manifest`）。而「顺序会不会改变 handler 归属」
   这件事由 `tests/test_route_shadowing.py` 直接断言：**不许出现两条互相遮蔽的
   router 路由**。2026-09-20 第 3 步实测过（139 条路由、0 条 router 间遮蔽），
   所以原来那份「冻结历史交错序」的期望值已删除 —— 它对行为没有影响，
   却会在每次加插件时逼人做一次无意义的取舍。

⚠️ 这套用例**不需要 torch**：`requirements-dev.txt` 刻意不装它，
所以"26 个模块全都能导入"这件事在 CI 的裸环境里同样成立。
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="plugin_loader 依赖 fastapi")

from fastapi import APIRouter  # noqa: E402

import plugin_loader  # noqa: E402
import plugin_manifest  # noqa: E402

_M2 = Path(__file__).resolve().parents[1]

#: 路由模块总数。**故意写死**：它是「有没有人顺手删掉一个能力」的独立哨兵 ——
#: 挂载顺序本身改由清单给出，所以这份字面量只剩计数这一件事。
#: 数字变了要同步 README 与 `docs/插件化设计.md` 里的说法。
_ROUTER_COUNT = 26


@pytest.fixture(autouse=True)
def _clean_registry():
    """用例之间不许共享 registry —— 否则"上一条用例留下的 broken"会污染下一条。"""
    plugin_loader.reset()
    yield
    plugin_loader.reset()


# ---------------------------------------------------------------- 1. 隔离生效


def test_unimportable_module_is_isolated(monkeypatch):
    """导入失败只记一笔，不抛 —— 抛出去就成了"整个后端起不来"。"""
    # sys.modules 里某名字被置成 None 时，`import` 会抛 ImportError（CPython 既有行为），
    # 比造一个假模块更贴近"真的导入失败"。
    monkeypatch.setitem(sys.modules, "seed_vc", None)
    assert plugin_loader.load_router("seed_vc") is None
    assert plugin_loader.is_loaded("seed_vc") is False
    broken = {r.module: r for r in plugin_loader.broken()}
    assert "seed_vc" in broken
    # 只钉「是导入类错误 + 点名了模块」：`sys.modules` 里塞 None 抛的是
    # `ModuleNotFoundError`（`ImportError` 的子类），措辞也可能随 CPython 版本微调，
    # 写死 "ImportError" 会变成一条会自己烂掉过的断言。
    reason = broken["seed_vc"].reason
    assert "Error" in reason and "seed_vc" in reason, reason


def test_module_without_router_says_so(monkeypatch):
    """「导入了但没有 router」要给出与「导入失败」不同的原因 —— 两者排查方向完全不同。"""
    monkeypatch.setitem(sys.modules, "_pl_no_router", types.ModuleType("_pl_no_router"))
    assert plugin_loader.load_router("_pl_no_router") is None
    reason = plugin_loader.broken()[0].reason
    assert "没有 router" in reason, reason


def test_healthy_module_returns_its_router():
    router = plugin_loader.load_router("effects")
    assert isinstance(router, APIRouter)
    assert plugin_loader.is_loaded("effects") is True
    assert plugin_loader.broken() == []


def test_healthy_module_list_handles_every_router_module():
    """把 `server._ROUTER_ORDER` 整串跑一遍，健康树上必须零失败。

    这条同时钉住两件事：加载器对 26 个模块都有效；没有一个模块在裸环境里导不进来
    （`requirements-dev.txt` 不装 torch，CI 就是这种环境）。
    """
    import server

    for name in server._ROUTER_ORDER:
        assert plugin_loader.load_router(name) is not None, f"{name} 加载失败"
    report = plugin_loader.report(server._ROUTER_ORDER)
    assert f"{_ROUTER_COUNT}/{_ROUTER_COUNT}" in report, report
    assert "不可用" not in report, report


def test_broken_module_does_not_stop_server_from_starting(tmp_path):
    """★ 本次改动的核心回归 —— 端到端，不是 mock。

    用一个"假装是 seed_vc"的坏模块抢占 `sys.path`，然后在**子进程**里 `import server`：

    · 改写前：`from seed_vc import router` 直接把它抛出来 → 子进程非零退出、后端起不来；
    · 改写后：`seed_vc` 记为不可用，其余 25 个照常注册，启动横幅点名说明原因。
    """
    (tmp_path / "seed_vc.py").write_text(
        'raise RuntimeError("模拟：这台机器没装 Seed-VC 的依赖")\n', encoding="utf-8"
    )
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "import plugin_loader\n"
        "import server\n"
        "print('BROKEN', sorted(r.module for r in plugin_loader.broken()))\n"
        f"print('TOTAL {_ROUTER_COUNT}')\n"
    )
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # 横幅里有中文，别让编码把用例搞红
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_M2),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    assert proc.returncode == 0, f"后端没起来（这正是要修的症状）：\n{proc.stderr}"
    assert "'seed_vc'" in proc.stdout, proc.stdout
    # 容错不能变成静默：横幅必须点名 + 给原因
    assert "不可用: seed_vc" in proc.stdout, proc.stdout
    assert "Error" in proc.stdout, proc.stdout  # 原因必须跟着出来


# ---------------------------------------------------------------- 2. 钩子


def test_call_hook_failure_is_not_masked_by_router_success(monkeypatch):
    """`(模块, 用途)` 分开记账：同一模块 router 成功，不能把钩子的失败盖掉。

    这不是假想 —— `audio_api` 正是"既提供 router 又提供启动钩子"的模块。
    """
    mod = types.ModuleType("_pl_hook")
    mod.router = APIRouter()

    def boom():
        raise ValueError("钩子炸了")

    mod.boom = boom
    monkeypatch.setitem(sys.modules, "_pl_hook", mod)

    assert plugin_loader.load_router("_pl_hook") is not None
    assert plugin_loader.call_hook("_pl_hook", "boom") is False

    labels = {r.label: r.ok for r in plugin_loader.results()}
    assert labels["_pl_hook"] is True
    assert labels["_pl_hook.boom"] is False


def test_keyboard_interrupt_is_not_swallowed(monkeypatch):
    """只吞 `Exception`：吞掉 `BaseException` 会让启动期的 Ctrl+C 失灵 —— 比导入失败更难查。"""
    mod = types.ModuleType("_pl_kbd")

    def raise_kbd():
        raise KeyboardInterrupt

    mod.raise_kbd = raise_kbd
    monkeypatch.setitem(sys.modules, "_pl_kbd", mod)

    with pytest.raises(KeyboardInterrupt):
        plugin_loader.call_hook("_pl_kbd", "raise_kbd")


@pytest.mark.parametrize(
    "module_name, attr",
    [
        ("pet_market", "ensure_default_bundle"),
        ("market_images", "start_background_sync"),
        ("warmup", "start_background"),
        ("audio_api", "_start_audio_audit"),
    ],
)
def test_optional_hooks_are_spelled_correctly(module_name, attr):
    """钩子改成"按字符串取属性"之后，**拼错不再报错，只会静默不生效**。

    原来写的是 `from pet_market import ensure_default_bundle` —— 名字打错是 ImportError，
    一眼可见；现在塞进字符串里，最坏情况是"这个副作用静偷偷不跑了"。
    所以每个钩子都得有一条断言把它钉在真属性上（尤其 `_start_audio_audit` 是私有名，改它很容易）。
    """
    mod = importlib.import_module(module_name)
    assert callable(getattr(mod, attr, None)), f"{module_name} 里没有可调用的 {attr}"


# ---------------------------------------------------------------- 3. 顺序冻结


def test_mount_order_follows_the_manifest(monkeypatch):
    """挂载顺序必须**完全等于**清单算出来的顺序。

    `monkeypatch` 把禁用集钉成空：第 6 步起挂载会跳过被关掉的能力，
    而这条比的是**结构**（清单 → server 的模块名来源），不该被某个开发机上
    残留的 `outputs/plugins.json` 影响。跳过行为本身由
    `tests/test_plugin_switch.py` 覆盖。

    清单是模块名的唯一来源（`server.py` 不再手写那 26 个模块名），所以这里比的是
    「`server.py` 有没有照清单走」，而不是「顺序是不是某个历史快照」。

    为什么不再冻结一份历史顺序：2026-09-20 第 3 步把「顺序有语义」这句话量了一遍
    （139 条真实路由）—— **没有任何两条 router 路由互相遮蔽**，65 条遮蔽关系全部是
    「router vs SPA 兜底」且全部无害。也就是说注册顺序对 handler 归属**没有影响**，
    冻结它只会在每次加插件时逼人改一次快照。真正该守的那件事（不许出现重叠）在
    `tests/test_route_shadowing.py::test_no_router_route_shadows_another`。
    """
    import server

    monkeypatch.setattr(plugin_manifest, "disabled_ids", lambda: set())
    expected = [module for _, module in plugin_manifest.mount_plan()]
    assert list(server._ROUTER_ORDER) == expected
    assert len(expected) == _ROUTER_COUNT


# ---------------------------------------------------------------- 4. 界面出口


def _load_all() -> None:
    """把 `server._ROUTER_ORDER` 整串过一遍加载器（复现一次启动时的记账）。"""
    import server

    for name in server._ROUTER_ORDER:
        plugin_loader.load_router(name)


def test_capabilities_endpoint_reports_load_state():
    """`GET /api/capabilities` 是「哪个能力不可用」走到界面上的唯一出口。

    健康树上的形状要稳：前端靠 `broken` 决定要不要弹降级提示。
    """
    from fastapi.testclient import TestClient

    import server

    _load_all()
    body = TestClient(server.app).get("/api/capabilities").json()
    assert body["ok"] is True
    assert body["total"] == _ROUTER_COUNT
    assert body["loaded"] == body["total"]
    assert body["broken"] == []


def test_capabilities_endpoint_lists_broken_with_reason(monkeypatch):
    """坏一个模块时：`ok` 置假、计数对得上、**原因跟着出来**。

    只报「坏了」而不给原因，等于把日志里的问题又讲一遍 —— 界面提示必须自带原因。
    """
    from fastapi.testclient import TestClient

    import server

    monkeypatch.setitem(sys.modules, "seed_vc", None)
    _load_all()
    body = TestClient(server.app).get("/api/capabilities").json()
    assert body["ok"] is False
    assert body["loaded"] == body["total"] - 1
    assert [b["module"] for b in body["broken"]] == ["seed_vc"]
    assert body["broken"][0]["reason"], "必须带上原因"


def test_capabilities_endpoint_does_not_need_torch():
    """本端点**不能**依赖重库。

    反例就是挨着它的 `/api/health`：第一行 `import torch`，没 torch 时它自己 500。
    而「某个能力没装依赖」恰恰是没 torch 的环境里最该看到的信息 ——
    把能力清单挂在 /health 上等于在最需要的时候消失。
    这条用裸环境（`requirements-dev.txt` 不装 torch）在 CI 上就是真实场景。
    """
    import inspect

    import server
    import system_api

    src = inspect.getsource(system_api.capabilities)
    # ⚠️ 必须先把 docstring 摘掉：本函数的注释**故意**举例了 `import torch`（说明为什么不挂在
    # /health 上），不摘就会拿注释里的例子把自己判红 —— 断言要判的是**代码**，不是文档。
    code = src.replace(system_api.capabilities.__doc__ or "", "")
    assert "torch" not in code, "capabilities 的实现里不许出现 torch"
    assert "plugin_loader" in code
    # 打不到服务也不能因为缺 torch 而挂：源码层面钉住后，再确认它真的能返回
    assert server.app is not None


def test_report_exposes_failure_reasons(monkeypatch):
    """`report()` 必须把失败摊开说 —— 这是"容错没有变成静默"的唯一出口。"""
    monkeypatch.setitem(sys.modules, "seed_vc", None)
    plugin_loader.load_router("seed_vc")
    plugin_loader.load_router("effects")

    text = plugin_loader.report(("effects", "seed_vc"))
    assert "1/2" in text, text
    assert "不可用: seed_vc" in text, text
    assert "Error" in text, text  # 原因必须跟着出来，不能只报"坏了"
