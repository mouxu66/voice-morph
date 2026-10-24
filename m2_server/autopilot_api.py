"""「全局托管」的 HTTP 面：授权、急停、配置、会话挑选、发送测试。

为什么整个 router 挂 loopback
-----------------------------
这是**会替用户给别人发消息**的能力。局域网里的任何人都不能点开它 —— 做法与
`assistant_api` / `ai_api` 一致：router 级 `Depends(_require_loopback)`，
不是每个端点手写一遍（漏写一个就是后门）。

为什么要 `consent=true` 才能 start
----------------------------------
前端有一张风险告知；但 API 才是真正的闸门 —— 任何直接 curl 都要显式声明
「我知道它在替我发消息」。少了这个字段一律 403。

为什么配置只能在停止时改
------------------------
正在跑的引擎每轮都从 `_cfg` 快照读配置；跑到一半改白名单会出现
「A 会话用旧规则、B 会话用新规则」的混合状态，事后根本查不清哪条为什么发。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import chat_autopilot

router = APIRouter(prefix="/api/autopilot", tags=["autopilot"])


def _require_loopback(request: Request) -> None:
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="这个接口只允许本机访问")


router.dependencies.append(Depends(_require_loopback))


class StartReq(BaseModel):
    consent: bool = False


class TestSendReq(BaseModel):
    text: str


def _advisor_status() -> dict:
    try:
        import reply_advisor

        return reply_advisor.status()
    except Exception as exc:  # noqa: BLE001 —— 状态页不该因为模型没装而 500
        return {"ok": False, "reason": f"本地模型状态不可用：{exc}"}


def _wechat_status() -> dict:
    try:
        import wechat_uia

        return wechat_uia.status()
    except Exception as exc:  # noqa: BLE001
        return {"ready": False, "reason": f"微信状态不可用：{exc}"}


@router.get("/state")
def state():
    """托管状态 + 当前配置 + 最近日志 + 两个依赖（微信 UIA / 本地模型）的健康。"""
    eng = chat_autopilot.engine()
    return {
        "ok": True,
        "server_time": round(time.time(), 3),
        **eng.status(),
        "config": eng.config(),
        "recent": eng.recent(40),
        "wechat": _wechat_status(),
        "advisor": _advisor_status(),
        "limits": {
            "max_per_chat_per_hour": eng.config()["max_per_chat_per_hour"],
            "max_per_run": eng.config()["max_per_run"],
            "min_gap_s": eng.config()["min_gap_s"],
        },
    }


@router.get("/sessions")
def sessions(limit: int = 20):
    """实时会话列表（含未读/免打扰/置顶），供用户挑白名单。"""
    try:
        import wechat_text

        out = wechat_text.scan_sessions(limit=limit)
    except Exception as exc:  # noqa: BLE001 —— 微信没开是正常状态
        return {"ok": False, "sessions": [], "error": f"读会话列表失败：{exc}"}
    cfg_in = chat_autopilot.engine().config()
    for s in out.get("sessions") or []:
        s["whitelisted"] = s.get("chat") in cfg_in["whitelist"]
        s["group"] = s.get("chat") in cfg_in["groups"]
    return out


@router.put("/config")
def put_config(body: dict):
    eng = chat_autopilot.engine()
    if eng.status()["status"] in ("running", "paused", "stopping"):
        raise HTTPException(status_code=409, detail="托管运行中不能改配置：先暂停或停止")
    try:
        data = chat_autopilot.save_config(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 引擎持有的是启动时的快照；停止状态下它和磁盘应该立刻一致
    eng.set_config(data)
    return {"ok": True, "config": data}


@router.post("/start")
def start(req: StartReq):
    eng = chat_autopilot.engine()
    try:
        return {"ok": True, **eng.start(req.consent)}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pause")
def pause():
    return {"ok": True, **chat_autopilot.engine().pause()}


@router.post("/resume")
def resume():
    return {"ok": True, **chat_autopilot.engine().resume()}


@router.post("/stop")
def stop():
    return {"ok": True, **chat_autopilot.engine().stop()}


@router.post("/test-send")
def test_send(req: TestSendReq):
    """只在「文件传输助手」发一条，验证整条发送链路（不进白名单、不算频控）。"""
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="测试内容不能为空")
    if len(text) > 40:
        raise HTTPException(status_code=400, detail="测试内容最多 40 字")
    import wechat_text

    original = ""
    try:
        original = wechat_text.selected_chat()
    except Exception:
        original = ""
    sel = wechat_text.select_session("文件传输助手")
    if not sel.get("ok"):
        raise HTTPException(status_code=409, detail=f"切不到「文件传输助手」：{sel.get('error')}")
    try:
        res = wechat_text.send_text(text, expect_chat="文件传输助手")
    finally:
        if original and original != "文件传输助手":
            try:
                wechat_text.select_session(original)
            except Exception:
                pass
    chat_autopilot.engine().log(
        "test" if res.get("ok") else "error",
        "文件传输助手",
        reply=text,
        method=res.get("method", ""),
        reason=res.get("error", ""),
    )
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error") or "发送失败")
    return {"ok": True, **res}
