"""AI 动作审计（append-only JSONL）—— 写动作的账本。

为什么与 `history.jsonl` 分开（设计稿 §6.2）：history 是**用户的作品**，
有 2000 条上限、会被用户清理；审计是**外部动作的记录**，不该被
「清作品库」顺带清掉，也不该混进作品列表。两者生命周期不同。

三条硬规则（改动前先读 `docs/AI调试端点设计.md` §六）：

1. **写不进审计 = 不许执行**（fail-closed）。审计是这个端点存在的唯一凭据：
   没有账本的写动作等于没有发生过。
2. **路径走 `cfg.OUTPUTS_DIR`**，不在导入期早绑定 —— 测试的
   `VM_OUTPUTS_DIR` 要能生效（`test_output_isolation.py` 是现成守卫）。
3. **起线程那一刻快照路径**，不许运行时查全局变量 —— 否则 monkeypatch
   会在写侧失效，把审计数据搬进真实`outputs/`（§2.24 教训）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

import config as cfg

AUDIT_ENV = "VM_AI_AUDIT_FILE"
_MAX_PARAMS_CHARS = 400


class AuditUnavailable(RuntimeError):
    """审计落盘失败 → 写动作必须被拒（fail-closed）。"""


def audit_path() -> str:
    """每次调用现取，让 `VM_OUTPUTS_DIR` / `VM_AI_AUDIT_FILE` 的 monkeypatch 生效。"""
    override = os.environ.get(AUDIT_ENV, "")
    if override:
        return override
    return str(cfg.OUTPUTS_DIR / "ai_actions.jsonl")


def redact_params(params: Any) -> Any:
    """审计里的参数也要脱敏 —— 动作参数可能带用户文字（虽然是白名单动作）。"""
    if not isinstance(params, dict):
        return {"_non_dict": str(params)[:_MAX_PARAMS_CHARS]}
    out: dict[str, Any] = {}
    for k, v in params.items():
        s = v if isinstance(v, (int, float, bool)) or v is None else str(v)
        out[k] = s if len(str(s)) <= _MAX_PARAMS_CHARS else str(s)[:_MAX_PARAMS_CHARS] + "…"
    return out


def record(
    action: str,
    params: Any,
    *,
    dry_run: bool,
    ok: bool,
    code: int,
    actor: str = "unknown",
    evidence: Any = None,
    duration_ms: int = 0,
    message: str = "",
) -> dict:
    """追加一条审计。**抛`AuditUnavailable` 时调用方必须拒绝执行**。

    落盘失败绝不静默吞掉：吞掉的话调用方会以为写成功了，
    而这个端点的全部可信度都建立在「有账本」上。
    """
    entry = {
        "id": uuid.uuid4().hex[:12],
        "ts": int(time.time()),
        "action": action,
        "params": redact_params(params),
        "dry_run": dry_run,
        "ok": ok,
        "code": code,
        "duration_ms": duration_ms,
        "actor": actor or "unknown",
        "evidence": evidence,
        "message": message[:200],
    }
    path = audit_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        # append-only：只开追加模式，且每次写完立刻 flush ——
        # 崩溃后账本不能少最后一条（写动作已经发生过了）。
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        raise AuditUnavailable(f"审计落盘失败，动作未执行：{exc}") from exc
    return entry


def tail(limit: int = 100) -> dict:
    """读最近 N 条审计（倒序）。**只读**，坏行跳过而不是整体 500。"""
    path = audit_path()
    if not os.path.isfile(path):
        return {"items": [], "total": 0, "source": path, "note": "还没有 AI 操作记录"}
    items: list[dict] = []
    skipped = 0
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    items.append(json.loads(ln))
                except json.JSONDecodeError:
                    skipped += 1
    except OSError as exc:
        return {"items": [], "total": 0, "source": path, "note": f"审计文件读取失败：{exc}"}
    return {
        "items": items[-max(1, int(limit)) :][::-1],
        "total": len(items),
        "source": path,
        "skipped_bad_lines": skipped,
        "note": "审计文件与作品库分开保存，清作品库不会清掉它" if items else "",
    }