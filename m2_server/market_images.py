"""音色市场 —— 配图解析与远程图库同步。

配图优先级：
  1. 远程图库缓存  outputs/market/imgs_cache/<voice_id>.<ext>
     （启动/定时自动同步，需配置 VM_MARKET_IMG_REPO，未配置则此层为空）
  2. 打包图        m2_server/assets/market_imgs/<voice_id>.<ext>
     （随安装包分发，作为出厂默认）
  3. 都没有        前端按分类配色 + 首字母占位（image 字段缺省）

返回给前端的 URL 带内容指纹（`?v=<size>-<mtime>`，见 `image_url()`）——
配图端点是 `Cache-Control: max-age=86400`，没有指纹的话"换图"要等 24h 才可见，
远程图库那套「推文件即更新」的设计就废了。

远程图库（业界做法：Modrinth icon_url / CurseForge thumbnail / GitHub 图床+CDN 缓存）：
  图源中心化、客户端零配置——默认图库 repo 内置（mouxu66/voice-market-assets，public），
  所有用户桌面端启动/刷新市场时自动经 jsdelivr 同步；作者换图 = 往仓库推文件。
  仓库结构：
      images.json                {"revision": "<任意字符串，内容变了就换>", "imgs": {"<voice_id>": "imgs/x.png"}}
      imgs/<voice_id>.png|jpg    图片文件
  同步通道：cdn.jsdelivr.net/gh（国内可达、免认证；分支引用 CDN 缓存约 12h，
  换图最多延迟半天）→ 失败回退 raw.githubusercontent.com。
  VM_MARKET_IMG_REPO 环境变量可换库；设为空则禁用远程层（只用打包图）。
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

SYNC_TTL = 6 * 3600  # 远程清单刷新间隔（秒）
_HTTP_TIMEOUT = (5, 30)  # (连接, 读取) 超时
_IMG_EXTS = ("png", "jpg", "jpeg", "webp")

_REPO = os.environ.get("VM_MARKET_IMG_REPO", "mouxu66/voice-market-assets").strip()
# 默认图库内置在产品里（业界做法：图源中心化，客户端零配置，jsdelivr CDN 全球可达）。
# 设 VM_MARKET_IMG_REPO 可换库，设 VM_MARKET_IMG_REPO= 空可禁用远程层（只用打包图）。
_CDN_TPL = "https://cdn.jsdelivr.net/gh/{repo}@main/{path}"
_FALLBACK_TPL = "https://raw.githubusercontent.com/{repo}/main/{path}"

_lock = threading.Lock()
_state = {
    "last_sync": 0.0,
    "revision": None,
    "ok": None,
    "error": "",
    "downloaded": 0,
    "failed": 0,
}


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
    """有本地配图则返回 /api 相对路径（前端过 mediaUrl 转绝对），否则 None。

    带 `?v=<size>-<mtime_ns>` 内容指纹，**这是远程图库能生效的前提**：
    `market_api.market_image()` 返回 `Cache-Control: public, max-age=86400`，
    而 URL 原本只含 voice_id —— 于是「作者推新图 → 客户端同步到新图」之后，
    Chromium 仍会拿缓存里那份旧图顶 **24 小时**（后端端口固定 8000，
    源与 URL 都没变），图库「换图即生效」的承诺直接失效。
    带上指纹后：文件一变 URL 就变 → 缓存自然失效；文件没变则 URL 稳定、
    24h 缓存照旧生效（省掉重复传输）。

    指纹用 size+mtime 而不是内容哈希：列表接口一次要算 30 条，
    读内容（约 300KB）会让每次 `/api/market` 都多一轮磁盘 IO；
    stat 是免费的，而 size+mtime 对"换图"这件事足够敏感。
    """
    path = local_image_path(voice_id)
    if path is None:
        return None
    vid = str(voice_id).strip().lower()
    try:
        st = path.stat()
        # 用 mtime_ns 而不是 int(mtime)：后者截断到秒，"同一秒内换成同样大小的图"
        # 会得到同一个指纹（NTFS 的精度是 100ns，白白丢掉）
        stamp = f"?v={st.st_size:x}-{st.st_mtime_ns:x}"
    except OSError:
        stamp = ""      # stat 失败不该让整条列表挂掉；退回无指纹（仍是合法 URL）
    return f"{API_PREFIX}/market/image/{vid}{stamp}"


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
    return {
        "last_sync": _state["last_sync"],
        "revision": _state["revision"],
        "ok": _state["ok"],
        "error": _state["error"],
        "downloaded": _state["downloaded"],
        "failed": _state["failed"],
    }


def _valid_entry(vid, rel) -> tuple[str, str, str] | None:
    """校验并归一化一条清单条目 → (voice_id, rel, ext)；脏条目返回 None。

    下载与"查缺"共用同一份判据，避免两处口径漂移（一处放行、另一处拦住）。
    """
    vid = str(vid).strip().lower()
    rel = str(rel).lstrip("/")
    ext = Path(rel).suffix.lower().lstrip(".")
    if (
        not re.fullmatch(r"[a-z0-9_]{1,64}", vid)
        or not rel
        or ".." in rel
        or ext not in _IMG_EXTS
    ):
        return None
    return vid, rel, ext


def _entries(doc: dict) -> list[tuple[str, str, str]]:
    """清单 → 合法条目列表（脏条目丢弃，不发请求）。"""
    out = []
    for vid, rel in (doc.get("imgs") or {}).items():
        e = _valid_entry(vid, rel)
        if e is not None:
            out.append(e)
    return out


def _missing(entries: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """其中本地缓存**还没有**的那些。"""
    return [e for e in entries if not (CACHE_DIR / f"{e[0]}.{e[2]}").is_file()]


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

    entries = _entries(doc)
    # ★ 两种触发条件，作用**不同**，不能合并成"只下缺的"：
    #   ① revision 变了 → 清单内容可能变了。作者换图常常**不改文件名**
    #      （改的是同一张 `diyin.png` 的内容）→ 必须**全量重下**，
    #      否则同名新图永远盖不掉本地旧文件，"换图"看起来没生效。
    #   ② revision 没变、但本地缺文件 → 上次同步没下全。下载循环里单个文件
    #      失败是 `continue` 跳过，但循环结束后**仍然写入新 revision** →
    #      下次只看 revision 就会整体跳过，**缺的文件永远补不上**。
    #      2026-09-18 实测过一次：安装版缓存长期停在 12/30 张，
    #      而因为 `local_image_path()` 缓存优先，其余 18 张静默回退到打包图，
    #      表面完全看不出来。
    revision_changed = not revision or revision != last_revision
    todo = entries if revision_changed else _missing(entries)
    if not todo:
        _state.update(
            ok=True, error="", revision=revision or None,
            last_sync=time.time(), downloaded=0, failed=0,
        )
        REVISION_FILE.write_text(revision, "utf-8")
        return

    downloaded = failed = 0
    for vid, rel, ext in todo:
        content = _http_get(_CDN_TPL.format(repo=_REPO, path=rel)) or _http_get(
            _FALLBACK_TPL.format(repo=_REPO, path=rel)
        )
        if content is None:
            failed += 1
            continue
        (CACHE_DIR / f"{vid}.{ext}").write_bytes(content)
        downloaded += 1
    _state.update(
        ok=failed == 0,
        error="" if failed == 0 else f"{failed} 张下载失败（下次同步自动补拉）",
        revision=revision or None,
        last_sync=time.time(),
        downloaded=downloaded,
        failed=failed,
    )
    # revision **照写**，即使这次有文件失败 —— 因为上面 ② 的补拉机制会接手：
    # 下次 `revision_changed=False` → `todo = _missing(...)` → 只重试缺的那几张。
    #
    # 反过来（失败就不写）看着更"严谨"，实际有个更糟的退化：
    # 若某张图**永久**失败（远程仓库里那张坏了/被删了但清单仍引用它），
    # revision 就永远写不进去 → 每次同步都当"首次"→ **每次都全量重下全部图**。
    # 而照写的话，永久失败的那张每次只重试它自己 1 张，开销可忽略。
    REVISION_FILE.write_text(revision, "utf-8")


def start_background_sync(delay: float = 3.0) -> None:
    """后端启动后台线程同步一次（不阻塞主链路；未配置 repo 直接返回）。"""
    if not _REPO:
        return

    def _run() -> None:
        time.sleep(delay)
        sync_remote(force=True)

    threading.Thread(target=_run, name="market-img-sync", daemon=True).start()
