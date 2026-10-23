"""插件清单（manifest）：把「哪些能力存在、它们要什么、缺了会怎样」变成机器可读。

这一步（`docs/插件化设计.md` 第 2 步）**只声明，不改行为**：
`server.py` 照旧用 `_register(...)` 挂载全部 26 个 router，本模块只负责
把 `m2_server/plugins/<id>/plugin.json` 读进来、校验、算出每个插件的状态，
供只读的 `GET /api/plugins` 输出。

为什么值得单独一步
------------------
同一份「能力知识」现在散在四处，且各会漂：

    1. README 的「可选能力各自独立」散文表
    2. tools/doctor.py 的 run_checks()
    3. system_api.py 的 /diagnose
    4. 前端各页自己写的「未就绪」提示

manifest 是给这四处准备的**唯一真相源**。本步先把源头建起来并对齐现实，
不急着去改那四个消费者（那是第 3/6 步）。

状态为什么是三态
----------------
`ok` / `broken` / `disabled` 必须分开。前端 `SetupBanner` 的弹出条件是
「有 broken」（见 `web/src/components/ModelSetupPanel.tsx` 里的 `caps.broken`），
所以**用户主动关掉的插件绝不能被算进 broken** —— 否则关一个就弹一条
「N 个能力未加载」的告警，把「你关的」和「坏掉的」混为一谈。

`broken` 的判据来自 `plugin_loader` 的 registry（导入是否成功），
这比在这里重新 import 一次更准：registry 记录的就是**启动时真实发生的事**。

状态文件放哪 / 为什么用 disabled 而不是 enabled
-----------------------------------------------
`outputs/plugins.json`，与其它后端状态文件同处（`cascade_state.json`、
`live_asr_state.json`、`pet-skin-state.json` 都在 `cfg.OUTPUTS_DIR`）。
**不用 `%APPDATA%`**：设计稿原先写的是那里，但后端必须能脱离 Electron 独立跑
（局域网/手机访问场景没有 appConfig），而 outputs/ 已是后端自己的状态目录。

键名用 `disabled` 而不是 `enabled`：**新加的插件默认必须是开的**。
用白名单（enabled）的话，以后每加一个插件，老用户都被默认排除在外。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config as cfg
import plugin_loader

PLUGINS_DIR = Path(__file__).resolve().parent / "plugins"
STATE_FILE = cfg.OUTPUTS_DIR / "plugins.json"


def _seed_marker() -> Path:
    """播种标记：与状态文件同目录的 `plugins.seeded`。

    为什么做成**函数**而不是常量：测试夹具只 `monkeypatch` `STATE_FILE`（见
    `conftest.py::_isolated_plugin_state`）—— 标记必须跟着它走，常量会在
    导入期求值、对 patch 无感知（同 `docs/犯错档案-工程.md` §8.36 的早绑定坑）。
    """
    return STATE_FILE.with_name("plugins.seeded")

STATE_OK = "ok"
STATE_BROKEN = "broken"
STATE_DISABLED = "disabled"

# 顶层字段里「必须有」的那些；缺失即 ManifestError（宁可启动时大声报，
# 也别让一个半截 manifest 静默生效）
_REQUIRED = ("id", "name", "kind", "category", "order", "summary", "routers")
_CATEGORIES = ("core", "sound", "pet", "hook")
_KINDS = ("builtin", "user")
# 侧栏分组（2026-09-26 起四组：用户反馈"两个 rvc 变声 + 千问 + 特效，有点多"——
# 按心智模型分组而不是平铺：开始 / 变声 / 音色 / 桌面。前端 StudioNav 按
# NAV_GROUP_LABELS 渲染；组的**显示顺序**也由它定，别只加 id 不改前端。）
_NAV_GROUPS = ("start", "vc", "voices", "desktop")
_HOOK_WHEN = ("import", "main")
_EXTERNAL_KINDS = ("dir", "app", "audio-device")


class ManifestError(RuntimeError):
    """清单本身写错了（不是「这个能力不可用」）。"""


@dataclass(frozen=True)
class Plugin:
    id: str
    name: str
    kind: str
    category: str
    order: int
    summary: str
    routers: tuple[str, ...]
    requires: tuple[str, ...] = ()
    hooks: tuple[dict[str, str], ...] = ()
    routes: tuple[dict[str, Any], ...] = ()
    legacy_routes: tuple[dict[str, str], ...] = ()
    extras: dict[str, Any] = field(default_factory=dict)
    health: dict[str, str] | None = None
    disable_note: str = ""
    path: str = ""

    @property
    def is_core(self) -> bool:
        """核心插件不可关（关掉它整个界面就没有意义了）。"""
        return self.category == "core"


def _as_str_tuple(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
        raise ManifestError(f"{where} 必须是字符串数组")
    return tuple(raw)


def _parse(entry: dict[str, Any], path: Path) -> Plugin:
    for key in _REQUIRED:
        if key not in entry:
            raise ManifestError(f"{path} 缺字段 {key!r}")
    pid = entry["id"]
    if not isinstance(pid, str) or not pid or "." not in pid:
        raise ManifestError(f"{path}: id 必须形如 'sound.tts'（至少一段点），实际 {pid!r}")
    if entry["kind"] not in _KINDS:
        raise ManifestError(f"{path}: kind 必须是 {_KINDS} 之一")
    if entry["category"] not in _CATEGORIES:
        raise ManifestError(f"{path}: category 必须是 {_CATEGORIES} 之一")
    if not isinstance(entry["order"], int):
        raise ManifestError(f"{path}: order 必须是整数")

    hooks: list[dict[str, str]] = []
    for h in entry.get("hooks") or []:
        if not isinstance(h, dict) or not h.get("module") or not h.get("attr"):
            raise ManifestError(f"{path}: hooks 每条必须是 {{module, attr, when}}")
        when = h.get("when", "import")
        if when not in _HOOK_WHEN:
            raise ManifestError(f"{path}: hook.when 必须是 {_HOOK_WHEN} 之一")
        hooks.append({"module": h["module"], "attr": h["attr"], "when": when})

    routes: list[dict[str, Any]] = []
    for r in entry.get("routes") or []:
        if not isinstance(r, dict) or not r.get("path"):
            raise ManifestError(f"{path}: routes 每条必须有 path")
        for key in ("module", "export"):
            if not r.get(key):
                raise ManifestError(f"{path}: routes[{r['path']}] 缺 {key!r}（页面是具名导出，必须指名）")
        nav = r.get("nav")
        if nav is not None:
            if nav.get("group") not in _NAV_GROUPS:
                raise ManifestError(f"{path}: routes[{r['path']}].nav.group 必须是 {_NAV_GROUPS} 之一")
            if not nav.get("label"):
                raise ManifestError(f"{path}: routes[{r['path']}].nav 缺 label")
        routes.append(dict(r))

    legacy: list[dict[str, str]] = []
    for lr in entry.get("legacy_routes") or []:
        if not isinstance(lr, dict) or not lr.get("path") or not lr.get("redirect"):
            raise ManifestError(f"{path}: legacy_routes 每条必须有 path 与 redirect")
        legacy.append({"path": lr["path"], "redirect": lr["redirect"]})

    extras = entry.get("extras") or {}
    if not isinstance(extras, dict):
        raise ManifestError(f"{path}: extras 必须是对象")
    for key in ("python", "external", "models"):
        val = extras.get(key)
        if val is not None and not isinstance(val, list):
            raise ManifestError(f"{path}: extras.{key} 必须是数组")
    for ext in extras.get("external") or []:
        if not isinstance(ext, dict) or ext.get("kind") not in _EXTERNAL_KINDS:
            raise ManifestError(f"{path}: extras.external[].kind 必须是 {_EXTERNAL_KINDS} 之一")
        if not ext.get("label"):
            raise ManifestError(f"{path}: extras.external[] 缺 label")

    health = entry.get("health")
    if health is not None and (not isinstance(health, dict) or not health.get("module") or not health.get("attr")):
        raise ManifestError(f"{path}: health 必须是 null 或 {{module, attr}}")

    return Plugin(
        id=pid,
        name=entry["name"],
        kind=entry["kind"],
        category=entry["category"],
        order=entry["order"],
        summary=entry["summary"],
        routers=_as_str_tuple(entry["routers"], f"{path}: routers"),
        requires=_as_str_tuple(entry.get("requires"), f"{path}: requires"),
        hooks=tuple(hooks),
        routes=tuple(routes),
        legacy_routes=tuple(legacy),
        extras=extras,
        health=health,
        disable_note=(entry.get("disable") or {}).get("note", "") if isinstance(entry.get("disable"), dict) else "",
        path=str(path.relative_to(PLUGINS_DIR.parent.parent)),
    )


def _validate(plugins: list[Plugin]) -> None:
    """跨文件的一致性检查：id/order 唯一、依赖存在、无环、router 不重复认领。"""
    ids = [p.id for p in plugins]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise ManifestError(f"插件 id 重复：{sorted(dup)}")
    known = set(ids)

    for p in plugins:
        if not p.routers:
            raise ManifestError(f"{p.id}: routers 不能为空（一个不挂 router 的插件不该存在）")
        for req in p.requires:
            if req not in known:
                raise ManifestError(f"{p.id}: requires 指向不存在的插件 {req!r}")
            if req == p.id:
                raise ManifestError(f"{p.id}: requires 不能指向自己")

    # 每个 router 模块只能被一个插件认领 —— 这是「唯一真相源」的地基：
    # 两个插件都声明同一个模块，关掉其中一个到底该不该卸载路由就没有答案了。
    seen: dict[str, str] = {}
    for p in plugins:
        for mod in p.routers:
            if mod in seen:
                raise ManifestError(f"router {mod!r} 被 {seen[mod]} 与 {p.id} 同时认领")
            seen[mod] = p.id

    orders = [p.order for p in plugins]
    dup_order = {o for o in orders if orders.count(o) > 1}
    if dup_order:
        raise ManifestError(f"order 重复：{sorted(dup_order)}")

    # 环检测（DFS；插件数量小，朴素实现足够）
    by_id = {p.id: p for p in plugins}
    state: dict[str, int] = {}

    def visit(pid: str, stack: list[str]) -> None:
        if state.get(pid) == 2:
            return
        if state.get(pid) == 1:
            raise ManifestError(f"requires 成环：{' -> '.join([*stack, pid])}")
        state[pid] = 1
        for dep in by_id[pid].requires:
            visit(dep, [*stack, pid])
        state[pid] = 2

    for p in plugins:
        visit(p.id, [])


_CACHE: list[Plugin] | None = None
_LOCK = threading.Lock()


def load_all(*, refresh: bool = False) -> list[Plugin]:
    """读取并校验全部清单，按 `order` 排序。结果缓存（清单是随包发的静态文件）。"""
    global _CACHE
    with _LOCK:
        if _CACHE is not None and not refresh:
            return list(_CACHE)
        if not PLUGINS_DIR.is_dir():
            raise ManifestError(f"找不到插件目录 {PLUGINS_DIR}")

        plugins: list[Plugin] = []
        for manifest in sorted(PLUGINS_DIR.glob("*/plugin.json")):
            try:
                entry = json.loads(manifest.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ManifestError(f"{manifest} 不是合法 JSON：{exc}") from exc
            if not isinstance(entry, dict):
                raise ManifestError(f"{manifest} 顶层必须是对象")
            plugins.append(_parse(entry, manifest))
        if not plugins:
            raise ManifestError(f"{PLUGINS_DIR} 下没有任何 */plugin.json")
        _validate(plugins)
        plugins.sort(key=lambda p: p.order)
        _CACHE = plugins
        return list(plugins)


def reset_cache() -> None:
    """清缓存 —— **只给测试用**。"""
    global _CACHE
    with _LOCK:
        _CACHE = None


def mount_plan(*, include_disabled: bool = False) -> list[tuple[str, str]]:
    """挂载计划：`[(plugin_id, router_module), ...]`，**顺序即 `include_router` 的顺序**。

    顺序 = 插件 `order` 升序（`load_all()` 已排好），插件内按 `routers` 的声明序。
    这是 `server.py` 唯一的模块名来源 —— 它不再手写 26 个模块名。

    为什么顺序要有个明确出处：FastAPI 按注册顺序匹配路由，两条路由互相遮蔽时
    **先注册的那个接**，所以挂载顺序是**行为**，不该由「谁先被写进 server.py」决定。

    但也要说清这份顺序**今天**意味着什么（2026-09-20 第 3 步实测，139 条真实路由）：
    没有任何两条 router 路由互相遮蔽，65 条遮蔽关系**全部**是「router vs SPA 兜底」，
    低优先级路由 / websocket / Mount 均为 0。所以「历史交错序 → 清单 order」这次
    搬家是**行为等价**的。真正的守卫是 `tests/test_route_shadowing.py` ——
    一旦有人引入重叠，它直接点名是哪两条路径、属于哪个插件，
    而不是靠一份没人解释得清的历史顺序去兜。

    注意：这里**不**跳过 `disabled` 的插件。挂载只认清单的**结构**，「开关」是第 6 步
    的事 —— 现在跳过会立刻改变行为：第 4 步还没让前端按清单渲染路由，用户关掉一个
    能力会直接看到 404，而关闭守卫（核心不可关、依赖传播、409）也都还没做。
    第 6 步起**默认跳过 `disabled` 的插件**：关掉一个能力就要真的不 import 它
    （不拉 torch/CUDA 上下文、不吃显存），只让前端不显示是「省了个入口，没省资源」。
    跳过的模块记进 `_SKIPPED`，好让 `state_of()` 不把「你关的」报成「未注册」。
    要**结构全貌**（对账/遮蔽检查）时传 `include_disabled=True`。
    """
    on = enabled_ids() if not include_disabled else None
    plan: list[tuple[str, str]] = []
    for p in load_all():
        if on is not None and p.id not in on:
            for module in p.routers:
                _SKIPPED.add((module, plugin_loader.ROUTER_PURPOSE))
            continue
        plan.extend((p.id, module) for module in p.routers)
    return plan


def hooks_for(when: str, *, include_disabled: bool = False) -> list[dict[str, str]]:
    """取出声明为 `when` 阶段的启动钩子，按插件 `order` 排序。

    `when` 只有两个取值（`_HOOK_WHEN`）：

    - `"import"`：随 app 装配一起跑（`server.py` 模块级）。错放这里 → 每次
      `import server` 都产生副作用，测试会被牵连。
    - `"main"`：只在 `python server.py` 入口跑。错放那里 → 打包/测试环境下
      这个副作用永远不发生。
    """
    if when not in _HOOK_WHEN:
        raise ValueError(f"when 必须是 {_HOOK_WHEN} 之一，实际 {when!r}")
    on = enabled_ids() if not include_disabled else None
    out: list[dict[str, str]] = []
    for p in load_all():
        if on is not None and p.id not in on:
            for hook in p.hooks:
                _SKIPPED.add((hook["module"], hook["attr"]))
            continue
        out.extend(hook for hook in p.hooks if hook["when"] == when)
    return out


def disabled_ids() -> set[str]:
    """读 `outputs/plugins.json` 的 `disabled` 列表；文件缺失/损坏一律当「一个都没关」。

    损坏时不当成错误：这是用户级配置，读坏了不该让 /api/plugins 变成 500。
    """
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(raw, dict):
        return set()
    items = raw.get("disabled")
    if not isinstance(items, list):
        return set()
    return {x for x in items if isinstance(x, str)}


# ---------------------------------------------------------------- 开关（第 6 步）
#
# 前 5 步都在建「声明」，从第 6 步起声明要**改变行为**：关掉的能力不再挂载、
# 不再出现在前端、不再装它的重依赖。三条判据必须先钉死，否则会出现
# 「关掉 A 之后 B 变砖」这类比不关更糟的结果：
#
#   1. 核心（`core.*`）不可关 —— 关掉整个界面就没有意义了。
#   2. 仍有**启用中**的插件 `requires` 它 → 不许关（409 并在 detail 里点名依赖者）。
#      这是设计稿 §6.3 的关闭守卫。
#   3. 被关掉但**仍被依赖**的插件要保留启用 —— 与第 2 条互补：
#      用户先关 A（当时没人依赖它），后来又开了依赖 A 的 B，A 必须复活。

# 套餐预设：插件 id 的**起点**（`docs/插件化设计.md` §8.1 的表）。
#   None = 全开。`core.*` 与 `requires` 闭包由 `expand()` 补上。
# 放本模块而不是 `tools/plugin_extras.py`：**后端也要用它** —— 设置页要展示预设、
# 开关接口要套用预设，两处不能各写一份。
PRESETS: dict[str, list[str] | None] = {
    # 轻量：只要一个能变声的，硬盘紧张
    "light": ["sound.offline-vc"],
    # ★ 标准（默认）= **变声本体**（2026-09-23 用户拍板）。
    #
    # 就是「选音色 → 变 → 听」这个最小闭环：
    #   core.voices（选音色）+ sound.offline-vc / sound.rvc-live（变：文件·麦克风 / 实时）
    #   + sound.audition（听：A/B 试听）。`core.*` 与 `requires` 闭包由 `expand()` 补上。
    #
    # 其余 9 项（训练 `sound.workshop`、微调 `sound.ft`、发掘 `sound.mine`、
    # 输字变声 `sound.tts`、有声书、效果器、桌宠、皮肤、微信）**默认关闭**，
    # 由用户在设置页按需开启 —— 这就是「一切皆插件」真正落地的那一步。
    #
    # 值得做的理由**不是**省安装包体积（实测只有 ~0.1%，见 §8.2），而是**首次体验路径**：
    # 新用户第一屏该看到"能变声"，而不是 20 项能力的配置表（同 §8.1 引的 DSH 教训）。
    "standard": ["sound.offline-vc", "sound.rvc-live", "sound.audition"],
    # ── 场景模式三件套（2026-10-03 用户拍板）：按「用户是谁」分组，不按「功能多少」。
    # 打游戏的用不上微信、录課老师用不上实时变声 —— 套餐只是**起点**，套用后仍可在
    # 设置页逐项开关；禁用的插件其路由/首页入口整个下架（前端按 enabled 过滤）。
    # 微信变声：变声本体 + 微信发送 + 特效声板（[爆炸] 等音效标记只在微信链路生效）。
    # ★ sound.tts 必须带：微信发送 UI 是输字变声页的 tab（/tts?tab=wechat），
    #   不带 tts 等于 tab 宿主页整个下架（有声书同理挂在 /tts?tab=book）。
    # ★ sound.course 必须带（2026-10-08）：录课零额外依赖（python=[]，出片才要
    #   PowerPoint），不该被场景切换埋掉 —— 用户三轮"开了又没"全是切模式所致。
    "wechat": ["sound.offline-vc", "sound.tts", "hook.wechat", "sound.fx-board", "sound.course", "hook.radar", "hook.assistant"],
    # 声音保存（护嗓）：建档录老师的声音 → 输字让它说话 → 整篇有声书 → 录课出片。
    # 有声书 requires tts，闭包自动补上；不带实时变声/微信 —— 课堂场景用不上。
    # ★ sound.course 必须带（2026-10-08 用户拍板）：这个模式的目标用户就是录课老师，
    #   「PPT + 音色 → 讲课视频」是该场景的主打闭环 —— 漏掉它等于模式切换一次
    #   录课就消失一次（真实事故：用户在 preserve 模式下怎么开都留不住）。
    "preserve": ["sound.preserve", "sound.tts", "sound.audiobook", "sound.course"],
    # 游戏变声：游戏 / 直播 / 会议里实时变声 —— 就是变声本体本身（rvc-live 走虚拟声卡）。
    "game": ["sound.offline-vc", "sound.rvc-live", "sound.audition"],
    # 全能：全开**除停用路线**（`RETIRED`，见下）—— 停用是产品口径，不是用户的一时开关，
    # 所以连「全能」也不带它；想用回可以逐项开启（`set_enabled`，可逆）。
    "full": None,
    # 仅核心：连离线变声都不要（跑测试 / CI 用）
    "core": [],
}

#: `standard` 的成员 = 变声本体的**唯一定义**。`test_plugin_switch.py` 钉住它，
#: 因为这是产品决定，改动必须有意为之（同 `test_core_plugins_are_declared_core` 的道理）。
BODY_IDS: tuple[str, ...] = ("sound.offline-vc", "sound.rvc-live", "sound.audition")

DEFAULT_PRESET = "standard"

#: 已停用路线：**产品口径上不再提供**的能力。任何预设（含「全能」）都不带它 ——
#: 但**不删代码、不碰用户的开关**：`RETIRED` 只作用在 `preset_ids()` 这一处，
#: 用户依旧可以在设置页把它手动开回来（走 `set_enabled`），已发布产物照旧可用。
#:
#: 为什么要有这一个概念（而不是直接把插件删掉）：删掉不可逆，而「不再作为默认路线」
#: 可逆；两者的证据强度不同 —— 停用只需要一个产品决定，删除需要一条长期没人再用的记录。
#:
#: 2026-10-14 用户拍板：克隆自己的声音**统一走「音色训练」(RVC)**，千问微调不再用 ——
#: 依据 `docs/product/research/m1f_千问微调vsRVC-声音克隆裁决_2026-10-14.md`：
#: 同素材同文本同参考音下，微调相对未微调仅 +0.0024（落在自身 σ=0.0361 内），
#: 而 RVC +0.035~+0.044 且逐句 3/3、跨 seed 区间不重叠。
RETIRED: dict[str, str] = {
    "sound.ft": "克隆统一走「音色训练」(RVC)：千问微调不提升音色像度（2026-10-14 裁决）",
}


PRESET_LABELS: dict[str, str] = {
    "light": "轻量",
    # 「本体」写进标签：设置页那一排按钮是用户唯一能看见"标准到底含什么"的地方
    # ★ 术语：产品是「变声工坊」，一律叫「变声」。曾误写成「变身」（body-transformation
    #   的字面义），用户侧栏一眼看错，已全量改正 —— 改这里前先 grep「变身」。
    "standard": "标准（变声本体）",
    # 场景模式：名字直接说场景，用户不猜
    "wechat": "微信变声",
    "preserve": "声音保存（护嗓）",
    "game": "游戏变声",
    "full": "全能",
    "core": "仅核心",
}


def by_id() -> dict[str, Plugin]:
    return {p.id: p for p in load_all()}


def expand(ids) -> set[str]:
    """补上 `core.*` 与 `requires` 闭包。

    * `core.*` 不可关，无论预设怎么给都补上。
    * `requires` 是硬依赖：启用试音间就得启用离线变声，否则试音间起来也是 broken。
      清单里的 id 都已由 `_validate()` 校验存在，这里不重复抛。
    """
    plugins = by_id()
    out = {pid for pid in ids if pid in plugins}
    out |= {pid for pid, p in plugins.items() if p.is_core}
    queue = list(out)
    while queue:
        pid = queue.pop()
        for req in plugins[pid].requires:
            if req not in out:
                out.add(req)
                queue.append(req)
    return out


def preset_ids(name: str = DEFAULT_PRESET) -> set[str]:
    """预设展开成启用集。未知预设**直接抛**（拼错预设名应当当场炸，不是静默全开）。

    ★ 停用路线（`RETIRED`）**在这里被剔除**，`full`（`None` = 全开）也一样 ——
    "不再作为默认路线"是一句产品口径，不能在某个预设上留个后门。
    用户手动开启走 `set_enabled`，不经过这里，所以可逆性不受影响。
    """
    if name not in PRESETS:
        raise KeyError(f"未知预设 {name!r}，可选：{sorted(PRESETS)}")
    start = PRESETS[name]
    base = by_id().keys() if start is None else start
    return expand([pid for pid in base if pid not in RETIRED])


def enabled_ids(disabled: set[str] | None = None) -> set[str]:
    """当前**实际启用**的插件 id —— 与 `disabled_ids()` 不是简单互补。

    被关掉的插件**若仍被某个启用中的插件 `requires`，就必须保留**：
    用户先关 A（当时没人依赖它），后来开了依赖 A 的 B，A 得复活，
    否则 B 起来就是 broken。宁可多留一个能力，也不要让「关掉 A」把「还在用的 B」弄坏。

    复活是**传递闭包**（复活的 A 也有依赖 → 一并复活，直到不动为止）：
    否则会造出「A 启用了、A 依赖的 B 却还关着」的半吊子状态，B 照样把 A 拖成 broken。
    当前清单里没有「可选能力依赖可选能力再依赖可选能力」的链，这一跳今天到不了；
    用传递闭包钉住，是为了清单以后加一条就立刻正确，而不是那天再被咬一口。
    """
    off = set(disabled if disabled is not None else disabled_ids())
    plugins = by_id()
    on = {pid for pid in plugins if pid not in off}
    changed = True
    while changed:
        changed = False
        for pid in sorted(on):
            for req in plugins[pid].requires:
                if req in off:
                    off.discard(req)
                    on.add(req)
                    changed = True
    return on


def dependents_of(pid: str, disabled: set[str] | None = None) -> list[str]:
    """哪些**启用中**的插件 `requires` 了 `pid` —— 关闭守卫的判据（409 的 detail）。"""
    on = enabled_ids(disabled) - {pid}
    return sorted(p.id for p in by_id().values() if p.id in on and pid in p.requires)


def write_disabled(disabled) -> None:
    """写 `outputs/plugins.json`。原子替换：先写临时文件再 `replace`，
    避免写一半崩溃留下一个解析不了的 JSON（`disabled_ids()` 会把损坏当成
    「一个都没关」，即**全部启用**，那是比崩溃更危险的方向）。
    """
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"disabled": sorted(disabled)}, ensure_ascii=False, indent=2) + "\n"
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(STATE_FILE)
    # 写过盘就留凭证：此后「文件消失」是误删，不再等于「全新安装」（见 ensure_state_file）。
    _seed_marker().touch()


def set_enabled(pid: str, on: bool) -> dict:
    """开关一个能力。**不抛异常**：拦下来了就在返回值里说清是谁拦的。

    返回 `{id, enabled, blocked_by, reason}`：
    - `blocked_by` 非空 → 关闭守卫拦下（调用方应回 409）；
    - `reason == "core"` → 核心能力不可关；
    - 否则已写入，`enabled` 是写入后的真实状态。
    """
    plugins = by_id()
    if pid not in plugins:
        raise KeyError(f"没有这个能力：{pid!r}")
    if plugins[pid].is_core:
        return {"id": pid, "enabled": True, "blocked_by": [], "reason": "core"}

    off = set(disabled_ids())
    if on:
        off.discard(pid)
        blocked: list[str] = []
        reason = ""
    else:
        blocked = dependents_of(pid, off)
        if blocked:
            return {"id": pid, "enabled": True, "blocked_by": blocked, "reason": "dependents"}
        off.add(pid)
        reason = ""
    write_disabled(off)
    return {"id": pid, "enabled": on, "blocked_by": blocked, "reason": reason}


def apply_preset(name: str, *, strict: bool = False) -> dict:
    """套用一个预设。

    ⚠️ 写进文件的是**用户想关的**，不是「最终启用的」—— 因为被依赖而保留的
    能力不该被记成「用户主动开的」，否则切走再切回来会凭空多出几个开启项。
    保留逻辑统一留在读侧（`enabled_ids()`），一处权威。

    ★ 2026-10-15 用户拍板改语义：**默认「只增不减」（merge）**。

    改之前是「整套重设」：`off = 全部 - 套餐`，于是点一下模式就把用户手动开的能力
    全部写进 `disabled`。这个语义在「一开始就是某个模式」时无感，但用户一旦在能力面板
    里逐项开过（`_current_preset()` 判定为 `custom`），点模式就是**静默没收他的设置** ——
    2026-10-08 报过一次（17 项全开 → 重启 → 被模式选择页随手一点清零），当时的修法是
    「只补知情权、不改语义」（前端加一行会关掉哪几项的小字）。2026-10-15 用户明确：
    **知情权不够，这个语义本身不能这么搞**。

    所以现在默认只做**加法**：把套餐点名的能力从 `disabled` 里移除（开启），
    **绝不**往 `disabled` 里加任何东西 —— 你手动开的，模式动不了。
    真想「收窄到只剩这个套餐」走**显式的** `strict=True`（前端是一个单独的
    「严格套用（会关掉 N 项）」动作 + 确认，不再是点模式卡片的副作用）。

    返回 `mode` 与 `kept`：`kept` = 这次之后**仍在启用、但不属于该套餐**的能力
    （含被依赖复活的），前端据此如实告诉用户「保留了你手动开启的 N 项」。
    不要省掉它去猜 —— 用户此刻最想知道的正是「我开的东西掉没掉」。
    """
    keep = preset_ids(name)
    if strict:
        off = {p.id for p in load_all() if p.id not in keep}
    else:
        # 只从禁用集里**移除**套餐点名的那些；不动任何其它 id（含用户手动关的）
        off = disabled_ids() - keep
    write_disabled(off)
    on = enabled_ids(off)
    return {
        "preset": name,
        "mode": "strict" if strict else "merge",
        "disabled": sorted(off),
        "enabled": sorted(on),
        "kept": sorted(on - keep),
    }


#: 首次运行播种的开关。`0` = 不播种（测试用，同 `VM_WARMUP` / `VM_WECHAT_RESTART`）。
#: 它由**本模块**检查（而不是 `server.py` 的调用点），这样没有哪条调用路径能忘了带。
SEED_ENV = "VM_PLUGIN_SEED"


def ensure_state_file() -> set[str] | None:
    """首次运行时，把 `DEFAULT_PRESET` 落成一份**显式**状态；已存在则不动。

    返回写进去的 `disabled` 集合；没有写（已存在 / 被 `VM_PLUGIN_SEED=0` 关掉）时返回 `None`。

    为什么需要这一步 —— `disabled_ids()` 在文件缺失时返回空集，也就是「一个都没关」
    （= 20 项**全开**，而且 `_current_preset()` 会把它算成 `full`）。那是**刻意**的设计，
    被 `test_missing_or_broken_state_file_means_nothing_disabled` 钉住了，理由写在它的
    注释里：黑名单模型下**新插件默认是开的**。所以要让「本体」成为新用户的起点，
    只能写一份状态进去，不能改读取语义。

    为什么不去改读取语义（「文件缺失 ⇒ 用 `DEFAULT_PRESET`」）—— 那会同时弄坏两件事：
      ① 语法坏掉的配置会从「全开」（fail-open，用户自己再关）变成「只剩本体」
         （fail-closed，用户会找不到功能，而且不知道是自己配置坏了）；
      ② 「读一次配置」会变成**有副作用**的操作。
    宁可多一次显式的初始化写入，也不要让每次读都带上方 ① 的风险。

    ⚠️ **本函数必须在 `mount_plan()` 之前调用**：`mount_plan()` 是读着
    `disabled_ids()` 算出来的，晚一步就是「种了一份状态、但这次启动按旧的算」。
    唯一的调用点在 `server.py::_mount_all()` 的开头。

    **「全新安装」与「文件被删」必须区分开（2026-10-08 事故）**：用户删了状态文件，
    重启后被静默关回 18 项 —— 播种把「文件缺失」当成了「全新安装」，用户的选择
    被无声降级。现在播种时/写盘时都会留下标记（`_seed_marker()`）；标记在而文件没
    → 按读取语义走 fail-open（全开）并打日志，**不再重播种**。恢复默认套餐改成
    设置页里套用预设的**显式**动作，这是它本来就该有的语义。
    """
    if os.environ.get(SEED_ENV, "").strip() == "0":
        return None
    if STATE_FILE.exists():
        return None
    if _seed_marker().exists():
        print(
            f"[plugins] {STATE_FILE.name} 缺失但播种标记在 —— 视为被误删，"
            f"按全开处理；要恢复默认套餐请在设置页套用 {DEFAULT_PRESET!r}"
        )
        return None
    off = {p.id for p in load_all() if p.id not in preset_ids(DEFAULT_PRESET)}
    write_disabled(off)
    return off


# 挂载期**刻意跳过**的 (模块, 用途)。`state_of()` 靠它区分
# 「因为被关掉所以没加载」（正常）与「声称挂着却没挂上」（清单与挂载不一致，要报）。
_SKIPPED: set[tuple[str, str]] = set()


def skipped() -> set[tuple[str, str]]:
    return set(_SKIPPED)


def reset_skipped() -> None:
    """清跳过记账 —— **只给测试用**（与 `reset_cache()` 同理）。"""
    _SKIPPED.clear()


def state_of(plugin: Plugin, disabled: set[str] | None = None) -> tuple[str, list[str]]:
    """算出 `(state, reasons)`。

    - `disabled`：用户主动关掉。**不算 broken**（见模块注释三态的理由）。
    - `broken`：该插件的任一 router / hook 在启动时加载失败，或**根本没被尝试过**
      （后者说明 manifest 与 `server.py` 的注册表不一致 —— 声称挂着却没挂）。
    - `ok`：全部 router 与 hook 都成功。
    """
    off = disabled if disabled is not None else disabled_ids()

    results = {(r.module, r.purpose): r for r in plugin_loader.results()}
    skip = skipped()
    reasons: list[str] = []
    for mod in plugin.routers:
        res = results.get((mod, plugin_loader.ROUTER_PURPOSE))
        if res is None:
            # 因为被关掉而**刻意没加载**的不算问题（否则关一个就报一条「未注册」，
            # 把「你关的」和「坏掉的」混为一谈 —— 与三态分开是同一个道理）。
            if (mod, plugin_loader.ROUTER_PURPOSE) not in skip:
                reasons.append(f"{mod}: 未注册（manifest 与 server.py 的注册表不一致）")
        elif not res.ok:
            reasons.append(f"{mod}: {res.reason}")
    for hook in plugin.hooks:
        # 钩子缺失**不报**：`when="main"` 的钩子只在 `python server.py` 时调用，
        # 而 `state_of()` 在任意 import 场景下都会跑 —— 报「未调用」会把
        # 「这个阶段还没到」当成「坏了」。钩子的失败仍然报（那是真失败）。
        res = results.get((hook["module"], hook["attr"]))
        if res is not None and not res.ok:
            reasons.append(f"{hook['module']}.{hook['attr']}: {res.reason}")
    _ = skip  # （skip 只用于 router，见上）

    # 被关掉的插件**仍然把原因算出来**（理由见上），只是不计入 broken ——
    # 这样设置页可以说「你关的，而且它其实也加载失败了」，而不是把信息丢掉。
    if plugin.id in off:
        return STATE_DISABLED, reasons
    return (STATE_BROKEN, reasons) if reasons else (STATE_OK, [])


def probe_health(plugin: Plugin) -> dict[str, Any] | None:
    """跑一次清单里声明的健康探针，返回**原始快照**；没声明就返回 `None`。

    ★ **不猜 `ok`**：两个探针返回的东西形状完全不同（`warmup.status()` 是
    `{running, done, steps}`，`wechat_uia.status()` 是 `{enabled, ready, reason}`），
    硬凑一个 `ok` 字段等于替用户下判断 —— 而判断错了比不给更糟（界面上一片绿，
    实际微信根本没挂上）。所以只回答三件事：**跑没跑起来**、**失败原因**、
    **原始快照**，判断留给消费方。

    ★ **绝不能拖垮调用它的端点**：`/api/plugins` 存在的理由就是「在最坏的时候还能答」
    （见 system_api 的注释）。探针 import 失败（比如没装 comtypes）、抛异常、
    返回的不是字典 —— 一律降级成 `ran=False` 加一条原因，而不是让整个目录 500。
    """
    spec = plugin.health
    if not spec:
        return None
    try:
        import importlib  # 惰性导入：本模块被 tools/ 也在用，不该替它付 import 成本

        mod = importlib.import_module(spec["module"])
        fn = getattr(mod, spec["attr"])
        raw = fn()
        if not isinstance(raw, dict):
            return {"ran": False, "error": f"探针返回了 {type(raw).__name__}，不是字典", "data": None}
        return {"ran": True, "error": None, "data": raw}
    except Exception as exc:  # noqa: BLE001 —— 见 docstring：这里必须兜住一切
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}", "data": None}


def catalog(*, include_disabled: bool = True) -> dict[str, Any]:
    """给 `GET /api/plugins` 用的只读目录。

    **不做任何 import**：这里只读 JSON + 查 `plugin_loader` 的 registry，
    所以它不会像 `/api/health` 那样（那里有模块级 `import torch`）在最需要
    这份信息的时候自己先倒掉。
    """
    plugins = load_all()
    off = disabled_ids()
    on = enabled_ids(off)
    out: list[dict[str, Any]] = []
    counts = {STATE_OK: 0, STATE_BROKEN: 0, STATE_DISABLED: 0}
    for p in plugins:
        state, reasons = state_of(p, off)
        counts[state] += 1
        if state == STATE_DISABLED and not include_disabled:
            continue
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "kind": p.kind,
                "category": p.category,
                "order": p.order,
                "summary": p.summary,
                "core": p.is_core,
                "state": state,
                "reasons": reasons,
                # 用户关了（`disabled`）但被别人依赖 → `enabled` 仍为 True。
                # 前端开关必须读这个字段，不能拿 `state != "disabled"` 推 ——
                # 那会让「被依赖而保留」的能力显示成关着的。
                "enabled": p.id in on,
                "blockedBy": dependents_of(p.id, off),
                "requires": list(p.requires),
                "routers": list(p.routers),
                "routes": [dict(r) for r in p.routes],
                "legacyRoutes": [dict(r) for r in p.legacy_routes],
                "extras": p.extras,
                # `health` 是**声明**（要跑哪个函数），`healthProbe` 是**这次跑的结果**。
                # 两个字段都在：只有声明，消费方无法确认探针真的跑过；只有结果，
                # 出问题时会不知道该去看哪个函数。
                "health": p.health,
                # 关掉的能力不跑探针 —— 它连模块都没加载，问它健康没有意义。
                "healthProbe": probe_health(p) if p.id in on else None,
                "disableNote": p.disable_note,
            }
        )
    return {
        "ok": counts[STATE_BROKEN] == 0,
        "counts": {
            "total": len(plugins),
            "ok": counts[STATE_OK],
            "broken": counts[STATE_BROKEN],
            "disabled": counts[STATE_DISABLED],
        },
        # 开关是「重启生效」的：router 已经在启动时挂好了，改状态文件不会
        # 立刻卸载路由。前端必须把这话说出来，否则用户以为开关坏了。
        "restartRequired": True,
        "presets": [
            {
                "id": name,
                "label": PRESET_LABELS.get(name, name),
                "plugins": sorted(preset_ids(name)),
            }
            for name in PRESETS
        ],
        "preset": _current_preset(off, plugins),
        "plugins": out,
    }


def _current_preset(off: set[str], plugins: list[Plugin]) -> str:
    """当前禁用集对应哪个预设；对不上就是 `"custom"`（用户逐项改过）。

    注意比的是「用户写的禁用集」而不是 `enabled_ids()` 的结果 —— 后者会把
    「被依赖而保留」的补回来，与任何预设都难以吻合。
    """
    for name in PRESETS:
        if {p.id for p in plugins if p.id not in preset_ids(name)} == off:
            return name
    return "custom"
