"""AI 白名单动作表（**无路由**）—— 读侧目录与写侧执行器共用的唯一真相源。

为什么单独一个模块：`ai_api`（只读面）归 `core.system`（核心，不可关），
写动作归可选插件 `hook.ai`。**core侧绝不能 import 已关闭插件的模块**
（插件化的硬要求），所以动作表不能放进写侧执行器 `ai_actions_api.py` ——
否则 `hook.ai` 一关，只读面连「有哪些动作可用」都答不出来。

三条设计规则（改动前先读 `docs/交接-AI必读.md` §五）：

1. **声明式**：动作表里没有的 `action_id` 一律 404（默认拒绝）。
   不靠「记得不要实现它」——那种约定迟早被人违反。
2. **不造第二套功能 API**：每个动作都转调既有函数
   （`plugin_manifest.set_enabled` / `apply_preset`），不重写一遍逻辑。
   本项目已吃过「死参数」的亏（`docs/犯错档案-工程.md` §8.38）。
3. **零外部副作用优先**：第一期只放 `plugins.*` —— 改的是本进程内的
   插件开关列表，不碰系统设备、不碰微信、不删东西。`audio.*` 这类
   改系统默认设备的动作等真机需求出现再谈（设计稿 §九第3 条仍待拍板）。

`evidence` 是「下一步该看哪」的机器可读指针（如`{"plugins_disabled": [...]}`），
人话放`message` —— 反过来会让消费方去解析长文本（`docs/犯错档案-工程.md` §8.21）。
"""

from __future__ import annotations

import time
from typing import Any, Callable

import plugin_manifest

SCHEMA = 1

#: 动作目录的版本号，与 `ai_api.SCHEMA` 同源但独立演进 ——
#: 动作表加了东西读侧要能看出来，不能悄悄变。
ACTIONS_SCHEMA = 1


class ActionError(Exception):
    """动作被拒。`code` 直接映射 HTTP 状态码（设计稿 §3.3）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _as_bool(value: Any, field: str) -> bool:
    """严格布尔解析：只认真正的 bool。

    为什么不容错：`enabled="false"` 这种字符串在 Python 里是**真值**，
    不严格处理就成了「用户说关、实际开着」—— 而 UI 上显示已关闭，
    排障时这个不一致极难发现（同类教训见 `docs/犯错档案-工程.md` §8.40）。
    """
    if isinstance(value, bool):
        return value
    raise ActionError(400, f"{field} 必须是 true/false，收到 {type(value).__name__}")


def _require_str(params: dict, field: str) -> str:
    val = params.get(field)
    if not isinstance(val, str) or not val.strip():
        raise ActionError(400, f"缺参数 {field}（非空字符串）")
    return val.strip()


# ---------------------------------------------------------------- 动作实现


def _act_plugins_enable(params: dict) -> tuple[dict, str]:
    on = _as_bool(params.get("enabled"), "enabled")
    pid = _require_str(params, "id")
    try:
        # 既有函数的关闭守卫原文我们不加工：409 与 `blocked_by` 直接透传，
        # 这样 AI 看到的理由与用户在 UI 里看到的一字不差（单一真相源）。
        res = plugin_manifest.set_enabled(pid, on)
    except KeyError as exc:
        raise ActionError(404, f"没有这个能力：{pid}") from exc

    evidence: dict[str, Any] = {"plugins_enabled": res["enabled"]}
    if res.get("blocked_by"):
        raise ActionError(
            409,
            f"仍被 {', '.join(res['blocked_by'])} 依赖，不能关闭"
            f"（能力 {pid} 的关闭守卫）",
        )
    if res.get("reason") == "core":
        raise ActionError(409, f"{pid} 是核心能力，不可关闭")
    if on:
        msg = f"已启用 {pid}"
    else:
        msg = f"已关闭 {pid}"
        evidence["plugins_disabled"] = [pid]
    # 如实回报是否需要重启：插件开关是落盘 + 下次启动挂载的，
    # 说成「立即生效」会让用户白等一次重启（诚实性不变量）。
    msg += "（重启桌面端后生效）"
    return evidence, msg


def _act_plugins_preset(params: dict) -> tuple[dict, str]:
    name = _require_str(params, "name")
    # 默认只增不减（2026-10-15）；要「收窄到只剩该套餐」得显式 strict=true
    strict = bool(params.get("strict"))
    valid = sorted(getattr(plugin_manifest, "PRESETS", {}).keys())
    if valid and name not in valid:
        raise ActionError(400, f"没有这个预设：{name}；可选{sorted(valid)}")
    res = plugin_manifest.apply_preset(name, strict=strict)
    disabled = sorted(res.get("disabled") or [])
    kept = sorted(res.get("kept") or [])
    evidence = {"plugins_disabled": disabled, "mode": res.get("mode"), "kept": kept}
    if strict:
        return evidence, f"已严格套用预设「{name}」，关闭 {len(disabled)} 项（重启后生效）"
    return evidence, (
        f"已套用预设「{name}」：只补齐、未关闭任何能力"
        f"（保留你手动开启的 {len(kept)} 项，重启后生效）"
    )


#: 动作表。**这张表就是全部权限** —— 表外的东西一律不存在。
#: 新增动作必须同时给出：schema 声明 + 实现 + 前置条件 + 风险等级。
_ACTIONS: dict[str, dict[str, Any]] = {
    "plugins.enable": {
        "handler": _act_plugins_enable,
        "schema": {
            "id": "string（能力 id，如 sound.watermark）",
            "enabled": "boolean（true=启用，false=关闭）",
        },
        "risk": "low",
        "desc": "启用/关闭一个能力（核心不可关；被依赖时拒绝关闭）",
    },
    "plugins.preset": {
        "handler": _act_plugins_preset,
        "schema": {
            "name": "string（预设名，如 standard / wechat / class）",
            "strict": "boolean（可选，默认 false。true = 严格套用，会关闭不在套餐里的能力）",
        },
        "risk": "low",
        "desc": "套用场景预设：默认只增不减（不动你手动开启的能力），strict=true 才整套重设",
    },
}

#: 第二期只开放插件类动作。这份白名单会被 `GET /api/ai/actions` 如实回报，
#: 让消费方（AI/人）能看见「现在到底能做什么」，而不是靠猜。
ALLOWED_IDS = tuple(sorted(_ACTIONS))


def is_allowed(action_id: str) -> bool:
    """默认拒绝：表外一律不存在。"""
    return action_id in _ACTIONS


def catalog() -> list[dict[str, Any]]:
    """动作目录（**不含** handler —— 那是可执行对象，不许出后端）。"""
    out = []
    for aid in ALLOWED_IDS:
        spec = _ACTIONS[aid]
        out.append(
            {
                "id": aid,
                "desc": spec["desc"],
                "risk": spec["risk"],
                "schema": spec["schema"],
            }
        )
    return out


def run(action_id: str, params: dict) -> dict:
    """执行一个动作。**调用方必须先过完三重门**（`ai_actions_api` 的职责）。

    返回统一形状：`{ok, code, message, evidence, duration_ms}` ——
    设计稿 §5.1 定的形状，`evidence` 是机器可读指针。
    """
    if not is_allowed(action_id):
        raise ActionError(404, f"没有这个动作：{action_id}")
    t0 = time.perf_counter()
    spec = _ACTIONS[action_id]
    evidence, message = spec["handler"](params or {})
    return {
        "ok": True,
        "code": 200,
        "action": action_id,
        "message": message,
        "evidence": evidence,
        "duration_ms": int((time.perf_counter() - t0) * 1000),
    }


def dry_run(action_id: str, params: dict) -> dict:
    """只算「会发生什么」，**零副作用**。

    为什么不复用 `run` 再回滚：插件开关是**落盘**的，回滚要再写一次盘 ——
    中途失败就真改了用户设置。「试算」必须真的不写。
    """
    if not is_allowed(action_id):
        raise ActionError(404, f"没有这个动作：{action_id}")
    params = params or {}

    if action_id == "plugins.enable":
        pid = _require_str(params, "id")
        on = _as_bool(params.get("enabled"), "enabled")
        plugins = plugin_manifest.by_id()
        if pid not in plugins:
            raise ActionError(404, f"没有这个能力：{pid}")
        if not on and plugins[pid].is_core:
            raise ActionError(409, f"{pid} 是核心能力，不可关闭")
        blocked = (
            plugin_manifest.dependents_of(pid, plugin_manifest.disabled_ids())
            if not on
            else []
        )
        if blocked:
            raise ActionError(409, f"仍被 {', '.join(blocked)} 依赖，不能关闭")
        verb = "启用" if on else "关闭"
        return {
            "ok": True,
            "code": 200,
            "action": action_id,
            "dry_run": True,
            "message": f"将会{verb} {pid}（重启后生效）",
            "evidence": {"would_enable": on, "target": pid},
            "duration_ms": 0,
        }
    if action_id == "plugins.preset":
        name = _require_str(params, "name")
        strict = bool(params.get("strict"))
        valid = sorted(getattr(plugin_manifest, "PRESETS", {}).keys())
        if valid and name not in valid:
            raise ActionError(400, f"没有这个预设：{name}；可选 {sorted(valid)}")
        # `preset_ids()` 返回的是**启用集**（未知预设直接抛KeyError），
        # 关闭集 = 全部能力 - 启用集。别拿启用集当关闭集报 —— 会说反。
        would_enable = sorted(plugin_manifest.preset_ids(name))
        # ★ 默认只增不减：真正会被**新开**的只是「套餐点名、当前还没开」的那些；
        #   会关的一个也没有。dry_run 必须跟实际行为一致 —— 报成「关闭 N 项」
        #   会让用户以为要掉东西（与 `apply_preset` 的语义改动同步，2026-10-15）。
        already_on = plugin_manifest.enabled_ids()
        if strict:
            would_disable = sorted(set(plugin_manifest.by_id()) - set(would_enable))
            newly_on: list[str] = []
        else:
            would_disable = []
            newly_on = sorted(set(would_enable) - already_on)
        msg = (
            f"将会严格套用预设「{name}」：启用 {len(would_enable)} 项、"
            f"关闭 {len(would_disable)} 项（重启后生效）"
            if strict
            else f"将会套用预设「{name}」：新开 {len(newly_on)} 项、不关闭任何能力（重启后生效）"
        )
        return {
            "ok": True,
            "code": 200,
            "action": action_id,
            "dry_run": True,
            "message": msg,
            "evidence": {
                "would_enable": newly_on if not strict else would_enable,
                "would_disable": would_disable,
                "mode": "strict" if strict else "merge",
            },
            "duration_ms": 0,
        }
    raise ActionError(404, f"没有这个动作：{action_id}")


#: 供静态检查/测试断言用：动作表里的每个 handler 都必须返回 (dict, str)。
Handler = Callable[[dict], tuple[dict, str]]