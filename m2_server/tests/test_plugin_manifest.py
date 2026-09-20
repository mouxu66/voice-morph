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
| 清单与**前端**（路由 + 侧边栏）对得上 | `test_frontend_...` |
| 用户配置不会被存储清理顺手删掉 | `test_plugins_json_...` |
| `/api/plugins` 与 `/api/capabilities` 不互相矛盾 | `test_plugins_endpoint_...` |

> 「路由注册顺序不许重排」这条**已从本文件移出**：2026-09-20 第 3 步实测（139 条路由）
> 没有任何两条 router 路由互相遮蔽，顺序对 handler 归属没有影响。该守的改成了
> 「不许出现重叠」→ `tests/test_route_shadowing.py`。
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


def test_extras_python_are_declared_dependencies():
    """`extras.python` 里的包名必须**在仓库某处真的被装过**。

    防的是"凭印象写依赖"（例如给微调写上 peft —— 那份依赖其实在 RVC 整合包里，
    这个仓库的主环境从未声明它）。判定口径就取三个真实的安装入口。
    """
    sources = "\n".join(
        (_ROOT / f).read_text(encoding="utf-8")
        for f in ("requirements.txt", "requirements-dev.txt", "tools/setup_env.ps1")
    )
    for p in plugin_manifest.load_all():
        for pkg in p.extras.get("python", []):
            assert re.search(rf"\b{re.escape(pkg)}\b", sources), (
                f"{p.id}: {pkg} 不在 requirements*.txt / setup_env.ps1 里 —— 不要凭印象写依赖"
            )


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


def _app_route_paths() -> set[str]:
    return set(re.findall(r'<Route\s+path="([^"]+)"', _APP_TSX))


def _nav_items(group: str) -> list[tuple[str, str, str]]:
    """从 `StudioNav.tsx` 里取某组的 `(path, label, icon)`，按源码顺序。

    两组的名字就是源码里那两个数组（`START_ITEMS` / `MORE_ITEMS`）—— 只取那一段，
    避免把文件里其它形如 `{ path: … }` 的对象一起卷进来。
    """
    const = "START_ITEMS" if group == "start" else "MORE_ITEMS"
    m = re.search(rf"const {const}: NavItem\[\] = \[(.*?)\n\]", _NAV_TSX, re.S)
    assert m, f"StudioNav.tsx 里找不到 {const}"
    return re.findall(
        r'\{\s*path:\s*"([^"]+)",\s*label:\s*"([^"]+)",\s*icon:\s*([A-Za-z0-9_]+)', m.group(1)
    )


def test_manifest_routes_match_app_tsx():
    """清单声明的路由必须与 `App.tsx` 里真实渲染的路由**完全一致**。

    第 4 步要拿这份清单去替掉 `App.tsx` 的硬编码，所以现在就得对账 ——
    否则等替换那天才发现清单少了一条旧路由（旧书签会 404，而且没人会立刻注意到）。
    """
    declared = {r["path"] for p in plugin_manifest.load_all() for r in p.routes}
    declared |= {r["path"] for p in plugin_manifest.load_all() for r in p.legacy_routes}
    actual = _app_route_paths()
    # `/` 与 `*` 是外壳（重定向到首页/兜底），不属于任何插件
    assert declared == actual - {"/", "*"}, f"只在 App.tsx：{sorted(actual - declared - {'/', '*'})}"


def test_manifest_nav_matches_studio_nav():
    """侧边栏项（标签/图标/分组）要与 `StudioNav.tsx` 逐条对上，**顺序也要**。

    导航顺序就是产品的信息架构（“开始”组的主路径 + “更多功能”），
    清单若与它对不上，第 4 步切过去就会把侧边栏悄悄重排 —— 那种改动很难在
    code review 里被看见（"只是换了个顺序"），所以拿断言钉住。
    """
    declared = [
        (p.id, r["path"], r["nav"]["label"], r["nav"]["icon"], r["nav"]["group"], r["nav"]["order"])
        for p in plugin_manifest.load_all()
        for r in p.routes
        if r.get("nav")
    ]
    declared_paths = {row[1] for row in declared}
    for group in ("start", "more"):
        want = [(path, label, icon) for path, label, icon in _nav_items(group)]
        got = sorted(
            [(path, label, icon) for _, path, label, icon, g, _o in declared if g == group],
            key=lambda row: next(o for _, p2, _l, _i, g2, o in declared if p2 == row[0] and g2 == group),
        )
        assert got == want, f"{group} 组不一致：{got} != {want}"
    # 反向：StudioNav 里的项必须都在清单里（漏了会让侧边栏在替换后少一项）
    assert declared_paths == {p for g in ("start", "more") for p, _, _ in _nav_items(g)}


@pytest.mark.parametrize("plugin_id", ["core.system", "sound.tts", "pet.market"])
def test_declared_page_module_and_export_exist(plugin_id):
    """`routes[].module/export` 必须在 `web/src/pages/<module>/index.tsx` 里真的是那个导出。

    页面全是**具名导出**（`Home/index.tsx` → `HomeRoute`），没有 default export，
    所以 `import.meta.glob` 必须指名取哪个 —— 名字打错 = 白屏，且在构建期不报错。
    """
    plugin = next(p for p in plugin_manifest.load_all() if p.id == plugin_id)
    for r in plugin.routes:
        page = _ROOT / "web" / "src" / "pages" / r["module"] / "index.tsx"
        assert page.is_file(), f"{plugin_id}: 找不到页面 {page}"
        src = page.read_text(encoding="utf-8")
        assert re.search(rf"(?m)^export\s+(?:default\s+)?(?:function|const)\s+{re.escape(r['export'])}\b", src), (
            f"{plugin_id}: {r['module']}/index.tsx 里没有导出 {r['export']}"
        )


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
