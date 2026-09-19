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
3. **顺序没被顺手重排** —— 注册顺序是**行为**：FastAPI 按注册顺序匹配路由，
   重排会让重叠路径换一个 handler 接。

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

_M2 = Path(__file__).resolve().parents[1]

#: 期望的注册顺序 —— 抄自改写前 `server.py` 里 26 处 `include_router` 的**实际先后**。
_EXPECTED_ORDER: tuple[str, ...] = (
    "rvc_live",
    "finetune",
    "audiobook",
    "offline_vc",
    "seed_vc",
    "cascade",
    "effects",
    "wechat_voice",
    "history_api",
    "system_api",
    "voices_api",
    "raw_media_api",
    "pipeline_api",
    "clips_api",
    "tts_api",
    "mine_api",
    "market_api",
    "pet_market_api",
    "capture_api",
    "ab_api",
    "ab_chain",
    "audio_api",
    "audition_api",
    "media_api",
    "rvc_dataset_api",
    "openai_compat",
)


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
    assert f"{len(_EXPECTED_ORDER)}/{len(_EXPECTED_ORDER)}" in report, report
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
        f"print('TOTAL {len(_EXPECTED_ORDER)}')\n"
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


def test_router_registration_order_is_frozen():
    """⚠️ 别重排 `_ROUTER_ORDER`。

    FastAPI 按注册顺序匹配路由 —— 两条路径重叠时（例如 `/api/voices/{name}` 与
    `/api/voices/market`），先注册的那个接。顺序是**行为**，不是排版。
    要动这份期望值，请先读 `server.py` 里 `_register` 上方那段注释。
    """
    import server

    assert list(server._ROUTER_ORDER) == list(_EXPECTED_ORDER)


def test_report_exposes_failure_reasons(monkeypatch):
    """`report()` 必须把失败摊开说 —— 这是"容错没有变成静默"的唯一出口。"""
    monkeypatch.setitem(sys.modules, "seed_vc", None)
    plugin_loader.load_router("seed_vc")
    plugin_loader.load_router("effects")

    text = plugin_loader.report(("effects", "seed_vc"))
    assert "1/2" in text, text
    assert "不可用: seed_vc" in text, text
    assert "Error" in text, text  # 原因必须跟着出来，不能只报"坏了"
