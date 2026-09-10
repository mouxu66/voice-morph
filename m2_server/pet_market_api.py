"""桌面人偶市场 —— API（清单 / 搜索 / 详情 / 安装队列 / 应用 / 卸载 / 预览）。

完整链路：
  - GET    /api/pet-market/manifest    内置清单（皮肤 id / 分类 / 许可 / 下载源）
  - GET    /api/pet-market/search      本地模糊搜索 + 分类过滤（带 installed/applied）
  - GET    /api/pet-market/detail/{id} 皮肤详情（帧尺寸 / 状态表 / 许可全文 / 源链接）
  - GET    /api/pet-market/installed   已安装皮肤列表（含是否当前应用）
  - GET    /api/pet-market/progress    安装任务队列轮询（{items, active, queued}）
  - GET    /api/pet-market/image/{name} 预览图（<skin_id> 或 <skin_id>.png）
  - POST   /api/pet-market/install     加入安装队列（≤2 并发，其余排队）
  - POST   /api/pet-market/cancel      取消排队/进行中的安装任务
  - POST   /api/pet-market/apply       应用皮肤（切换桌宠外观）
  - DELETE /api/pet-market/uninstall   卸载皮肤（内置 bundle 仅复位应用态）

安全：皮肤 id 白名单校验；下载域名白名单见 pet_market.py。
"""
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from runtime import API_PREFIX
import pet_market
import pet_scan

router = APIRouter(prefix=API_PREFIX)

_IMG_EXT_RE = re.compile(r"\.(png|jpe?g|webp)$")


class SkinRequest(BaseModel):
    skin_id: str = Field(..., description="皮肤 id（限字母数字_- ≤32）")


class CancelRequest(BaseModel):
    skin_id: str = Field(..., description="要取消的安装任务皮肤 id")


def _to_http(exc: pet_market.PetMarketError) -> HTTPException:
    """把业务错误映射为 HTTP 状态码（409 冲突 / 404 不存在 / 400 非法）。"""
    msg = str(exc)
    if "未安装" in msg or "无此皮肤" in msg or "资源缺失" in msg or "无此安装任务" in msg:
        return HTTPException(status_code=404, detail=msg)
    if "进行中" in msg or "任务中" in msg or "无法取消" in msg:
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=400, detail=msg)


@router.get("/pet-market/manifest")
def pet_manifest():
    """内置清单（前端「推荐」Tab 数据源）。"""
    items = pet_market.get_manifest()
    return {"items": items, "default": pet_market.DEFAULT_SKIN}


@router.get("/pet-market/search")
def pet_search(q: str = "", cat: str = ""):
    """本地模糊搜索 + 分类过滤（命中 id/名称/描述/分类/作者/许可）。"""
    return {"items": pet_market.search(q, cat)}


@router.get("/pet-market/detail/{skin_id}")
def pet_detail(skin_id: str):
    """皮肤详情：清单信息 + 源链接 + 已装/应用状态 + 帧尺寸 + 状态动画表 + 许可全文。"""
    try:
        return pet_market.detail(skin_id)
    except pet_market.PetMarketError as exc:
        raise _to_http(exc) from exc


@router.get("/pet-market/installed")
def pet_installed():
    """已安装皮肤 + 当前应用标记。"""
    return {"items": pet_market.installed_skins()}


@router.get("/pet-market/applied")
def pet_applied():
    """当前应用皮肤配置（渲染器换肤用）：{id, frameW, frameH, states}。"""
    return pet_market.applied_skin()


@router.get("/pet-market/sheet/{skin_id}/{name}")
def pet_sheet(skin_id: str, name: str):
    """皮肤 spritesheet 文件（name 限该皮肤 skin.json 状态表内的 sheet，防枚举/穿越）。"""
    try:
        p = pet_market.sheet_file(skin_id, name)
    except pet_market.PetMarketError as exc:
        raise _to_http(exc) from exc
    return FileResponse(p, media_type="image/webp")


@router.get("/pet-market/progress")
def pet_progress():
    """安装进度（无任务时 status=idle）。"""
    return pet_market.progress()


@router.get("/pet-market/image/{name}")
def pet_image(name: str):
    """皮肤预览图：<skin_id> 或 <skin_id>.png（未生成时 404）。"""
    stem = _IMG_EXT_RE.sub("", name)
    try:
        d = pet_market.skin_dir(stem)
    except pet_market.PetMarketError as exc:
        raise _to_http(exc) from exc
    for cand in (d / "preview.png", d / "preview.webp", d / "preview.jpg"):
        if cand.exists():
            return FileResponse(cand)
    raise HTTPException(status_code=404, detail="preview not found")


@router.post("/pet-market/scan")
def pet_scan_start():
    """启动 GitHub 扫描器（后台线程；同一时间仅一个，进行中触发 → 409）。"""
    try:
        return pet_scan.start_scan()
    except pet_scan.ScanError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/pet-market/scan/progress")
def pet_scan_progress():
    """扫描进度轮询（阶段/仓库进度/候选统计/错误/取消标记）。"""
    return pet_scan.progress_scan()


@router.post("/pet-market/scan/cancel")
def pet_scan_cancel():
    """请求取消扫描（正在处理的候选跑完即停）。"""
    try:
        return pet_scan.cancel_scan()
    except pet_scan.ScanError as exc:
        if "没有运行中" in str(exc):
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/pet-market/discovery")
def pet_discovery():
    """发现 Tab：扫描器「扫描即上线」的候选皮肤（附已安装标记）。"""
    return {"items": pet_scan.discovery_items()}


@router.delete("/pet-market/discovery/{skin_id}")
def pet_discovery_remove(skin_id: str):
    """下线候选：从 ext 清单移除（不物理删已安装目录）。"""
    try:
        return pet_scan.remove_discovery(skin_id)
    except pet_scan.ScanError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/pet-market/install")
def pet_install(req: SkinRequest):
    """加入安装队列（≤2 并发下载/转换，其余排队；进度走 /pet-market/progress 轮询）。"""
    try:
        return pet_market.install(req.skin_id)
    except pet_market.PetMarketError as exc:
        raise _to_http(exc) from exc


@router.post("/pet-market/cancel")
def pet_cancel(req: CancelRequest):
    """取消排队/进行中的安装任务。"""
    try:
        return pet_market.cancel(req.skin_id)
    except pet_market.PetMarketError as exc:
        raise _to_http(exc) from exc


@router.post("/pet-market/apply")
def pet_apply(req: SkinRequest):
    """应用皮肤；成功后桌宠窗口通过轮询 /pet-market/installed 或推送换肤。"""
    try:
        return pet_market.apply(req.skin_id)
    except pet_market.PetMarketError as exc:
        raise _to_http(exc) from exc


@router.delete("/pet-market/uninstall")
def pet_uninstall(req: SkinRequest):
    """卸载皮肤（内置 bundle 不物理删除，仅复位应用态）。"""
    try:
        return pet_market.uninstall(req.skin_id)
    except pet_market.PetMarketError as exc:
        raise _to_http(exc) from exc