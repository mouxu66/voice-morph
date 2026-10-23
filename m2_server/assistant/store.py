"""提案队列：内存 + 落盘的薄存储。

为什么落盘
----------
桌宠随时会被关掉，而"待确认的点歌"是**跨重启仍该在**的东西 —— 用户看到一半去
开会，回来桌宠重启了，提案不能凭空消失（那正是"AI 擅自决定"的观感）。

为什么还要有上限
----------------
这是待办队列，不是历史记录。历史在 `outputs/assistant/history.jsonl`（执行器写完
就追加一行），这里的 JSON 只留最近 `MAX_KEEP` 条，且**终态且超过一小时的**会被清掉。
不设上限的队列迟早变成几万行 JSON，每次启动都要解析。

为什么写盘要原子
----------------
`Path.write_text` 先截断再写。两个线程同时写、或写到一半进程被杀，就会留下
半截 JSON —— 下次启动解析失败，用户的所有待办全没了。这里用 tmp + os.replace。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from dataclasses import fields
from pathlib import Path

try:  # 允许以脚本方式直接跑（tools/ 下的一次性脚本常常这么 import）
    import config as cfg
except ImportError:  # pragma: no cover
    import sys as _sys
    from pathlib import Path as _P

    _sys.path.insert(0, str(_P(__file__).resolve().parents[1]))
    import config as cfg

from .schema import (
    STATUS_CANCELLED,
    STATUS_PENDING,
    Proposal,
)

#: 队列里最多留多少条。超了从最老的终态开始丢。
MAX_KEEP = 200
#: 终态提案的保留秒数。过期的直接清（不影响 history.jsonl）。
TERMINAL_TTL_S = 3600

_LOCK = threading.RLock()
_CACHE: dict[str, Proposal] | None = None
#: dataclass 认识的字段名。落盘数据里多的键（老版本 / 误用 to_dict）在读取时剔掉。
_FIELDS = {f.name for f in fields(Proposal)}


def _path() -> Path:
    d = Path(cfg.OUTPUTS_DIR) / "assistant"
    d.mkdir(parents=True, exist_ok=True)
    return d / "proposals.json"


def _history_path() -> Path:
    return Path(cfg.OUTPUTS_DIR) / "assistant" / "history.jsonl"


def _load() -> dict[str, Proposal]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    _CACHE = {}
    p = _path()
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 坏文件不该让整个后端起不来；留个 .bad 供事后看，然后从空队列开始。
            with contextlib.suppress(OSError):
                p.replace(p.with_suffix(".bad"))
            raw = []
        for item in raw if isinstance(raw, list) else []:
            try:
                # 只取 dataclass 认识的字段：老版本落盘的数据（或误写成 to_dict 的）
                # 会多出 label/sends，直接 Proposal(**item) 会抛 TypeError 整条丢掉。
                rec = {k: v for k, v in item.items() if k in _FIELDS}
                pr = Proposal(**rec)
            except (TypeError, AttributeError):
                continue  # 单条脏数据不该连累其余提案
            _CACHE[pr.id] = pr
    return _CACHE


def _flush(items: dict[str, Proposal]) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    # 用 to_record() 而不是 to_dict()：后者含 label/sends 派生字段，读回来时
    # Proposal(**rec) 会因为未知关键字整条跳过（用户看到"待办全没了"）。
    payload = [pr.to_record() for pr in items.values()]
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def _gc(items: dict[str, Proposal]) -> dict[str, Proposal]:
    """清掉过期的终态，再按创建时间保留最近 MAX_KEEP 条。

    ★ **原地删除**，不要返回新字典：返回值一旦没被赋回 `_CACHE`，内存里会留着
    已经被"清掉"的条目，于是测试和真实运行都会看到"删了还在"（缓存与盘不一致）。
    """
    now = time.time()
    for k in [
        k for k, v in items.items() if v.terminal and now - v.updated > TERMINAL_TTL_S
    ]:
        del items[k]
    if len(items) > MAX_KEEP:
        order = sorted(items.values(), key=lambda x: x.created)
        for stale in order[: len(items) - MAX_KEEP]:
            items.pop(stale.id, None)
    return items


def add(prop: Proposal) -> Proposal:
    with _LOCK:
        items = _load()
        items[prop.id] = prop
        _flush(_gc(items))
        return prop


def list_all(include_terminal: bool = True) -> list[Proposal]:
    with _LOCK:
        items = list(_load().values())
    if not include_terminal:
        items = [p for p in items if not p.terminal]
    items.sort(key=lambda p: p.created, reverse=True)
    return items


def get(pid: str) -> Proposal | None:
    with _LOCK:
        return _load().get(pid)


def update(prop: Proposal) -> Proposal:
    with _LOCK:
        items = _load()
        items[prop.id] = prop
        _flush(_gc(items))
        return prop


def drop(pid: str) -> bool:
    """取消一条。只对 pending 生效 —— running 的不许撤销（发到一半撤销会更乱）。"""
    with _LOCK:
        items = _load()
        pr = items.get(pid)
        if pr is None or pr.status != STATUS_PENDING:
            return False
        pr.touch(STATUS_CANCELLED)
        _flush(_gc(items))
        return True


def append_history(prop: Proposal) -> None:
    """执行完成追加一行。历史**只增不改**，用于事后回答"我什么时候发过这条"。"""
    rec = prop.to_dict()
    rec["finished"] = time.time()
    with _LOCK, _history_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def reset_cache_for_tests() -> None:
    """测试用：丢掉进程内缓存，强制下次从盘上读（改了 OUTPUTS_DIR 之后必须调）。"""
    global _CACHE
    with _LOCK:
        _CACHE = None
