"""桌面人偶市场 —— API（清单 / 安装 / 应用 / 卸载 / 预览）。

完整链路：
  - GET    /api/pet-market/manifest    内置清单（皮肤 id / 分类 / 许可 / 下载源）
  - GET    /api/pet-market/installed   已安装皮肤列表（含是否当前应用）
  - GET    /api/pet-market/progress    安装进度轮询（下载中/生成中/完成/失败）
  - GET    /api/pet-market/image/{name} 预览图（<skin_id> 或 <skin_id>.png）
  - POST   /api/pet-market/install     安装皮肤（下载源→转换→物化）
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

router = APIRouter(prefix=API_PREFIX)

_IMG_EXT_RE = re.compile(r"\.(png|jpe?g|webp)$")


class SkinRequest(BaseModel):
    skin_id: str = Field(..., description="皮肤 id（限字母数字_- ≤32）")


def _to_http(exc: pet_market.PetMarketError) -> HTTPException:
    """把业务错误映射为 HTTP 状态码（409 冲突 / 404 不存在 / 400 非法）。"""
    msg = str(exc)
    if "进行中" in msg:
        return HTTPException(status_code=409, detail=msg)
    if "未安装" in msg or "无此皮肤" in msg or "资源缺失" in msg:
        return HTTPException(status_code=404, detail=msg)
    return HTTPException(status_code=400, detail=msg)


@router.get("/pet-market/manifest")
def pet_manifest():
    """内置清单（前端「推荐」Tab 数据源）。"""
    items = pet_market.get_manifest()
    return {"items": items, "default": pet_market.DEFAULT_SKIN}


@router.get("/pet-market/installed")
def pet_installed():
    """已安装皮肤 + 当前应用标记。"""
    return {"items": pet_market.installed_skins()}


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


@router.post("/pet-market/install")
def pet_install(req: SkinRequest):
    """启动安装（异步；进度走 /pet-market/progress 轮询）。"""
    try:
        return pet_market.install(req.skin_id)
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