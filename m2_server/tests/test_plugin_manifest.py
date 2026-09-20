"""插件清单（`m2_server/plugins/*/plugin.json`）的门禁。

这一步（`docs/插件化设计.md` 第 2 步）**只声明、不改行为**。所以门禁的重点
不是"功能对不对"，而是**声明与现实有没有对上** —— 一份没人校验的清单会立刻开始漂，
而它恰恰是被设计成"唯一真相源"的东西。

| 守什么 | 用例 |
|---|---|
| 26 个 router 被清单**不多不少**全覆盖 | `test_manifest_covers_...` |
| 挂载顺序 = 插件 `order` + 插件内声明序 | `test_mount_plan_...` |
| 4 个启动钩子都被声明，且 `when` 与**实际调用时机**一致 | `test_all_four_...` / `test_hooks_fire_...` |
| 健康探针 / `extras` 不写不存在的东西 | `test_health_...` / `test_extras_...` |
| **三态**：`disabled` 不能被算成 `broken` | `test_disabled_...` |
| 前端**不再硬编码**路由/侧栏，只能从清单拿 | `test_app_tsx_no_longer_...` / `test_studio_nav_no_longer_...` |
| 清单里的 IA 是**冻结快照**（少一条就红） | `test_manifest_routes_are_the_frozen_...` / `..._nav_is_the_frozen_...` |
| 声明的页面 / 导出在磁盘上真的存在（全量） | `test_declared_page_module_and_export_exist` |
| 用户配置不会被存储清理顺手删掉 | `test_plugins_json_...` |
| `/api/plugins` 与 `/api/capabilities` 不互相矛盾 | `test_plugins_endpoint_...` |

> 「路由注册顺序不许重排」这条**已从本文件移出**：2026-09-20 第 3 步实测（139 条路由）
> 没有任何两条 router 路由互相遮蔽，顺序对 handler 归属没有影响。该守的改成了
> 「不许出现重叠」→ `tests/test_route_shadowing.py`。
>
> 「`extras.python` 的包必须能在仓库里装到」这条**也已移出**（第 5 步）：它的旧口径是
> 「包名必须出现在 `requirements*.txt` / `setup_env.ps1` 文本里」，而第 5 步之后
> `setup_env.ps1` 改成**读清单装 extras**，文本里不再有包名 —— 旧口径会退化成
> 「看谁的字面量多」。新口径改成**静态 import 图对账** → `tests/test_plugin_deps.py`。
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="plugin_manifest 依赖 fastapi（经 plugin_loader）")

import plugin_loader  # noqa: E402
import plugin_manifest  # noqa: E402

_M2 = Path(__file__).resolve().parents[1]
_ROOT = _M2.parent
_APP_TSX = (_ROOT / "web" / "src" / "App.tsx").read_text(encoding="utf-8")
_NAV_TSX = (_ROOT / "web" / "src" / "components" / "voice-studio" / "StudioNav.tsx").read_text(
    encoding="utf-8"
)


@pytest.fixture(autouse=True)
def _clean_caches():
    """用例之间不共享 registry / 清单缓存。"""
    plugin_loader.reset()
    plugin_manifest.reset_cache()
    yield
    plugin_loader.reset()
    plugin_manifest.reset_cache()


def _load_all() -> None:
    """复现一次启动时的加载记账（`server._ROUTER_ORDER` 整串过一遍加载器）。"""
    import server

    for name in server._ROUTER_ORDER:
        plugin_loader.load_router(name)


#: 探针脚本：在**子进程**里把 `call_hook` 换成记录器，再 `import server`，
#: 然后打出「实际调了哪些钩子」与「清单声明了哪些」。用子进程是因为钩子只在
#: **第一次** `import server` 时跑一次，同进程里没法重放。
_HOOK_PROBE = """
import json
import plugin_loader

calls = []
plugin_loader.call_hook = lambda m, a, *rest: (calls.append([m, a]), True)[1]

import plugin_manifest
import server

print("RESULT " + json.dumps({
    "calls": sorted(calls),
    "import": sorted([h["module"], h["attr"]] for p in plugin_manifest.load_all()
                     for h in p.hooks if h["when"] == "import"),
    "main": sorted([h["module"], h["attr"]] for p in plugin_manifest.load_all()
                   for h in p.hooks if h["when"] == "main"),
}))
"""


def test_hooks_fire_in_the_phase_the_manifest_declares():
    """`when` 不是装饰性字段 —— 所以别只比源码文本，**跑一次 `import server` 看谁被调用**。

    · 声明 `when="import"` 的钩子必须**恰好**在这时被调用：少一个 = 副作用静默消失，
      多一个 = 有钩子没进清单（清单就不再是唯一真相源）；
    · 声明 `when="main"` 的钩子必须**没有**被调用 —— 它只在 `python server.py` 入口跑。
      放错位置会让打包探测 / 测试 / 工具脚本每次 `import server` 都产生副作用。

    为什么换掉了原来那版：旧版是从源码里按**行号**抓 `call_hook("mod", "attr")`
    字面量，再猜它在不在 `__main__` 块里。第 3 步把调用改成「遍历清单」之后字面量
    就没了，那种测法会「两边都空」地白绿 —— 而且它测的本来就是**文本**，不是**行为**。
    """
    proc = subprocess.run(
        [sys.executable, "-c", _HOOK_PROBE],
        cwd=str(_M2),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},  # 横幅里有中文
    )
    assert proc.returncode == 0, f"`import server` 就失败了：\n{proc.stderr}"

    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, f"探针没打出结果：\n{proc.stdout}\n{proc.stderr}"
    got = json.loads(line.removeprefix("RESULT "))

    # 先确认这份断言不是空的 —— 两边都空也会「相等」
    assert got["import"], "清单里一条 when=import 的钩子都没有？那这个断言是空的"
    assert got["calls"] == got["import"], (
        f"import 期实际调用的钩子与清单不符：\n  实际 {got['calls']}\n  清单 {got['import']}"
    )
    for hook in got["main"]:
        assert hook not in got["calls"], f"when=main 的钩子 {hook} 在 import 期就被调用了"


# ------------------------------------------------------- 覆盖：不多不少、顺序不乱


def test_manifest_covers_exactly_the_registered_routers():
    """清单认领的 router 集合必须与 `server.py` 实际注册的**完全相等**。

    少一个 = 有能力的归属没人声明（第 4 步渲染侧边栏时它会凭空消失）；
    多一个 = 清单在吹牛（声称挂着一个其实没注册的模块）。
    """
    import server

    plugins = plugin_manifest.load_all()
    declared = [m for p in plugins for m in p.routers]
    assert sorted(declared) == sorted(server._ROUTER_ORDER)
    assert len(declared) == len(set(declared)), "同一模块被两个插件认领"


def test_mount_plan_is_plugin_order_then_declared_router_order():
    """挂载计划的两层结构：插件按 `order` 升序成块，块内按 `routers` 声明序。

    `server.py` 直接拿 `mount_plan()` 去挂载，所以这条钉的就是「挂载顺序」本身。
    和 `test_plugin_loader.py::test_mount_order_follows_the_manifest` 的分工：
    那边比 `server._ROUTER_ORDER`（实现有没有照清单走），这边比清单算出来的结构。

    注意**不再**冻结一份历史全序。第 3 步实测（139 条路由）表明没有两条 router 路由
    互相遮蔽，所以顺序对 handler 归属没有影响；该守的是「不许出现重叠」，见
    `tests/test_route_shadowing.py`。
    """
    plan = plugin_manifest.mount_plan()
    plugins = plugin_manifest.load_all()

    # 按插件切块（挂载计划里同一插件的模块必然相邻）
    blocks: list[tuple[str, list[str]]] = []
    for pid, module in plan:
        if blocks and blocks[-1][0] == pid:
            blocks[-1][1].append(module)
        else:
            blocks.append((pid, [module]))

    # ① 块的先后 = 插件 order 升序（这里自己排一遍，不依赖 load_all 的内部排序）
    assert [pid for pid, _ in blocks] == [p.id for p in sorted(plugins, key=lambda x: x.order)]
    # ② 块内顺序 = 该插件 routers 的声明序（重排数组 = 悄悄改挂载顺序）
    by_id = {p.id: p for p in plugins}
    for pid, modules in blocks:
        assert modules == list(by_id[pid].routers), f"{pid} 的挂载顺序与声明序不符：{modules}"


def test_manifest_ids_and_order_are_unique():
    plugins = plugin_manifest.load_all()
    assert len(plugins) == 19, "插件数量变了就要同步更新 README 与设计文档里的数字"
    assert [p.order for p in plugins] == sorted(p.order for p in plugins)
    assert all(p.summary and p.name for p in plugins)


def test_core_plugins_are_declared_core():
    """核心/可选的分界是产品决定，别顺手改 —— 关掉核心插件界面就没了意义。"""
    core = {p.id for p in plugin_manifest.load_all() if p.category == "core"}
    assert core == {
        "core.system",
        "core.voices",
        "core.media",
        "core.history",
        "core.audio",
        "core.market",
        "core.openai",
    }


# ------------------------------------------------------- 钩子 / 探针 / extras


def test_all_four_startup_hooks_are_declared():
    """4 个启动钩子一个都不能少 —— 少一个就是「副作用静默消失」。

    这份字面量是刻意的哨兵：`when` 的**正确性**由上面那条按行为验证的用例负责，
    这里只回答「有没有人把一条钩子从清单里删掉了」。第 3 步起 `server.py` 不再手写
    `call_hook(...)` 字面量，所以源码比对已经不可能了 —— 只能钉清单本身。
    """
    declared = {(h["module"], h["attr"]): h["when"] for p in plugin_manifest.load_all() for h in p.hooks}
    assert declared == {
        ("pet_market", "ensure_default_bundle"): "import",
        ("market_images", "start_background_sync"): "import",
        ("warmup", "start_background"): "import",
        ("audio_api", "_start_audio_audit"): "main",
    }


def test_hook_and_health_targets_exist():
    """钩子与健康探针必须是**真属性**。

    钩子从 `from x import y` 改成字符串 `("x", "y")` 之后，拼错不再报错、只会静默不生效
    （`test_plugin_loader.py` 已为 4 个钩子钉过一次）。`health` 是同一类风险的新增字段，
    所以在这里一起钉：探针名字打错的话，第 6 步的"能力自检"会永远显示未知状态。
    """
    for p in plugin_manifest.load_all():
        for h in p.hooks:
            mod = importlib.import_module(h["module"])
            assert callable(getattr(mod, h["attr"], None)), f"{p.id}: {h['module']}.{h['attr']} 不存在"
        if p.health:
            mod = importlib.import_module(p.health["module"])
            assert callable(getattr(mod, p.health["attr"], None)), f"{p.id}: 探针不存在"


def test_health_probe_returns_a_dict_for_the_declared_ones():
    """已声明的两个探针要能真的调、且返回 dict（第 6 步会把它直接塞进 JSON）。"""
    declared = [(p.id, p.health) for p in plugin_manifest.load_all() if p.health]
    assert {pid for pid, _ in declared} == {"sound.tts", "hook.wechat"}
    for pid, h in declared:
        fn = getattr(importlib.import_module(h["module"]), h["attr"])
        assert isinstance(fn(), dict), f"{pid} 的探针没返回 dict"


def test_extras_env_vars_are_real():
    """`extras.models[].env` / `extras.external[].env` 必须是后端真的会读的环境变量。"""
    backend = "\n".join(f.read_text(encoding="utf-8") for f in _M2.glob("*.py"))
    for p in plugin_manifest.load_all():
        for item in [*p.extras.get("models", []), *p.extras.get("external", [])]:
            env = item.get("env")
            if env:
                assert env in backend, f"{p.id}: {env} 不是后端读取的环境变量"


# ------------------------------------------------------- 三态语义


def test_all_plugins_are_ok_on_a_healthy_start():
    _load_all()
    cat = plugin_manifest.catalog()
    assert cat["ok"] is True
    assert cat["counts"] == {"total": 19, "ok": 19, "broken": 0, "disabled": 0}
    assert all(p["state"] == "ok" for p in cat["plugins"])


def test_one_broken_router_marks_only_its_plugin(monkeypatch):
    """坏一个 router：**只有**它所属的插件变 broken，其余照常。"""
    monkeypatch.setitem(__import__("sys").modules, "seed_vc", None)
    _load_all()
    cat = plugin_manifest.catalog()
    broken = {p["id"] for p in cat["plugins"] if p["state"] == "broken"}
    assert broken == {"sound.offline-vc"}, broken
    assert cat["ok"] is False
    offline = next(p for p in cat["plugins"] if p["id"] == "sound.offline-vc")
    assert any("seed_vc" in r for r in offline["reasons"]), offline["reasons"]


def test_router_never_attempted_counts_as_broken_not_ok():
    """清单声称的 router 若**根本没被注册过**，那是清单与代码脱节，必须算 broken。

    这条是"只声明不改行为"这一步唯一能自己抓到的漂移：清单先写了，
    而 `server.py` 那边忘了挂（或改了名字）。
    """
    cat = plugin_manifest.catalog()  # 注意：**没有** _load_all()，registry 是空的
    assert cat["counts"]["broken"] == 19
    assert all("未注册" in " ".join(p["reasons"]) for p in cat["plugins"] if p["state"] == "broken")


def test_disabled_is_not_broken(monkeypatch):
    """★ 三态的核心约束：用户主动关掉的插件不能被算成"坏了"。

    前端 `SetupBanner` 的弹出条件就是"有 broken"（`ModelSetupPanel.tsx`），
    所以把 disabled 混进 broken = 用户每关一个插件就吃一条"能力未加载"的告警。
    """
    _load_all()
    monkeypatch.setattr(plugin_manifest, "disabled_ids", lambda: {"sound.tts", "sound.effects"})
    cat = plugin_manifest.catalog()
    assert cat["counts"] == {"total": 19, "ok": 17, "broken": 0, "disabled": 2}
    assert cat["ok"] is True, "有插件被关掉不该让整体 ok 变假"
    assert {p["id"] for p in cat["plugins"] if p["state"] == "disabled"} == {"sound.tts", "sound.effects"}


def test_disabled_plugin_still_reports_its_reasons(monkeypatch):
    """关掉的插件也把"其实加载失败了"记下来 —— 别把信息丢掉。

    设置页要能说清「你关的，而且它本来就是坏的」；把 reasons 清空就说不清了。
    """
    monkeypatch.setitem(__import__("sys").modules, "seed_vc", None)
    _load_all()
    monkeypatch.setattr(plugin_manifest, "disabled_ids", lambda: {"sound.offline-vc"})
    offline = next(p for p in plugin_manifest.catalog()["plugins"] if p["id"] == "sound.offline-vc")
    assert offline["state"] == "disabled"
    assert any("seed_vc" in r for r in offline["reasons"])


def test_missing_or_broken_state_file_means_nothing_disabled(tmp_path, monkeypatch):
    """用户配置读不出来时，**一律当成"一个都没关"**，不能让端点 500。"""
    monkeypatch.setattr(plugin_manifest, "STATE_FILE", tmp_path / "nope.json")
    assert plugin_manifest.disabled_ids() == set()
    bad = tmp_path / "bad.json"
    bad.write_text("{不是 json", encoding="utf-8")
    monkeypatch.setattr(plugin_manifest, "STATE_FILE", bad)
    assert plugin_manifest.disabled_ids() == set()
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"enabled": ["sound.tts"]}), encoding="utf-8")
    monkeypatch.setattr(plugin_manifest, "STATE_FILE", ok)
    assert plugin_manifest.disabled_ids() == set(), "只认 disabled 键（白名单会让新插件默认被关掉）"


# ------------------------------------------------------- 与前端对齐
#
# 第 4 步之前这里是**正则扒源码**：从 `App.tsx` 抓 `<Route path="…">`、从
# `StudioNav.tsx` 抓 `START_ITEMS` / `MORE_ITEMS` 两个数组，再与清单逐条对账。
# 第 4 步把两处都换成清单驱动后，**源码里没有那些字面量了** —— 扒出来两边都是空集，
# 对账会「两边都空」地白绿（`docs/犯错指南.md` §8.19 那一类），所以必须换掉。
#
# 换成三条方向不同的门禁，合起来仍等价于原来那条「前端呈现的页面 == 清单声明的页面」：
#
#   A. 前端确实不再硬编码 → 它**只能**从清单拿（两条 `..._no_longer_hardcodes_...`）
#   B. 清单里的 IA 是**冻结快照** → 清单不会悄悄少一条（两条 `..._frozen_...`）
#
#   A ∧ B  ⇒  前端呈现的页面集合 == 冻结的那一份
#
# 另一件事 —— 「前端能不能**正确**消费清单」（glob key 公式、具名导出名、图标注册表、
# `disabled` 过滤）**在 Python 里验不了**，要真跑 JS。它由 `web/src/lib/pluginRoutes.test.ts`
# 负责，CI 的 web job 会 `npx vitest run`。下面 `test_declared_page_module_and_export_exist`
# 只做「磁盘上真的存在」那一层。


# 路径写在 `<Route>` 标签里的任意位置都要抓到（不能只认 `<Route path="` 开头，
# 那漏掉 `<Route element={…} path="/x">` 这种写法）
_LITERAL_ROUTE_RE = re.compile(r'<Route\b[^>]*?\bpath="([^"]+)"')
_PAGE_STATIC_IMPORT_RE = re.compile(r'from\s+"@/pages/[^"]+"')

# `/` 与 `*` 是外壳兜底（重定向到首页），不属于任何插件，允许字面量
_APP_SHELL_PATHS = frozenset({"/", "*"})


def test_app_tsx_no_longer_hardcodes_page_routes():
    """`App.tsx` 不许再静态 import 页面、也不许写死页面路径。

    第 4 步的全部意义就是「页面从清单来」。只要有人把一条 `<Route path="/xxx">` 或者
    一个 `import { XRoute } from "@/pages/X/index"` 加回去，那条路径就**不受清单管辖**了：
    清单里删掉它前端照样显示、清单里改了路径前端照样显示旧路径 —— 漂移重新长出来，
    而且没有任何门禁会红（这正是第 4 步要消灭的东西）。
    """
    literals = set(_LITERAL_ROUTE_RE.findall(_APP_TSX))
    # 空集哨兵：把 <Routes> 整块删掉也能让下面那条不等式成立
    assert literals, "App.tsx 里一条字面量 <Route> 都没有 —— <Routes> 是不是被删了？"
    assert literals <= _APP_SHELL_PATHS, (
        f"App.tsx 里写死了页面路径 {sorted(literals - _APP_SHELL_PATHS)}；"
        "页面路径只能来自清单（plugins/<id>/plugin.json 的 routes[].path）"
    )
    static_imports = _PAGE_STATIC_IMPORT_RE.findall(_APP_TSX)
    assert not static_imports, (
        f"App.tsx 里还有静态页面 import：{static_imports}；"
        "页面必须走 import.meta.glob + 清单白名单懒加载（见 lib/pluginRoutes.tsx）"
    )
    # 正向要求：确实接了清单。只看「没有写死」不够 —— 两条路都不走也是「没写死」。
    assert "buildRoutes(" in _APP_TSX, "App.tsx 没调用 buildRoutes：它到底怎么拿到路由的？"
    assert "usePluginCatalog(" in _APP_TSX, "App.tsx 没读能力清单"


def test_studio_nav_no_longer_hardcodes_nav_items():
    """`StudioNav.tsx` 不许再有硬编码导航数组，也不许写死链接目标。

    只检查「旧数组没了」是不够的（删掉数组留个空 `[]` 也算删），所以正面要求它
    调用 `navItems(...)`。
    """
    assert not re.search(r"const\s+(START_ITEMS|MORE_ITEMS)\b", _NAV_TSX), (
        "StudioNav.tsx 里还有 START_ITEMS / MORE_ITEMS 硬编码数组"
    )
    literals = re.findall(r'\bto="(/[^"]*)"', _NAV_TSX)
    assert not literals, f"StudioNav.tsx 里写死了链接目标 {literals}；应统一用 item.path"
    assert "navItems(" in _NAV_TSX, "StudioNav.tsx 没从清单取导航（navItems）"
    assert "usePluginCatalog(" in _NAV_TSX, "StudioNav.tsx 没读能力清单"


# 信息架构的**冻结快照**（2026-09-20 第 4 步落地时的状态）。
#
# 为什么这份冻结、而 `test_plugin_loader.py` 里「路由挂载顺序」那份冻结被删了？
#   · 挂载顺序**没有语义**（09-20 实测 139 条路由 0 重叠，见 `docs/插件化设计.md` §7 更正块），
#     冻结它只会带来无意义的快照 churn；
#   · 导航的信息架构**是产品决定** —— 「极简模式 = 首页 + 三条主路径」（09-15）、
#     「试音间进开始组」（09-18）都是明确拍板的，改动必须有意为之，不能顺手。
#
# 第 4 步之后清单是 IA 的**唯一**来源：改 `plugin.json` 就是改产品。没有这两条门禁，
# 少一个 nav 项、挪一下 `order`、删一条旧路由重定向，不会有任何东西发现。
_FROZEN_ROUTES: tuple[tuple[str, str, str], ...] = (
    # (path, module, export)
    ("/home", "Home", "HomeRoute"),
    ("/voices", "Voices", "VoicesRoute"),
    ("/pet-market", "PetMarket", "PetMarketRoute"),
    ("/audition", "Audition", "AuditionRoute"),
    ("/offlinevc", "OfflineVc", "OfflineVcRoute"),
    ("/live", "Live", "LiveRoute"),
    ("/tts", "Tts", "TtsRoute"),
    ("/workshop", "Workshop", "WorkshopRoute"),
)

_FROZEN_LEGACY: tuple[tuple[str, str], ...] = (
    # (path, redirect) —— 旧书签不能 404
    ("/market", "/voices?tab=market"),
    ("/wechat", "/tts?tab=wechat"),
    ("/audiobook", "/tts?tab=book"),
    ("/effects", "/offlinevc?tab=fx"),
    ("/ft", "/workshop?tab=ft"),
    ("/cascade", "/live?tab=qwen"),
    ("/qwen", "/live?tab=qwen"),
    ("/discover", "/workshop?tab=discover"),
)

_FROZEN_NAV: tuple[tuple[str, str, str, str, int], ...] = (
    # (path, label, icon, group, order) —— 已按 group + order 排成用户看到的先后
    ("/home", "首页", "Home", "start", 10),
    ("/audition", "试音间", "AudioLines", "start", 20),
    ("/tts", "输字变声", "Speech", "start", 30),
    ("/workshop", "训练变声", "Mic2", "start", 40),
    ("/offlinevc", "工具箱", "Wrench", "start", 50),
    ("/voices", "我的音色", "Library", "more", 10),
    ("/live", "实时变声", "Radio", "more", 20),
    ("/pet-market", "桌宠皮肤", "PawPrint", "more", 30),
)


def test_manifest_routes_are_the_frozen_page_set():
    """真页面 + 旧路由重定向 = 冻结快照。少一条、改一条路径都要显式改这里。"""
    got = tuple(
        (r["path"], r["module"], r["export"]) for p in plugin_manifest.load_all() for r in p.routes
    )
    assert sorted(got) == sorted(_FROZEN_ROUTES), (
        f"页面集合变了（对称差）：{sorted(set(got) ^ set(_FROZEN_ROUTES))}"
    )
    got_legacy = tuple(
        (r["path"], r["redirect"]) for p in plugin_manifest.load_all() for r in p.legacy_routes
    )
    assert sorted(got_legacy) == sorted(_FROZEN_LEGACY), (
        f"旧路由重定向变了（对称差）：{sorted(set(got_legacy) ^ set(_FROZEN_LEGACY))}"
    )


def test_manifest_nav_is_the_frozen_information_architecture():
    """侧边栏的（路径 / 标签 / 图标 / 分组 / 组内序）= 冻结快照。

    `order` 也是 IA 的一部分：它决定用户在侧栏看到的先后。
    """
    got = tuple(
        (r["path"], r["nav"]["label"], r["nav"]["icon"], r["nav"]["group"], r["nav"]["order"])
        for p in plugin_manifest.load_all()
        for r in p.routes
        if r.get("nav")
    )
    assert sorted(got) == sorted(_FROZEN_NAV), (
        f"导航 IA 变了（对称差）：{sorted(set(got) ^ set(_FROZEN_NAV))}"
    )
    # 独立于快照的不变量：每个真页面都得有导航项，否则页面存在但**侧栏里找不到入口**
    # （旧路由不需要 nav —— 它们只是重定向，不是页面）
    pages = {r["path"] for p in plugin_manifest.load_all() for r in p.routes}
    navs = {row[0] for row in _FROZEN_NAV}
    assert pages == navs, f"有页面没有导航项（用户找不到入口）：{sorted(pages - navs)}"


def test_declared_page_module_and_export_exist():
    """`routes[].module/export` 必须在 `web/src/pages/<module>/index.tsx` 里真的是那个导出。

    页面全是**具名导出**（`Home/index.tsx` → `HomeRoute`），没有 default export，
    所以 `import.meta.glob` 必须指名取哪个 —— 名字打错 = 白屏，且在构建期不报错。

    覆盖**全部**插件（原来只抽查 3 个）。「glob 里到底有没有这个 key」那一层
    由 `web/src/lib/pluginRoutes.test.ts` 验（那要真跑 JS）。
    """
    plugins = [p for p in plugin_manifest.load_all() if p.routes]
    assert plugins, "没有任何插件声明 routes —— 清单是不是整个坏了？"
    problems: list[str] = []
    checked = 0
    for plugin in plugins:
        for r in plugin.routes:
            page = _ROOT / "web" / "src" / "pages" / r["module"] / "index.tsx"
            if not page.is_file():
                problems.append(f"{plugin.id}: 找不到页面 {page}")
                continue
            src = page.read_text(encoding="utf-8")
            if not re.search(
                rf"(?m)^export\s+(?:default\s+)?(?:function|const)\s+{re.escape(r['export'])}\b", src
            ):
                problems.append(f"{plugin.id}: {r['module']}/index.tsx 里没有导出 {r['export']}")
            checked += 1
    assert not problems, "；".join(problems)
    assert checked == len(_FROZEN_ROUTES), f"只核了 {checked} 条页面，期望 {len(_FROZEN_ROUTES)}"


# ------------------------------------------------------- 端点 / 存储保护


def test_plugins_endpoint_shape_and_consistency():
    """`GET /api/plugins` 的形状，以及与 `/api/capabilities` 的数字不矛盾。"""
    from fastapi.testclient import TestClient

    import server

    _load_all()
    client = TestClient(server.app)
    body = client.get("/api/plugins").json()
    caps = client.get("/api/capabilities").json()

    assert body["counts"]["total"] == len(plugin_manifest.load_all())
    assert body["loaders"]["routers"] == caps["total"]
    assert body["loaders"]["loaded"] == caps["loaded"]
    assert sorted(body["loaders"]["broken"]) == sorted(b["module"] for b in caps["broken"])
    one = body["plugins"][0]
    assert {
        "id", "name", "kind", "category", "order", "summary", "core",
        "state", "reasons", "requires", "routers", "routes", "legacyRoutes",
        "extras", "health", "disableNote",
    } <= set(one)


def test_plugins_endpoint_does_not_import_heavy_libs():
    """同 `/capabilities`：状态端点不能依赖它要报告的那个东西（`/health` 的反例）。"""
    import system_api

    code = inspect.getsource(system_api.plugins).replace(system_api.plugins.__doc__ or "", "")
    assert "torch" not in code


def test_plugins_json_is_not_a_storage_cleanup_target(tmp_path, monkeypatch):
    """`outputs/plugins.json` 不能被"存储清理"顺手删掉。

    用户把一个能力关掉是**配置**不是垃圾；清理面板一键下去回到全开，
    属于典型的"越用越怪"。判定不放真删（`clean()` 会真的 unlink，而它有一项目标
    指向 `cfg.RVC_ROOT` 的权重 —— 绝不能在测试里跑），改为**只读地问每一项目标
    "你包含了哪些文件"：这正是 `clean()` 删的同一份清单。
    """
    import storage

    monkeypatch.setattr(storage.cfg, "OUTPUTS_DIR", tmp_path)
    keep = tmp_path / "plugins.json"
    keep.write_text(json.dumps({"disabled": ["sound.tts"]}), encoding="utf-8")
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"0" * 16)

    matched: set[Path] = set()
    for _key, (_label, _desc, getter, _cleanable) in storage._TARGETS.items():
        matched.update(Path(p) for p in getter())
    assert keep not in matched, "plugins.json 会被存储清理删掉"
    # 用例自检：getter 确实看到了这个临时 outputs 目录（否则上面那条是假绿）
    assert junk in matched, "getter 没扫到临时目录 —— 这条用例本身没生效"
