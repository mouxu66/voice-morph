"""变声任务持久化：统一登记各产出（tts/offlinevc/audiobook/fx），支持查询与删除。

设计（见 FRD F3）：
    - 追加写 outputs/history.jsonl（单行 JSON），读时倒序解析，坏行跳过不 500。
    - 历史上限 VM_HISTORY_MAX（默认 2000），超限裁剪最旧记录（可选一并删 wav）。
    - 写入失败（磁盘满等）不阻断主流程，仅日志告警。

作品库（B1）：每条记录带 `starred`（收藏）与 `tags`（自定义标签），可单独改、
可按收藏/标签筛选；支持批量删除与批量打包导出 zip。老记录无这两个字段时
读出来补默认值，不需要迁移历史文件。
"""
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

import config as cfg

HISTORY_FILE = cfg.OUTPUTS_DIR / "history.jsonl"
_MAX_ITEMS = int(os.environ.get("VM_HISTORY_MAX", "2000"))
_MAX_TAGS = 8                       # 单条记录标签上限（防滥用）
_MAX_TAG_LEN = 16
_TAG_RE = re.compile(r"^[^\s,，;；/\\]{1,%d}$" % _MAX_TAG_LEN)
_KINDS = {"tts", "offlinevc", "audiobook", "fx", "trial", "mine", "seedvc"}

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
        "starred": False,
        "tags": [],
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


def _normalize(rec: dict) -> dict:
    """补齐老记录缺失的收藏/标签字段（不落盘，读时补）。"""
    if "starred" not in rec:
        rec["starred"] = False
    if not isinstance(rec.get("tags"), list):
        rec["tags"] = []
    return rec


def clean_tags(tags) -> list[str]:
    """清洗标签：去空白/去重/过滤非法与超长（>16 字拒收），最多 _MAX_TAGS 个。"""
    out: list[str] = []
    for t in (tags or []):
        t = str(t).strip()
        if not t or not _TAG_RE.match(t) or t in out:
            continue
        out.append(t)
        if len(out) >= _MAX_TAGS:
            break
    return out


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
                items.append(_normalize(json.loads(line)))
            except json.JSONDecodeError:
                continue  # 坏行跳过，不 500
    except Exception:
        return items
    items.reverse()
    return items


def _write_all(items: list[dict]) -> None:
    """把（倒序的）记录列表还原为正序写回。"""
    with _lock:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            for r in reversed(items):
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def query(kind: str | None = None, voice_id: str | None = None,
          from_ts: int | None = None, to_ts: int | None = None,
          limit: int = 50, offset: int = 0,
          starred: bool | None = None, tag: str | None = None) -> dict:
    """按 kind / voice_id / 时间范围 / 收藏 / 标签过滤，ts 倒序分页返回。"""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    out = []
    for r in _read_all():
        if kind and r.get("kind") != kind:
            continue
        if voice_id and r.get("voice_id") != voice_id:
            continue
        if starred is not None and bool(r.get("starred")) != bool(starred):
            continue
        if tag and tag not in (r.get("tags") or []):
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


def all_tags() -> list[dict]:
    """全部标签及其使用次数（次数降序、同名升序），供前端渲染筛选 chips。"""
    counter: dict[str, int] = {}
    for r in _read_all():
        for t in (r.get("tags") or []):
            counter[t] = counter.get(t, 0) + 1
    return [{"tag": t, "count": c}
            for t, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]


def set_meta(item_id: str, starred: bool | None = None,
             tags: list | None = None) -> dict:
    """改单条记录的收藏 / 标签（None 表示不改）。返回更新后的记录；不存在返回 {}。"""
    items = _read_all()
    hit = None
    for r in items:
        if r.get("id") == item_id:
            if starred is not None:
                r["starred"] = bool(starred)
            if tags is not None:
                r["tags"] = clean_tags(tags)
            hit = r
            break
    if hit is None:
        return {}
    try:
        _write_all(items)
    except Exception as e:
        return {"error": str(e)}
    return hit


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
        _write_all(kept)
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


def bulk_delete(item_ids: list[str], keep_file: bool = False) -> dict:
    """批量删除：复用 delete 的语义，逐条执行并汇总，单条失败不中断。"""
    deleted, failed = 0, []
    for i in item_ids or []:
        res = delete(i, keep_file=keep_file)
        if res.get("ok"):
            deleted += 1
        else:
            failed.append({"id": i, "error": res.get("error") or "未知错误"})
    return {"ok": not failed, "deleted": deleted, "failed": failed}


def export_zip(item_ids: list[str]) -> tuple[bytes, list[str], list[str]]:
    """把指定记录的 wav 打包成 zip；返回 (zip字节, 打包的文件名, 缺失的文件名)。

    缺失（已被清理/手工删除）的文件跳过而不报错，前端提示"N 个文件已不在磁盘"。
    """
    import io
    import zipfile

    wanted = {i for i in (item_ids or [])}
    picked, missing = [], []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in _read_all():
            if r.get("id") not in wanted:
                continue
            wav = r.get("wav") or ""
            if not wav:
                continue
            p = cfg.OUTPUTS_DIR / Path(wav).name
            if not p.exists():
                missing.append(Path(wav).name)
                continue
            # 同名不同记录时加 id 前缀去重（zip 不允许重名）
            arcname = Path(wav).name
            if arcname in zf.namelist():
                arcname = f"{r['id']}_{arcname}"
            zf.write(p, arcname)
            picked.append(arcname)
    return buf.getvalue(), picked, missing


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
        _write_all(keep)  # keep 已倒序，_write_all 内部还原正序
    except Exception:
        pass
