"""人偶皮肤市场 —— GitHub 扫描器：搜索仓库 → 探测素材 → 试转 → 候选清单。

参考 HMCL 市场：HMCL 是「订阅第三方平台 JSON 索引」；我们没有自有平台，所以自落
一个扫描器，用 GitHub Search API 发现开源桌宠/像素宠物素材仓库，探测仓库内可能
含皮肤动画的候选文件，**下载原文件试转**成标准皮肤包（gif-multi / pixel-json
两类可全自动适配），试转通过即写入外置清单（OUT/pet-scan-ext.json）——
「扫描即上线」，发现 Tab 可一键安装，走既有 install 队列。

把关（许可 + 能不能转）：
  - 只收录带宽松许可（MIT/Apache-2.0/BSD/Mozilla/CC0/Unlicense…）的仓库；
    无许可/未知许可跳过（绝不把「许可不明」素材扫进市场）。
  - 候选必须完整走 build_skin + validate_skin_files 试转，失败即丢弃并记录原因。
  - atlas-8x9 类依赖人工 meta（frameW/frameH/row_map），无法全自动试转，
    不进 ext 清单，只在进度里统计「跳过（需人工参数）」。

安全：只用 api.github.com（GET，无 SSRF 面）；素材下载复用 pet_market 白名单
下载器（raw.githubusercontent.com + 重定向逐跳校验）。
GitHub 未认证限流（core 60/h、search 10/min）可用 GITHUB_TOKEN 环境变量提升；
限流时扫描器优雅停止并提示。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.parse
from pathlib import Path

import requests

from pet_market import MAX_SOURCE_BYTES, _download_to, add_ext_item, get_ext_items, remove_ext_item
from pet_skin_build import SkinBuildError, build_skin, validate_skin_files
from runtime import OUT

# ---- 扫描运行状态（同一时间仅一个扫描线程） ----
_SCAN_LOCK = threading.Lock()
_SCAN: dict = {
    "status": "idle",      # idle | running | done | failed | cancelled
    "phase": "",           # searching | probing | building | done
    "step": "",            # 当前正在处理的 repo/文件描述
    "total": 0, "current": 0,                # 仓库级进度
    "repos_seen": 0, "repos_lic_skip": 0, "repos_tree_skip": 0,
    "candidates": 0, "built_ok": 0, "built_fail": 0, "atlas_skip": 0,
    "error": "", "cancel": False,
    "finished_at": "",
    # 试转失败原因日志（前端「试转失败」计数可展开看原因）；上限防止大扫描撑爆状态
    "fails": [],            # [{repo, path, reason}, ...]
}

GITHUB_API = "https://api.github.com"
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30
MAX_REPOS = 15            # 单次最多探测仓库数（未认证 core 限流 60/h 预算内）
MAX_TREE_ENTRIES = 1500   # 单仓库树条目上限，防大库拖死扫描
MAX_WORK_FILES = 40       # 单仓库最多候选文件数
FAIL_LOG_MAX = 30         # 失败原因最多记录条数
_UA = {"User-Agent": "TraePetMarket/1.0 (discovery)"}

_LIC_ALLOWED = {
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "mpl-2.0",
    "cc0-1.0", "cc-by-4.0", "cc-by-sa-4.0", "unlicense", "isc", "wtfpl",
}
_SEED_QUERIES = ["desktop pet sprite", "desktop-pet", "pixel pet animation", "pet spritesheet"]

_FILE_KEYWORDS = ("pet", "sprite", "spritesheet", "anim", "atlas",
                  "idle", "walk", "walking", "run", "sleep", "slime",
                  "capybara", "cat", "dog", "fox", "duck", "penguin", "bear")
_SKIP_DIR_WORDS = ("node_modules", "/dist/", "/build/", "/docs/", "/icons/",
                   "/assets/svg/", "/svg/", "/fonts/", "/tests/", "/test/", "/.git/")


class ScanError(Exception):
    """扫描器业务错误（API 捕获后映射 409/400/503）。"""


# ---------------- GitHub API ----------------


def _gh_get(path: str, params: dict | None = None) -> dict | None:
    """请求 GitHub REST API（UA + 可选 Token + 限流检测）；404 返回 None。"""
    headers = dict(_UA)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"token {token}"
    try:
        r = requests.get(f"{GITHUB_API}{path}", headers=headers, params=params or {},
                         timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
    except requests.RequestException as exc:
        raise ScanError(f"GitHub API 请求失败: {exc}") from exc
    if r.status_code == 404:
        return None
    if r.status_code == 403:
        rem = r.headers.get("X-RateLimit-Remaining")
        if rem is not None and rem.isdigit() and int(rem) <= 0:
            raise ScanError("GitHub API 限流已耗尽。可设置环境变量 GITHUB_TOKEN 提升配额，"
                            "或稍等一分钟后再试。")
        raise ScanError(f"GitHub API 拒绝访问（403）: {r.text[:200]}")
    if r.status_code >= 400:
        raise ScanError(f"GitHub API 错误 {r.status_code}: {r.text[:200]}")
    return r.json()


# ---------------- 扫描状态 ----------------


def _reset(**kw) -> None:
    with _SCAN_LOCK:
        _SCAN.update({"status": "idle", "phase": "", "step": "", "total": 0, "current": 0,
                      "repos_seen": 0, "repos_lic_skip": 0, "repos_tree_skip": 0,
                      "candidates": 0, "built_ok": 0, "built_fail": 0, "atlas_skip": 0,
                      "error": "", "cancel": False, "finished_at": "", "fails": []})
        _SCAN.update(kw)


def _update(**kw) -> None:
    with _SCAN_LOCK:
        _SCAN.update(kw)


def _log_fail(repo_full: str, path: str, exc: Exception) -> None:
    """记录一条试转失败原因（吞异常可以，吞原因不行）。"""
    with _SCAN_LOCK:
        if len(_SCAN["fails"]) < FAIL_LOG_MAX:
            _SCAN["fails"].append(
                {"repo": repo_full, "path": path, "reason": str(exc)[:300]})
        _SCAN["built_fail"] += 1


def progress_scan() -> dict:
    """扫描进度快照（前端发现 Tab 轮询）。"""
    with _SCAN_LOCK:
        d = dict(_SCAN)
        d["fails"] = [dict(f) for f in _SCAN.get("fails") or []]
        d.pop("cancel", None)
        d.pop("bytes", None)     # 下载器写入的临时字段
        return d


def is_scan_running() -> bool:
    with _SCAN_LOCK:
        return _SCAN["status"] == "running"


def cancel_scan() -> dict:
    """请求取消扫描（扫描线程逐步检查 cancel 标记）。"""
    with _SCAN_LOCK:
        if _SCAN["status"] != "running":
            raise ScanError("当前没有运行中的扫描")
        _SCAN["cancel"] = True
        _SCAN["step"] = "正在取消…"
        return dict(_SCAN, cancel=True)


# ---------------- 搜索与探测 ----------------


def find_repos() -> list[dict]:
    """GitHub repo search：多组关键词去重，按收藏倒序，取前 MAX_REPOS。"""
    seen: dict[str, dict] = {}
    for q in _SEED_QUERIES:
        if len(seen) >= MAX_REPOS:
            break
        data = _gh_get("/search/repositories",
                       {"q": q, "sort": "stars", "order": "desc", "per_page": 8})
        for it in (data or {}).get("items", []):
            full = it.get("full_name") or ""
            if not full or full in seen:
                continue
            seen[full] = {
                "full_name": full,
                "default_branch": it.get("default_branch") or "main",
                "license": (it.get("license") or {}).get("spdx_id") or "",
                "stars": it.get("stargazers_count") or 0,
                "description": (it.get("description") or "")[:220],
            }
    return sorted(seen.values(), key=lambda r: r["stars"], reverse=True)[:MAX_REPOS]


def _licission_ok(repo: dict) -> bool:
    """宽松许可（可再分发）才收；无许可/未知许可跳过。"""
    return repo.get("license", "").lower() in _LIC_ALLOWED


def _is_candidate(path: str, size: int) -> tuple[str | None, str]:
    """按路径/扩展名/大小判定候选类型。kind ∈ gif|pixel|atlas|none。"""
    low = path.lower()
    if any(w in low for w in _SKIP_DIR_WORDS):
        return None, "目录排除"
    if size and (size < 200 or size > MAX_SOURCE_BYTES):
        return None, "大小不符"
    ext = Path(path).suffix.lower()
    name = Path(path).name.lower()
    if ext == ".gif":
        if any(k in low for k in ("idle", "walk", "walking", "run", "sleep", "anim", "pet",
                                  "slime", "cat", "dog", "fox", "duck", "penguin", "bear")):
            return "gif", "gif 动画"
        return None, "gif 但无宠物语义"
    if ext == ".json" and any(k in name for k in ("pixel", "pet", "sprite")):
        return "pixel", "像素 JSON"
    if ext in (".png", ".webp") and any(k in low for k in ("atlas", "spritesheet")):
        return "atlas", "atlas 合成图"
    return None, "无关文件"


def _pick_candidates(tree: list[dict]) -> list[dict]:
    """从递归树筛候选（含 atlas 计数位）。返回 [{path, size, kind}]。"""
    out: list[dict] = []
    for e in tree:
        if e.get("type") != "blob":
            continue
        path = e.get("path") or ""
        size = e.get("size") or 0
        kind, _reason = _is_candidate(path, size)
        if kind:
            out.append({"path": path, "size": size, "kind": kind})
    return out[:MAX_WORK_FILES]


def _repo_tree(repo: dict) -> list[dict] | None:
    """默认分支递归文件树；过大/异常返回 None（跳过该仓库）。"""
    data = _gh_get(f"/repos/{repo['full_name']}/git/trees/{repo['default_branch']}",
                   {"recursive": "1"})
    if data is None or data.get("truncated"):
        return None
    tree = data.get("tree") or []
    if len(tree) > MAX_TREE_ENTRIES:
        return None
    return tree


# ---------------- 下载 + 试转 ----------------


def _box() -> dict:
    """下载取消标记 box：直接复用 _SCAN（实时反映 cancel_scan 的取消标记）。"""
    return _SCAN


def _pic_fname(raw_url: str) -> str:
    """raw 直链的 url-safe path 解码后 ascii 文件名（Windows 可用）。"""
    tail = Path(urllib.parse.unquote(urllib.parse.urlparse(raw_url).path)).name
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in tail) or "pet"


def _markdown_id(owner: str, rname: str, stem: str) -> str:
    """生成合法皮肤 id（≤32，[a-z0-9_-]）：scan-<owner>-<repo>-<stem>。"""
    raw = f"scan-{owner}-{rname}-{stem}".lower()
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in raw)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-_")[:32] or "scan"


def _raw_url(repo: dict, path: str) -> str:
    branch = repo["default_branch"]
    encoded = path.replace(" ", "%20")
    return f"https://raw.githubusercontent.com/{repo['full_name']}/{branch}/{encoded}"


def _gh_download(repo: dict, path: str, dst: Path, box: dict) -> None:
    """经 api.github.com contents API 下载原文件（Accept: raw 流式落盘）。

    为什么不直接下 raw 直链：大陆网络下 raw.githubusercontent.com 常年 DNS 污染
    （2026-09-10 本机实测：api.github.com 通、raw 域名 getaddrinfo 直接失败，
    扫描 4 候选全挂在下载）。contents API 与搜索/取树同域名，可达性与扫描前几步
    一致；未认证同样吃 core 60/h 配额，与取树共享预算。
    """
    headers = dict(_UA)
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"token {token}"
    headers["Accept"] = "application/vnd.github.raw"
    url = f"{GITHUB_API}/repos/{repo['full_name']}/contents/{urllib.parse.quote(path)}"
    try:
        r = requests.get(url, headers=headers, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                         stream=True)
    except requests.RequestException as exc:
        raise ScanError(f"contents API 请求失败: {exc}") from exc
    if r.status_code >= 400:
        raise ScanError(f"contents API 错误 {r.status_code}: {path}")
    try:
        total = int(r.headers.get("Content-Length") or 0)
        if total > MAX_SOURCE_BYTES:
            raise ScanError(f"源文件超过上限 {MAX_SOURCE_BYTES} 字节")
        dst.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        with dst.open("wb") as f:
            for chunk in r.iter_content(256 * 1024):
                if box.get("cancel"):
                    raise ScanError("已取消")
                if not chunk:
                    continue
                f.write(chunk)
                received += len(chunk)
                if received > MAX_SOURCE_BYTES:
                    raise ScanError(f"源文件超过上限 {MAX_SOURCE_BYTES} 字节")
        box["bytes"] = received
    finally:
        r.close()


def _fetch_source(repo: dict, path: str, dst: Path, box: dict) -> None:
    """下载候选源文件：contents API 优先，失败回退 raw 直链（复用白名单下载器）。"""
    try:
        _gh_download(repo, path, dst, box)
    except Exception:
        _download_to(_raw_url(repo, path), dst, box)


def _gif_map_from_names(paths: list[str]) -> dict[str, str]:
    """按文件名关键字分状态；无 idle 命中取第一个。"""
    idle, play = "", ""
    for p in paths:
        low = Path(p).name.lower()
        if not idle and any(k in low for k in ("idle", "stand", "sleep", "sit", "look")):
            idle = p
        if not play and any(k in low for k in ("walk", "walking", "run", "moving", "play")):
            play = p
    if not idle and paths:
        idle = paths[0]
    return {"idle": idle, "play": play}


def _try_build(kind: str, repo: dict, cand: dict, tmp: Path) -> dict:
    """下载候选源 → 试转皮肤包 → 校验。成功返回 ext 条目，失败抛 ScanError。

    kind == "gif" 时 cand["paths"] 为同仓库全部 gif 列表（合并成一个皮肤）；
    kind 其他时 cand["path"] 为单文件。
    """
    full = repo["full_name"]
    owner, rname = full.split("/", 1)
    src_dir = tmp / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    if kind == "gif":
        paths = cand.get("paths") or [cand.get("path")] or []
        srcs: list[Path] = []
        fnames: list[str] = []
        for p in paths:
            fname = _pic_fname(_raw_url(repo, p))
            _fetch_source(repo, p, src_dir / fname, _box())
            srcs.append(src_dir / fname)
            fnames.append(fname)
        gif_map = _gif_map_from_names(fnames)
        if not gif_map.get("idle"):
            raise ScanError("gif 素材缺 idle 帧（文件名无 idle/walk/run 等关键字）")
        stem = Path(gif_map["idle"]).stem
        meta = {
            "id": _markdown_id(owner, rname, stem),
            "name": f"{rname} 桌宠"[:48],
            "category": "卡通",
            "license": repo["license"],
            "attribution": full,
            "gif_map": gif_map,
        }
        source_type = "gif-multi"
        build_skin("gif-multi", srcs, tmp / "skin", meta)
    elif kind == "pixel":
        fname = _pic_fname(_raw_url(repo, cand["path"]))
        _fetch_source(repo, cand["path"], src_dir / fname, _box())
        data = json.loads(src_dir.joinpath(fname).read_text("utf-8"))
        size = data.get("size")
        frames = data.get("frames")
        palette = data.get("palette")
        if not (isinstance(size, list) and len(size) >= 2 and isinstance(frames, list)
                and frames and isinstance(palette, dict) and palette):
            raise ScanError("像素 JSON 语义不合法（缺 size/palette/frames）")
        stem = Path(cand["path"]).stem
        meta = {
            "id": _markdown_id(owner, rname, stem),
            "name": f"{rname} 像素宠"[:48],
            "category": "像素萌宠",
            "license": repo["license"],
            "attribution": full,
        }
        source_type = "pixel-json"
        build_skin("pixel-json", [src_dir / fname], tmp / "skin", meta)
    else:
        raise ScanError(f"暂不支持的类型 {kind}")

    warns = validate_skin_files(tmp / "skin")
    if warns:
        raise ScanError("试转校验失败: " + "; ".join(warns))

    return {
        "id": meta["id"],
        "name": meta["name"],
        "category": meta["category"],
        "license": meta["license"],
        "attribution": full,
        "description": (repo.get("description") or f"GitHub 扫描发现的 {kind} 素材").strip(),
        "source_type": source_type,
        "bundle": False,
        "source_urls": [_raw_url(repo, p) for p in (cand.get("paths") or [] or [cand.get("path")])],
        "meta": {"gif_map": meta.get("gif_map") or {}} if kind == "gif" else {},
        "discovery": {
            "repo": full, "stars": repo.get("stars", 0), "license": repo["license"],
            "kind": kind, "path": cand.get("path", ""), "count": len(cand.get("paths") or []),
            "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


# ---------------- 扫描主流程 ----------------


def _scan_task() -> None:
    tmp_root = OUT / "pet-scan-tmp"
    try:
        _update(status="running", phase="searching", step="搜索 GitHub 仓库…", error="",
                finished_at="")
        repos = find_repos()
        _update(repos_seen=len(repos), total=len(repos))
        if not repos:
            _update(status="done", phase="done", step="未找到候选仓库")
            return

        for i, repo in enumerate(repos):
            if _SCAN["cancel"]:
                _update(status="cancelled", phase="done", step="已取消")
                return
            _update(current=i + 1, step=f"探测 {repo['full_name']}…")
            if not _licission_ok(repo):
                _update(repos_lic_skip=_SCAN["repos_lic_skip"] + 1)
                continue
            tree = _repo_tree(repo)
            if tree is None:
                _update(repos_tree_skip=_SCAN["repos_tree_skip"] + 1)
                continue
            cands = _pick_candidates(tree)
            gifs = [c for c in cands if c["kind"] == "gif"]
            pixels = [c for c in cands if c["kind"] == "pixel"]
            atlas_n = sum(1 for c in cands if c["kind"] == "atlas")

            # 该仓库全部 gif 合并成 1 个皮肤
            if gifs:
                _update(candidates=_SCAN["candidates"] + 1,
                        step=f"试转 {repo['full_name']} 的 gif 素材…")
                td = tmp_root / f"r{i}"
                try:
                    entry = _try_build("gif", repo, {"paths": [g["path"] for g in gifs]}, td)
                    add_ext_item(entry)
                    _update(built_ok=_SCAN["built_ok"] + 1)
                except Exception as exc:  # noqa: BLE001 - 单个候选失败不阻断扫描
                    _log_fail(repo["full_name"], f"{len(gifs)} 个 gif 合并", exc)
                finally:
                    if td.exists():
                        shutil.rmtree(td, ignore_errors=True)
            for p in pixels:
                if _SCAN["cancel"]:
                    _update(status="cancelled", phase="done", step="已取消")
                    return
                _update(candidates=_SCAN["candidates"] + 1,
                        step=f"试转 {repo['full_name']}:{p['path']}")
                td = tmp_root / f"r{i}p"
                try:
                    entry = _try_build("pixel", repo, p, td)
                    add_ext_item(entry)
                    _update(built_ok=_SCAN["built_ok"] + 1)
                except Exception as exc:  # noqa: BLE001 - 单个候选失败不中断扫描
                    _log_fail(repo["full_name"], p["path"], exc)
                finally:
                    if td.exists():
                        shutil.rmtree(td, ignore_errors=True)
            if atlas_n:
                _update(atlas_skip=_SCAN["atlas_skip"] + atlas_n)
        _update(status="done", phase="done", step="扫描完成",
                finished_at=time.strftime("%H:%M:%S"))
    except ScanError as exc:
        _update(status="failed", phase="done", step="扫描失败", error=str(exc),
                finished_at=time.strftime("%H:%M:%S"))
    except Exception as exc:  # noqa: BLE001 - 扫描线程兜底，不崩进程
        _update(status="failed", phase="done", step="扫描失败", error=f"扫描异常: {exc}",
                finished_at=time.strftime("%H:%M:%S"))
    finally:
        if tmp_root.exists():
            shutil.rmtree(tmp_root, ignore_errors=True)


def start_scan() -> dict:
    """启动后台扫描；同一时间仅一个，进行中重复触发 → 409。"""
    with _SCAN_LOCK:
        if _SCAN["status"] == "running":
            raise ScanError("已有扫描在进行中")
        _SCAN["status"] = "running"
    threading.Thread(target=_scan_task, daemon=True).start()
    return progress_scan()


# ---------------- 发现清单（扫描即上线，读 ext） ----------------


def discovery_items() -> list[dict]:
    """发现 Tab 数据：ext 清单候选 + 已安装标记。"""
    import pet_market
    installed = {d["id"] for d in pet_market.installed_skins()}
    return [{
        "id": it["id"], "name": it["name"], "category": it["category"],
        "license": it["license"], "attribution": it["attribution"],
        "description": it.get("description", ""), "source_type": it.get("source_type"),
        "installed": it["id"] in installed,
        "discovery": it.get("discovery") or {},
    } for it in get_ext_items()]


def remove_discovery(skin_id: str) -> dict:
    """从 ext 清单下线候选（不物理删已安装目录；未安装则无影响）。"""
    if not remove_ext_item(skin_id):
        raise ScanError("候选不存在（可能已下线）")
    return {"removed": skin_id}