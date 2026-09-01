"""变声任务持久化：统一登记各产出（tts/offlinevc/audiobook/fx），支持查询与删除。

设计（见 FRD F3）：
    - 追加写 outputs/history.jsonl（单行 JSON），读时倒序解析，坏行跳过不 500。
    - 历史上限 VM_HISTORY_MAX（默认 2000），超限裁剪最旧记录（可选一并删 wav）。
    - 写入失败（磁盘满等）不阻断主流程，仅日志告警。
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

import config as cfg

HISTORY_FILE = cfg.OUTPUTS_DIR / "history.jsonl"
_MAX_ITEMS = int(os.environ.get("VM_HISTORY_MAX", "2000"))
_KINDS = {"tts", "offlinevc", "audiobook", "fx", "trial", "mine"}

_lock = threading.Lock()


def register(kind: str, voice_id: str, wav: str, url: str, duration_s: float,
            input_text: str = "", params: dict | None = None) -> str:
    """登记一条产出历史，返回记录 id。写失败返回空串（不抛异常，不阻断主流程）。"""
    item_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    rec = {
        "id": item_id,
        "ts": int(time.time()),
        "kind": kind if kind in _KINDS else "tts",
        "voice_id": voice_id or "",
        "wav": wav,
        "url": url,
        "duration_s": round(float(duration_s), 2),
        "input_text": (input_text or "")[:500],
        "params": params or {},
    }
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[history] 登记失败（不影响主流程）: {e}", flush=True)
        return ""
    _trim()
    return item_id


def _read_all() -> list[dict]:
    """倒序读取全部记录；坏行跳过。"""
    items: list[dict] = []
    if not HISTORY_FILE.exists():
        return items
    try:
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 坏行跳过，不 500
    except Exception:
        return items
    items.reverse()
    return items


def query(kind: str | None = None, voice_id: str | None = None,
          from_ts: int | None = None, to_ts: int | None = None,
          limit: int = 50, offset: int = 0) -> dict:
    """按 kind / voice_id / 时间范围过滤，ts 倒序分页返回。"""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    out = []
    for r in _read_all():
        if kind and r.get("kind") != kind:
            continue
        if voice_id and r.get("voice_id") != voice_id:
            continue
        ts = int(r.get("ts") or 0)
        if from_ts is not None and ts < int(from_ts):
            continue
        if to_ts is not None and ts > int(to_ts):
            continue
        out.append(r)
    total = len(out)
    return {"items": out[offset:offset + limit], "total": total,
            "limit": limit, "offset": offset}


def delete(item_id: str, keep_file: bool = False) -> dict:
    """删除记录；keep_file=False 时一并删除对应 wav。返回结果。"""
    found = False
    wav = ""
    items = _read_all()
    kept = []
    for r in items:
        if r.get("id") == item_id:
            found = True
            wav = r.get("wav") or ""
            continue
        kept.append(r)
    if not found:
        return {"ok": False, "error": "记录不存在", "file_gone": False}
    kept.reverse()  # _read_all 倒序，写回前还原为正序
    try:
        with _lock:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                for r in kept:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception as e:
        return {"ok": False, "error": str(e), "file_gone": False}

    file_gone = False
    if not keep_file and wav:
        p = cfg.OUTPUTS_DIR / Path(wav).name
        if p.exists():
            try:
                p.unlink(missing_ok=True)
            except Exception:
                file_gone = True
        else:
            file_gone = True
    return {"ok": True, "file_gone": file_gone}


def _trim() -> None:
    """历史上限裁剪：保留最新 _MAX_ITEMS 条，超出的删除（记录 + 可选 wav）。"""
    items = _read_all()  # 已倒序（最新在前）
    if len(items) <= _MAX_ITEMS:
        return
    overflow = items[_MAX_ITEMS:]
    keep = items[:_MAX_ITEMS]
    # 删除溢出记录的 wav 文件
    for r in overflow:
        wav = r.get("wav") or ""
        if wav:
            p = cfg.OUTPUTS_DIR / Path(wav).name
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
    try:
        with _lock:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                for r in reversed(keep):  # 还原正序
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:
        pass
