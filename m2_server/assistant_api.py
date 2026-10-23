"""会话助理的 HTTP 面（`assistant` 包的薄壳）。

为什么只是一层壳
----------------
解析（`assistant.extract`）、搜索（`assistant.search`）、执行（`assistant.executor`）
全部在包里，且**不依赖 FastAPI** —— 这样 CLI 与测试都能直接调，不必起服务。
本模块只做三件事：把请求体转成提案、把提案状态序列化、把执行丢给 executor。

护栏（三条，改之前先想清楚）
--------------------------
1. `/observe` **绝不执行**。它只产出提案。想让 AI"看到就发"，请先删掉这个模块的一半。
2. `/confirm` 是唯一的执行入口，且要求提案处于 `pending`。重复点会 409 而不是再发一遍。
3. 整个 router 挂 `_require_loopback` —— 这是本机桌面能力，不该被局域网里任何人调用。
   做法与 `ai_api` 一致（router 级依赖，不是每个端点手写）。
"""

from __future__ import annotations

import time

from assistant import extract, search, store
from assistant.executor import ExecError, start
from assistant.schema import KINDS, Proposal, validate_params
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

#: 同一条提案在这个窗口内不重复生成（避免桌宠每 1.5s 轮询读屏就堆一条）。
DEDUPE_WINDOW_S = 300


def _require_loopback(request: Request) -> None:
    host = (request.client.host if request.client else "") or ""
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="这个接口只允许本机访问")


router.dependencies.append(Depends(_require_loopback))


class ObserveReq(BaseModel):
    text: str
    source: str = "chat"
    #: 只解析不落库（前端想先预览一下"AI 会怎么理解这句话"时用）
    dry_run: bool = False


class ManualReq(BaseModel):
    kind: str
    params: dict
    title: str = ""


class SearchReq(BaseModel):
    query: str
    limit: int = 5


def _dedupe(kind: str, params: dict, source: str) -> Proposal | None:
    """已有同 kind + 同关键参数的 pending 提案就复用，不再加一条。"""
    key = params.get("query") or params.get("text") or ""
    now = time.time()
    for p in store.list_all(include_terminal=False):
        if p.kind != kind or p.source != source:
            continue
        if now - p.created > DEDUPE_WINDOW_S:
            continue
        other = p.params.get("query") or p.params.get("text") or ""
        if other == key:
            return p
    return None


def _proposal_from_hit(hit, source: str) -> Proposal | None:
    clean, missing = validate_params(hit.kind, hit.params)
    if missing:
        # 抽不全就不生成可执行提案。如实说缺什么，不猜。
        return None
    spec = KINDS[hit.kind]
    if hit.kind == "song.send":
        title = f"发《{clean['query']}》"
        detail = f"看到「{hit.matched}」，要不要搜一首发过去？"
    elif hit.kind == "voice.say":
        who = clean.get("voice") or clean.get("dialect") or "当前音色"
        title = f"用{who}说一句"
        detail = f"看到「{hit.matched}」"
    else:
        title = spec.label
        detail = f"看到「{hit.matched}」"
    return Proposal(
        kind=hit.kind,
        title=title,
        detail=detail,
        params=clean,
        evidence={"matched": hit.matched, "from": source, "confidence": hit.confidence},
        source=source,
    )


@router.post("/observe")
def observe(req: ObserveReq):
    """喂一段聊天文本，返回**候选提案**（不执行任何动作）。"""
    text = (req.text or "").strip()
    if not text:
        return {"ok": True, "proposals": [], "note": "没有内容"}
    hits = extract.parse(text)
    made: list[dict] = []
    for hit in hits:
        prop = _proposal_from_hit(hit, req.source)
        if prop is None:
            continue
        if not req.dry_run:
            prop = _dedupe(prop.kind, prop.params, prop.source) or store.add(prop)
        made.append(prop.to_dict())
    note = ""
    if not hits:
        note = (
            "这句话看着像请求，但没抓到明确的歌名/内容"
            if extract.looks_like_request(text)
            else "没看出要做什么"
        )
    return {"ok": True, "proposals": made, "note": note, "kinds": sorted(KINDS)}


@router.post("/proposal")
def create_manual(req: ManualReq):
    """手动下一条指令（用户自己说"发首晴天"，不经读屏）。"""
    if req.kind not in KINDS:
        raise HTTPException(404, f"没有这个动作：{req.kind}")
    clean, missing = validate_params(req.kind, req.params)
    if missing:
        raise HTTPException(400, f"缺参数：{', '.join(missing)}")
    prop = Proposal(
        kind=req.kind,
        title=req.title or f"{KINDS[req.kind].label}：{clean.get('query') or clean.get('text', '')[:20]}",
        detail="手动创建",
        params=clean,
        source="manual",
    )
    store.add(prop)
    return {"ok": True, "proposal": prop.to_dict()}


@router.get("/proposals")
def list_proposals(include_terminal: bool = True):
    items = [p.to_dict() for p in store.list_all(include_terminal=include_terminal)]
    return {"ok": True, "proposals": items, "count": len(items)}


@router.get("/proposals/{pid}")
def get_proposal(pid: str):
    p = store.get(pid)
    if p is None:
        raise HTTPException(404, "没有这条提案")
    return {"ok": True, "proposal": p.to_dict()}


@router.post("/proposals/{pid}/confirm")
def confirm(pid: str):
    """执行。**唯一**会碰 TTS / RVC / 微信的入口。"""
    p = store.get(pid)
    if p is None:
        raise HTTPException(404, "没有这条提案")
    ok, why = start(p)
    if not ok:
        raise HTTPException(409, why)
    return {"ok": True, "status": "running", "proposal": p.to_dict()}


@router.post("/proposals/{pid}/cancel")
def cancel(pid: str):
    if not store.drop(pid):
        raise HTTPException(409, "这条不在待确认状态，取消不了")
    return {"ok": True, "status": "cancelled"}


@router.post("/search")
def search_songs(req: SearchReq):
    """手搜（"换一首"）：只返回候选，不下载、不发送。"""
    try:
        rows = search.search_song(req.query, limit=req.limit)
    except search.SearchError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "query": req.query, "results": rows}


@router.get("/capabilities")
def capabilities():
    """这个助理现在能做什么、哪些做不到 —— 前端据此隐藏按钮，而不是点了才报错。"""
    return {
        "ok": True,
        "kinds": {k: {"label": v.label, "summary": v.summary, "sends": v.sends} for k, v in KINDS.items()},
        "notes": {
            "dialect": "TTS 只有普通话/英文，方言发音做不了（会如实拒，不会假装）",
            "song_source": "只搜 B 站，因为下载白名单只放行国内平台",
            "confirm": "所有发送动作都必须人工确认，没有自动模式",
        },
        "errors": [ExecError.__name__],
    }
