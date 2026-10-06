"""AI 写动作执行器（第二期）—— 三重门 + 审计，默认全关。

归属`hook.ai`（**可选插件**）：关掉它= 路由不挂载 = 404。
为什么写侧不能并入 `core.system`（设计稿 §2.2）：写能力的安全等级高于读，
不给它核心的零门槛 —— 不可逆动作要有三重钥匙。

三重门（**fail closed**，任一不满足即拒，顺序即成本从低到高）：

1. **插件启用**：路由没挂载就404，这一条是结构性的；
2. **`VM_AI_CONTROL=1`**：总开关。同 `VM_WARMUP` / `VM_PLUGIN_SEED` 的既有手法，
   检查放**函数内部** —— 避免 import 期副作用（§8.41 教训）；
3. **`X-API-Key` 匹配 `VM_AI_TOKEN`**：**即使回环也要**；未设 token = 写能力不存在。
   刻意**不复用** `VM_API_TOKEN`：为手机开遥控顺手就开了「AI 写动作」的门 = 权限扩张。

另加一条硬规则：**非回环来源的 POST 一律拒**，即使 token 正确
（只读面已限定回环，写面更不能开 LAN —— 设计稿 §3.2）。
"""

from __future__ import annotations

import json
import os

from fastapi import APIRouter, HTTPException, Request

import ai_actions
import ai_audit
from runtime import API_PREFIX

router = APIRouter(prefix=API_PREFIX)

CONTROL_ENV = "VM_AI_CONTROL"
TOKEN_ENV = "VM_AI_TOKEN"
ACTOR_HEADER = "x-ai-actor"


def _loopback_only(request: Request) -> None:
    """写面比读面更严：非回环来源**永远**拒（含 LAN），不提供放行开关。

    读面已经因为「含本机路径与日志尾部」把LAN 整个关掉了；写面能改用户设置，
    放开 LAN 等于把机器交给同网段任何人。
    """
    host = getattr(getattr(request, "client", None), "host", "") or ""
    allowed = {"127.0.0.1", "::1", "localhost", "testclient"}
    if host in allowed:
        return
    if host.startswith("::ffff:") and host[7:] in allowed:
        return
    raise HTTPException(
        status_code=403,
        detail=f"AI 写动作仅限本机调用（来源 {host or '未知'}）—— 局域网永不开放",
    )


def _require_control_gate() -> None:
    """第2 门：`VM_AI_CONTROL=1`。函数内检查（import 期不能读环境变量）。"""
    if os.environ.get(CONTROL_ENV, "") != "1":
        raise HTTPException(
            status_code=403,
            detail=f"写动作未启用：需要环境变量 {CONTROL_ENV}=1（总开关，默认关）",
        )


def _require_token(request: Request) -> None:
    """第 3 门：token 必须匹配 `VM_AI_TOKEN`。**未设 token = 写能力不存在**。

    刻意不回退到 `VM_API_TOKEN`：两个 token 职责分离，任何一个的权限扩张
    都不许带出另一个（设计稿 §3.2）。
    """
    expected = os.environ.get(TOKEN_ENV, "")
    if not expected:
        raise HTTPException(
            status_code=403,
            detail=f"写动作不可用：未设 {TOKEN_ENV}（未设 token 即视为无写能力，不回退到 VM_API_TOKEN）",
        )
    given = request.headers.get("x-api-key", "")
    if not given:
        given = request.query_params.get("api_key", "")
    if given != expected:
        raise HTTPException(status_code=403, detail=f"{TOKEN_ENV} 不匹配")


def _actor(request: Request) -> str:
    raw = request.headers.get(ACTOR_HEADER, "") or "unknown"
    #审计字段会被展示，别让它无限长或带控制字符
    return "".join(ch for ch in raw if ch.isprintable())[:40] or "unknown"


@router.post("/ai/actions/{action_id}")
async def post_action(action_id: str, request: Request):
    """执行一个白名单动作。三重门任一不满足即拒。

    `dry_run=true` 强制优先：只算「会发生什么」，**零副作用**，
    但同样写一条 dry-run 审计 —— 否则试算就没人看得见了（设计稿 §五）。
    """
    _loopback_only(request)
    _require_control_gate()
    _require_token(request)

    # 强制 JSON body（设计稿 §3.2 硬化项）：顺带触发 CORS 预检，
    # 公网来源的页面过不了 Origin 守卫。
    body = await _read_json_body(request)
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    dry_run = bool(body.get("dry_run"))

    actor = _actor(request)
    #★ fail-closed 的正确顺序：**先落 intent 账本，再执行**。
    # 反过来（执行完再写）的话，审计写失败时动作已经发生 —— 那不是
    # 「无账本的写动作」，是「有副作用但查不到」的写动作，比不做更糟。
    try:
        intent = ai_audit.record(
            action_id,
            params,
            dry_run=dry_run,
            ok=False,  # 还没成功；成功与否由 result 决定
            code=0,
            actor=actor,
            message="(intent) 开始执行",
        )
    except ai_audit.AuditUnavailable as exc:
        # 审计写不进去 → 动作**不许发生**
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        if dry_run:
            result = ai_actions.dry_run(action_id, params)
        else:
            result = ai_actions.run(action_id, params)
    except ai_actions.ActionError as exc:
        # 失败的尝试已由上面那条 intent 记录；这里把拒绝原因补进账本
        _append_outcome(intent["id"], action_id, params, dry_run, False, exc.code, actor, exc.message)
        raise HTTPException(status_code=exc.code, detail=exc.message) from exc

    _append_outcome(
        intent["id"], action_id, params, dry_run, True, 200, actor,
        result.get("message", ""), result.get("evidence"), result.get("duration_ms", 0),
    )
    result["audit_id"] = intent["id"]
    result["actor"] = actor
    return result


def _append_outcome(
    audit_id: str,
    action_id: str,
    params: dict,
    dry_run: bool,
    ok: bool,
    code: int,
    actor: str,
    message: str,
    evidence: object = None,
    duration_ms: int = 0,
) -> None:
    """补记结果（append-only，同一条 id 的第二条 = 结果行）。

    结果行落盘失败**不再抛**：动作已经发生了，此时报错会让调用方以为
    动作没执行而重试 —— 那才是真事故。这里如实记不上就放行，
    intent 行至少还在（它证明「谁在什么时候试了什么」）。
    """
    try:
        ai_audit.record(
            action_id,
            params,
            dry_run=dry_run,
            ok=ok,
            code=code,
            actor=actor,
            evidence=evidence,
            duration_ms=duration_ms,
            message=message,
        )
    except ai_audit.AuditUnavailable:
        pass


async def _read_json_body(request: Request) -> dict:
    """读 JSON body（空 body 视作 {}）。

    `await request.body()` 是协程 —— 端点必须是 `async def` 才会正确等待。
    写成同步函数会在运行时炸出「coroutine was never awaited」的 500，
    而这个 500 会伪装成「动作表有问题」，把排障方向带偏（实测踩过）。
    """
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"body 必须是 JSON 对象：{exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="body 必须是 JSON 对象")
    return data