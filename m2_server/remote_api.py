"""手机遥控的 HTTP 接口（`hook.remote` 这个编排层自己的 router）。

    GET  /api/remote/capabilities   遥控页三个标签各自需要哪些能力、现在能不能用

为什么这个插件自己有一个 router
--------------------------------
遥控页（`web/public/remote.html`）的**功能**全部由别的插件提供：

    标签「手机当麦」    /api/capture/mic/upload   → pet.companion
    标签「实时变声」    /api/rvc/live/*           → sound.rvc-live
    标签「打字发语音」  /api/wechat/*             → hook.wechat

它一个router 都不拥有。所以 `plugin.json` 里 `routers: ["remote_api"]`
指向的**不是遥控的功能，而是遥控的「现状自述」** —— 也就是上面那张依赖表
反过来问一次「现在这几项各自是什么状态」。这正是 `routers 不能为空`
那条校验（`plugin_manifest.py`）当初判断的情况：它假设每个插件都Own一点
HTTP 表面，而编排层插件 Own 的是**知识**。这里没有为满足校验而造端点 ——
下面这个端点是手机端真正需要的（见下）。

为什么手机端需要这个端点
------------------------
`remote.html` 已经在读 `GET /api/plugins` 并自己判断 `capOff(id)`
（`web/public/remote.html` 的 `loadCaps`/`capOff`）。那为什么还要这一个端点？

因为**依赖关系只写在 `hook.remote/plugin.json` 里**，而遥控页读的是
`/api/plugins` 的扁平列表 —— 也就是说「手机当麦要 pet.companion、
实时变声要 rvc-live」这条知识在页面里被**手抄了一遍**
（三处 `capOff(...)` 字面量）。清单改一次而页面忘了改，
手机端就会对着已关掉的能力显示可点按钮，且没有任何测试会红
（`remotePage.test.ts` 钉的是「页面调的端点存在」，不是「依赖声明一致」）。

所以把这份依赖表搬到这里，由后端**从自己的 manifest 现算**再吐给页面：
    页面问「我要的 tab 现在能不能用」 →  后端答「能/不能 + 缺哪个 + 为什么」
页面从此不再硬编码插件 id。清单加一条 tab 依赖，这里自动跟着变。

这与 `test_docs_freshness.py` / §7 的 `ci.yml ↔ check.py` 守卫是同一个手法：
**任何只在一处声明的东西都不算接上了**，两处靠人的记性保持一致的那不叫声明。
"""

import plugin_manifest
from fastapi import APIRouter
from runtime import API_PREFIX

router = APIRouter(prefix=API_PREFIX)

#: 遥控页的标签 → 它依赖的能力。
#:
#: **每个标签的依赖集合 = 该标签用到��全部端点所属的能力，一个都不能少** ——
#: 少一个就会出现"关掉那个能力后页面仍显示可点按钮，按下去才404"。
#: （`core.voices` / `core.media` 也在列：页面要读音色列表、要播放录音回放。
#: 它们是核心插件所以不会缺，但"不会缺"是当前的事实，不是结构保证。）
#: 与 `hook.remote/plugin.json` 的 `requires` 的关系：**`requires` 是它的子集**
#: （只列可选能力 —— 核心插件本来就是开的，列进去会让 `expand()` 的语义变味）。
#: 两条断言分别钉住这两个方向，见 `test_remote_api.py`。
TABS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # (tab id, 页面上的名字, 依赖的能力 id)
    ("mic", "手机当麦", ("pet.companion", "sound.rvc-live", "core.voices", "core.media")),
    ("live", "实时变声", ("sound.rvc-live", "core.voices")),
    ("say", "打字发语音", ("hook.wechat", "core.voices", "core.media")),
)


@router.get("/remote/capabilities")
def remote_capabilities() -> dict:
    """遥控页三个标签各自的能力现状：能用什么、缺什么、缺的原因。

    供`remote.html` 在渲染前问一次，取代页���里手抄的 `capOff('...')` 字面量。

    **为什么不能 import `plugin_loader`**：那是重依赖入口（torch 等），
    而这个端点恰恰在「最需要知道自己缺什么」的时候被调 —— 与
    `plugin_manifest.catalog()` 同样的取舍：只读 JSON + 状态文件，不 import 任何东西。
    """
    plugins = plugin_manifest.by_id()
    off = plugin_manifest.disabled_ids()
    on = plugin_manifest.enabled_ids(off)

    tabs: list[dict] = []
    for tab_id, label, needs in TABS:
        items: list[dict] = []
        missing: list[str] = []
        for pid in needs:
            p = plugins.get(pid)
            if p is None:
                # 清单里没有这个 id = 有人改了 requires 却没改 TABS（或反过来）。
                # 不抛：这一页是手机上的辅助页，为它 500 比"少一个能力"更糟。
                items.append(
                    {"id": pid, "name": pid, "state": "unknown", "usable": False, "reasons": ["清单里没有这个能力"]}
                )
                missing.append(pid)
                continue
            state, reasons = plugin_manifest.state_of(p, off)
            # 用户关了但被别的启用能力依赖 → `enabled` 仍为 True（见 enabled_ids 的注释）：
            # 这里必须读 `enabled` 而不是 `state != disabled`，否则「被依赖而保留」
            # 的能力会被误报成不可用。
            usable = pid in on and state == plugin_manifest.STATE_OK
            items.append(
                {
                    "id": pid,
                    "name": p.name,
                    "state": state,
                    "enabled": pid in on,
                    "usable": usable,
                    "reasons": reasons,
                }
            )
            if not usable:
                missing.append(p.name)
        tabs.append(
            {
                "id": tab_id,
                "label": label,
                "usable": not missing,
                "missing": missing,
                "capabilities": items,
            }
        )

    return {
        "ok": all(t["usable"] for t in tabs),
        "tabs": tabs,
        # 编排层自己也要能被问「你被关了吗」—— 这一层关掉时它的 router 根本不挂，
        # 调用方拿到的是 404 而不是这份 JSON，所以这里显式带上，让调用方能区分
        # 「插件关了」与「插件在但依赖缺了」。
        "plugin": {"id": "hook.remote", "enabled": "hook.remote" in on},
    }
