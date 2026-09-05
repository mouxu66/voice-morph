"""音色市场 —— 双源搜索 / 仓库文件浏览 / 直链构造。

下载源抽象（借鉴 HMCL 思路）：
  - HuggingFace 源：搜索/仓库文件走 HF Model Hub API（经 hf-mirror.com 可达），
    官方 huggingface.co 作为镜像通道。
  - ModelScope 源：**官网已下线匿名搜索 API**（探测 2026-09-05：列表接口 404、
    POST /api/v1/models 被网关 401 反爬），故 search_ms() 退化为
    「精确路径解析 + 精选清单过滤」；仓库文件/直链则走仍可用的
    detail / repo-files / resolve API。

所有对外直链均落域名白名单（见 market_download.ALLOWED_HOSTS）。
"""
import re
import urllib.parse

import requests

from market_download import MarketError
from market_manifest import get_manifest

HF_API = "https://hf-mirror.com"              # 主 API 基址（本机可达；官方不可达）
HF_OFFICIAL = "https://huggingface.co"        # 镜像基址
MS_BASE = "https://modelscope.cn"

API_TIMEOUT = 20

# 每个搜索结果的排序权重（HF API 的 downloads/likes 在镜像上时常为 0，用 id 相关性兜底）
SEARCH_LIMIT_MAX = 50
FILE_LOOKUP_TOP = 3          # 搜索时对前 N 条做文件探测（每模型一次 tree 请求）
PTH_RE = re.compile(r"\.pth$", re.IGNORECASE)


# ---------------- 直链构造 ----------------
def hf_resolve(repo: str, path: str, base: str = HF_API) -> str:
    """HF 直链：{base}/{repo}/resolve/main/{path}（域名与路径独立，便于镜像切换）。"""
    return f"{base}/{urllib.parse.quote(repo, safe='/')}/resolve/main/{urllib.parse.quote(path, safe='/')}"


def ms_resolve(model_id: str, path: str) -> str:
    """魔搭直链（官方 SDK 模板）：/api/v1/models/{id}/repo?Revision=master&FilePath=.."""
    return (f"{MS_BASE}/api/v1/models/{urllib.parse.quote(model_id, safe='/')}"
            f"/repo?Revision=master&FilePath={urllib.parse.quote(path, safe='/')}")


# ---------------- HuggingFace ----------------
def _get_json(url: str, **params) -> list | dict:
    try:
        resp = requests.get(url, params=params, timeout=API_TIMEOUT,
                            headers={"User-Agent": "voice-morph/0.1"})
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        raise MarketError(f"请求失败: {url} ({exc.__class__.__name__})") from exc


def search_hf(query: str, limit: int = 10) -> list[dict]:
    """HF 模型搜索（hf-mirror Hub API）。

    文件探测代价高（每模型一次 tree 请求），只对前 FILE_LOOKUP_TOP 条执行，
    其余仅返回仓库元数据；安装文件列表请走 /market/repo 或精选清单。
    """
    limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
    results = _get_json(f"{HF_API}/api/models", search=query, limit=limit, full=True) or []
    items: list[dict] = []
    for m in results[:limit]:
        repo = m.get("id", "")
        if not repo:
            continue
        items.append({
            "id": repo,
            "name": _hf_title(m),
            "platform": "hf",
            "repo": repo,
            "downloads": int(m.get("downloads") or 0),
            "likes": int(m.get("likes") or 0),
            "tags": [t for t in m.get("tags") or []]
                    + [t for t in m.get("library_name") or [] if t],
            "updated_at": (m.get("lastModified") or "")[:10],
            "files": _hf_pick_files(repo) if len(items) < FILE_LOOKUP_TOP else [],
        })
    return items


def _hf_title(m: dict) -> str:
    for k in ("cardData", "tags"):
        pass
    pretty = (m.get("cardData") or {}).get("language") or ""
    tag_hint = next((t for t in (m.get("tags") or []) if t.lower() in ("rvc", "voice")), "")
    return pretty or (m.get("author", "") + "/" + m.get("name", "")) or m.get("id", "")


def _hf_pick_files(repo: str) -> list[dict]:
    """列出仓库顶层文件，挑出 .pth/.index/.zip 并附直链（供前端选文件/安装）。"""
    try:
        tree = _get_json(f"{HF_API}/api/models/{urllib.parse.quote(repo)}/tree/main",
                         recursive=False) or []
    except MarketError:
        return []
    files = []
    for f in tree:
        path = f.get("path", "")
        ftype = f.get("type")
        if ftype == "directory":
            continue
        if not path.lower().endswith((".pth", ".index", ".zip")):
            continue
        entry: dict = {
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "size": int(f.get("size") or 0),
            "type": "zip" if path.lower().endswith(".zip") else "model",
            "url": hf_resolve(repo, path, HF_API),
            "mirror_url": hf_resolve(repo, path, HF_OFFICIAL),
        }
        lfs = f.get("lfs") or {}
        if isinstance(lfs, dict) and lfs.get("sha256"):
            entry["sha256"] = lfs["sha256"]
        files.append(entry)
    return files[:20]


def repo_files_hf(repo: str, recursive: bool = False) -> list[dict]:
    """完整仓库文件列表（前端"查看文件"用；含目录树）。"""
    tree = _get_json(f"{HF_API}/api/models/{urllib.parse.quote(repo)}/tree/main",
                     recursive=recursive) or []
    return [{"name": f.get("path", "").rsplit("/", 1)[-1], "path": f.get("path", ""),
             "size": int(f.get("size") or 0), "type": f.get("type"), "url": None} for f in tree]


# ---------------- ModelScope ----------------
def search_ms(query: str, limit: int = 10) -> dict:
    """魔搭搜索（官网匿名搜索已下线 → 降级）。

    降级策略：
      1. 形如 owner/name → 走 detail API 精确解析（可安装条目）；
      2. 否则按关键词过滤精选清单中的魔搭条目；
    返回 {items, note}，note 说明降级原因，前端可在搜索 Tab 提示。
    """
    limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
    q = query.strip()
    items: list[dict] = []
    if re.fullmatch(r"[A-Za-z0-9_\-]+/[A-Za-z0-9_\-.]+", q):
        try:
            detail = _get_json(f"{MS_BASE}/api/v1/models/{urllib.parse.quote(q, safe='/')}")
            if isinstance(detail, dict) and detail.get("Code") == 200:
                d = detail.get("Data") or {}
                # 注意：detail API 的 Path 仅返回 owner（如 "hudddd"），全路径需补 Name
                owner = (d.get("Path") or "").strip("/")
                name = (d.get("Name") or "").strip()
                repo = owner if "/" in owner else f"{owner}/{name}".strip("/")
                tags = [str(d.get("Task") or ""), str(d.get("Backbone") or "")]
                if isinstance(d.get("Tags"), list):
                    tags += [str(t) for t in d["Tags"]]
                items.append({
                    "id": d.get("ModelId") or repo,
                    "name": d.get("ChineseName") or name or repo,
                    "platform": "modelscope",
                    "repo": repo,
                    "downloads": int(d.get("Downloads") or 0),
                    "likes": int(d.get("Likes") or 0),
                    "tags": [t for t in tags if t],
                    "files": repo_files_ms(repo, _only=True) if repo else [],
                    "updated_at": (d.get("UpdatedTime")
                                   or d.get("LastUpdateTime") or "")[:10],
                })
        except MarketError:
            items = []
    if not items:
        for m in get_manifest():
            if m["platform"] != "modelscope":
                continue
            if q and q.lower() not in (m["name"] + m["voice_id"]).lower() and \
                    q.lower() not in m.get("desc", "").lower():
                continue
            items.append(_manifest_to_search(m))
    return {"items": items[:limit], "note": "魔搭官网已下线匿名搜索，已按精确路径/精选匹配返回"}


def _manifest_to_search(m: dict) -> dict:
    files = []
    if m.get("download"):
        files.append({"name": m["download"]["url"].rsplit("/", 1)[-1], "path": "download",
                      "size": 0, "type": "model", "url": m["download"]["url"]})
    return {"id": m["id"], "name": m["name"], "platform": "modelscope",
            "repo": m["repo"], "downloads": 0, "likes": 0, "tags": ["rvc"],
            "prefs": m, "files": files}


def repo_files_ms(model_id: str, _only: bool = False, recursive: bool = True) -> list[dict]:
    """魔搭仓库文件列表（detail/repo-files API 仍可用）。"""
    try:
        data = _get_json(f"{MS_BASE}/api/v1/models/{urllib.parse.quote(model_id, safe='/')}/repo/files",
                         Revision="master", Recursive=recursive)
    except MarketError:
        return []
    flist = data.get("Data", {}).get("Files") or []
    out = []
    for f in flist:
        path, ftype, size = f.get("Path", ""), f.get("Type", "blob"), int(f.get("Size") or 0)
        if _only and ftype != "blob":
            continue
        if _only and not path.lower().endswith((".pth", ".index", ".zip")):
            continue
        out.append({"name": path.rsplit("/", 1)[-1], "path": path, "size": size,
                    "type": ftype, "sha256": f.get("Sha256") or None,
                    "url": ms_resolve(model_id, path) if ftype == "blob" else None})
    return out[:20] if _only else out


# ---------------- 统一入口 ----------------
def search(platform: str, query: str, limit: int = 10) -> dict:
    """统一搜索入口：platform ∈ hf / modelscope / all。"""
    platform = (platform or "all").lower()
    note = ""
    out: list[dict] = []
    if platform in ("hf", "all"):
        try:
            out += search_hf(query, limit if platform == "hf" else max(1, limit // 2))
        except MarketError as exc:
            note += f"HF: {exc}; "
    if platform in ("modelscope", "all"):
        try:
            r = search_ms(query, limit if platform == "modelscope" else max(1, limit // 2))
            out += r["items"]
            if r["note"] and platform == "modelscope":
                note += r["note"]
        except MarketError as exc:
            note += f"魔搭: {exc}; "
    return {"items": out, "note": note.strip(" ;") or None}