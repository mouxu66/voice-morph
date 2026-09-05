"""音色市场 —— API（精选清单 / 双源搜索 / 仓库浏览 / 下载安装）。

覆盖应用市场完整链路：
  - GET  /api/market/manifest    内置精选清单（推荐 Tab）
  - GET  /api/market/search      双源搜索（hf / modelscope / all）
  - GET  /api/market/repo        仓库文件列表（选文件 / 看详情）
  - POST /api/market/download    断点续传下载（单文件）
  - GET  /api/market/progress    下载 / 安装进度轮询
  - POST /api/market/install     一键安装到 RVC 音色库（.pth + 可选 .index）
  - GET  /api/market/installed   已安装音色 id 列表（前端标"已装"角标）
  - POST /api/market/cancel      取消当前下载 / 安装
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from runtime import API_PREFIX
from market_download import get_manager, MarketError
from market_install import get_installer, InstallError
from market_manifest import get_manifest, find_manifest_item
import market_preview
from market_search import search, repo_files_hf, repo_files_ms
from market_search import hf_resolve, ms_resolve
from market_search import readme_summary
from market_search import HF_API as HF_BASE

router = APIRouter(prefix=API_PREFIX)


class DownloadRequest(BaseModel):
    name: str = Field(..., description="任务名/权重名（不含扩展名与路径分隔符）")
    url: str = Field(..., description="主下载直链（域名须在白名单内）")
    mirror_url: str | None = Field(None, description="可选镜像直链（同文件的另一通道，主源失败自动回退）")
    sha256: str | None = Field(None, description="可选 SHA256（提供则下载完成后强校验）")
    expected_size: int | None = Field(None, description="可选期望字节数")


class FileSlot(BaseModel):
    url: str = Field(..., description="文件直链（域名须在白名单内）")
    mirror_url: str | None = Field(None, description="镜像直链")
    sha256: str | None = Field(None, description="SHA256（可选强校验）")


class InstallRequest(BaseModel):
    voice_id: str = Field(..., description="RVC 音色 ID（限字母数字_-，≤64）")
    download: FileSlot = Field(..., description="权重文件（.pth 或含 pth 的 zip）")
    index: FileSlot | None = Field(None, description="可选索引文件（.index）")
    display_name: str = Field("", description="中文展示名（空则用 voice_id）")
    manifest_id: str = Field("", description="来源清单条目 id（安装溯源）")
    overwrite: bool = Field(False, description="音色已存在时是否显式覆盖重装（默认拒绝）")


class PreviewRequest(BaseModel):
    voice_id: str = Field(..., description="要生成试听的市场音色 ID")


@router.get("/market/manifest")
def market_manifest():
    """内置精选清单（含每条的 download/index 直链与镜像）。"""
    return {"items": get_manifest()}


@router.get("/market/search")
def market_search(q: str = "", platform: str = "all", limit: int = 10):
    """双源搜索：platform ∈ hf / modelscope / all；魔搭源返回降级说明。"""
    query = q.strip()
    if not query:
        raise HTTPException(400, "缺少搜索关键词 q")
    data = search(platform, query, limit)
    return data


@router.get("/market/repo")
def market_repo(repo: str = "", platform: str = "hf", recursive: bool = False):
    """仓库文件列表（hf / modelscope）。blob 附直链，便于前端选文件安装。"""
    repo = repo.strip()
    if not repo:
        raise HTTPException(400, "缺少仓库名 repo")
    try:
        if platform == "modelscope":
            files = repo_files_ms(repo, _only=False, recursive=recursive)
            # 非 only 模式统一补按钮直链（目录除外）
            for f in files:
                if f.get("type") == "blob" and not f.get("url"):
                    f["url"] = ms_resolve(repo, f["path"])
        else:
            files = repo_files_hf(repo, recursive=recursive)
            for f in files:
                if f.get("type") == "blob":
                    f["url"] = hf_resolve(repo, f["path"], HF_BASE)
                    f["mirror_url"] = hf_resolve(repo, f["path"], "https://huggingface.co")
    except MarketError as exc:
        raise HTTPException(404, f"仓库不可用: {exc}")
    return {"repo": repo, "platform": platform, "files": files,
            "readme": readme_summary(repo, platform)}


@router.post("/market/install")
def market_install(req: InstallRequest):
    """一键安装音色到 RVC 音色库：串行下载 pth → 可选 index → 落位 logs/assets。"""
    dl = {"url": req.download.url, "mirror_url": req.download.mirror_url,
          "sha256": req.download.sha256}
    idx = {"url": req.index.url, "mirror_url": req.index.mirror_url} if req.index else None
    try:
        st = get_installer().run(
            voice_id=req.voice_id, download=dl, index=idx,
            display_name=req.display_name, manifest_id=req.manifest_id,
            overwrite=req.overwrite,
        )
    except InstallError as exc:
        raise HTTPException(409, str(exc))
    return {"task": st}


@router.get("/market/installed")
def market_installed():
    """已安装到 RVC 音色库的音色 id 列表。"""
    return {"installed": get_installer().installed_ids()}


class UninstallRequest(BaseModel):
    voice_id: str = Field(..., description="要卸载的音色 ID（须为市场安装来源）")


@router.delete("/market/uninstall")
def market_uninstall(req: UninstallRequest):
    """卸载市场安装的音色：清 logs/<id>/ + assets/weights/<id>.pth + 市场缓存。

    仅允许卸载带 source.json 市场标记的音色（防止误删自训产物）；
    安装/下载进行中返回 409。
    """
    try:
        return get_installer().uninstall(req.voice_id)
    except InstallError as exc:
        raise HTTPException(409, str(exc))


@router.get("/market/progress")
def market_progress():
    """当前下载 / 安装进度（无任务时返回 null）。"""
    st = get_installer().progress()
    if (st.get("idle") or not st.get("name")) and not st.get("install"):
        return {"task": None}
    return {"task": st}


@router.get("/market/preview")
def market_preview_status(voice_id: str = ""):
    """市场音色试听状态（ready/generating/failed/skipped/missing）+ 可播放 url。"""
    voice_id = voice_id.strip()
    if not voice_id:
        raise HTTPException(400, "缺少 voice_id")
    return market_preview.status(voice_id)


@router.post("/market/preview")
def market_preview_trigger(req: PreviewRequest):
    """触发市场音色试听生成（后台线程，同一音色去重；GPU 忙返回 skipped）。"""
    return market_preview.generate(req.voice_id.strip())


@router.post("/market/download")
def market_download(req: DownloadRequest):
    """启动（或续传）下载任务；已有任务在跑返回 409。"""
    try:
        st = get_manager().start(
            name=req.name, url=req.url, mirror_url=req.mirror_url,
            sha256=req.sha256, expected_size=req.expected_size,
        )
    except MarketError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"task": st}


@router.post("/market/cancel")
def market_cancel():
    """取消当前下载 / 安装（删除 .part，允许开启新任务）。"""
    try:
        st = get_installer().cancel()
    except (InstallError, MarketError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"task": st or None}
