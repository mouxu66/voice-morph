"""音色市场 —— 配图解析与远程图库同步。

配图优先级：
  1. 远程图库缓存  outputs/market/imgs_cache/<voice_id>.<ext>
     （启动/定时自动同步，需配置 VM_MARKET_IMG_REPO，未配置则此层为空）
  2. 打包图        m2_server/assets/market_imgs/<voice_id>.<ext>
     （随安装包分发，作为出厂默认）
  3. 都没有        前端按分类配色 + 首字母占位（image 字段缺省）

远程图库（业界做法：Modrinth icon_url / CurseForge thumbnail / GitHub 图床+CDN 缓存）：
  VM_MARKET_IMG_REPO 配置为 GitHub 仓库（如 "user/voice-market-assets"，public），
  仓库结构：
      images.json                {"revision": "<任意字符串，内容变了就换>", "imgs": {"<voice_id>": "imgs/x.png"}}
      imgs/<voice_id>.png|jpg    图片文件
  同步通道：cdn.jsdelivr.net/gh（国内可达、免认证；分支引用 CDN 缓存约 12h，
  换图最多延迟半天）→ 失败回退 raw.githubusercontent.com。
  换图/加图 = 往仓库推文件 + 更新 images.json 的 revision，桌面端刷新市场即自动拉新。
"""
import json
import os
import re
import threading
import time
from pathlib import Path

import requests

from config import OUTPUTS_DIR
from runtime import API_PREFIX

CACHE_DIR = OUTPUTS_DIR / "market" / "imgs_cache"
ASSET_DIR = Path(__file__).resolve().parent / "assets" / "market_imgs"
REVISION_FILE = CACHE_DIR / ".revision"

SYNC_TTL = 6 * 3600                      # 远程清单刷新间隔（秒）
_HTTP_TIMEOUT = (5, 30)                  # (连接, 读取) 超时
_IMG_EXTS = ("png", "jpg", "jpeg", "webp")

_REPO = os.environ.get("VM_MARKET_IMG_REPO", "").strip()   # "user/repo"，空=禁用
_CDN_TPL = "https://cdn.jsdelivr.net/gh/{repo}@main/{path}"
_FALLBACK_TPL = "https://raw.githubusercontent.com/{repo}/main/{path}"

_lock = threading.Lock()
_state = {"last_sync": 0.0, "revision": None, "ok": None, "error": "", "downloaded": 0}


def _http_get(url: str, timeout=_HTTP_TIMEOUT) -> bytes | None:
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "voice-morph-market/1.0"})
        if r.status_code == 200 and r.content:
            return r.content
    except Exception:
        pass
    return None


def _fetch_manifest(repo: str) -> dict | None:
    for tpl in (_CDN_TPL, _FALLBACK_TPL):
        data = _http_get(tpl.format(repo=repo, path="images.json"))
        if data:
            try:
                doc = json.loads(data)
                if isinstance(doc, dict) and isinstance(doc.get("imgs"), dict):
                    return doc
            except Exception:
                pass
    return None


def _find_local(directory: Path, voice_id: str) -> Path | None:
    for ext in _IMG_EXTS:
        p = directory / f"{voice_id}.{ext}"
        if p.is_file():
            return p
    return None


def local_image_path(voice_id: str) -> Path | None:
    """按优先级解析某音色的本地配图；没有则 None。"""
    vid = str(voice_id).strip().lower()
    if not vid:
        return None
    return _find_local(CACHE_DIR, vid) or _find_local(ASSET_DIR, vid)


def image_url(voice_id: str) -> str | None:
    """有本地配图则返回 /api 相对路径（前端过 mediaUrl 转绝对），否则 None。"""
    if local_image_path(voice_id) is None:
        return None
    return f"{API_PREFIX}/market/image/{str(voice_id).strip().lower()}"


def sync_remote(force: bool = False) -> dict:
    """拉远程图库清单并下载新图到缓存目录。无配置/失败静默 no-op（返回状态供诊断）。

    线程安全；单飞（同刻并发调用只跑一次）。
    """
    if not _REPO:
        return {"enabled": False}
    if not force and _state["last_sync"] and time.time() - _state["last_sync"] < SYNC_TTL:
        return {"enabled": True, "skipped": "ttl", **_snapshot()}
    with _lock:
        if not force and _state["last_sync"] and time.time() - _state["last_sync"] < SYNC_TTL:
            return {"enabled": True, "skipped": "ttl", **_snapshot()}
        try:
            _sync_locked()
        except Exception as exc:  # 网络等异常不进主链路
            _state.update(ok=False, error=str(exc), last_sync=time.time())
    return {"enabled": True, **_snapshot()}


def _snapshot() -> dict:
    return {"last_sync": _state["last_sync"], "revision": _state["revision"],
            "ok": _state["ok"], "error": _state["error"], "downloaded": _state["downloaded"]}


def _sync_locked() -> None:
    doc = _fetch_manifest(_REPO)
    if doc is None:
        _state.update(ok=False, error="manifest unreachable", last_sync=time.time())
        return
    revision = str(doc.get("revision") or "")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    last_revision = _state.get("revision")
    if last_revision is None and REVISION_FILE.is_file():
        last_revision = REVISION_FILE.read_text("utf-8").strip()
    if revision == last_revision and revision:
        _state.update(ok=True, error="", revision=revision or None, last_sync=time.time(),
                      downloaded=0)
        REVISION_FILE.write_text(revision, "utf-8")
        return
    downloaded = 0
    for vid, rel in doc["imgs"].items():
        vid = str(vid).strip().lower()
        rel = str(rel).lstrip("/")
        ext = Path(rel).suffix.lower().lstrip(".")
        # 先验证（vid 白名单 + 相对路径 + 扩展名），再下载——脏条目不发请求
        if not re.fullmatch(r"[a-z0-9_]{1,64}", vid) or not rel or ".." in rel \
                or ext not in _IMG_EXTS:
            continue
        content = _http_get(_CDN_TPL.format(repo=_REPO, path=rel)) \
            or _http_get(_FALLBACK_TPL.format(repo=_REPO, path=rel))
        if content is None:
            continue
        (CACHE_DIR / f"{vid}.{ext}").write_bytes(content)
        downloaded += 1
    _state.update(ok=True, error="", revision=revision or None, last_sync=time.time(),
                  downloaded=downloaded)
    REVISION_FILE.write_text(revision, "utf-8")


def start_background_sync(delay: float = 3.0) -> None:
    """后端启动后台线程同步一次（不阻塞主链路；未配置 repo 直接返回）。"""
    if not _REPO:
        return

    def _run() -> None:
        time.sleep(delay)
        sync_remote(force=True)

    threading.Thread(target=_run, name="market-img-sync", daemon=True).start()
