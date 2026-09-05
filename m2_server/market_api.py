"""音色市场 —— 下载 API（断点续传 / 进度查询 / 取消）。

本轮只做下载链路（断点续传 + 进度落盘 + 镜像回退）；搜索、清单、
安装注册在后续迭代接入。前端通过 /api/market/progress 轮询进度。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from runtime import API_PREFIX
from market_download import get_manager, MarketError

router = APIRouter(prefix=API_PREFIX)


class DownloadRequest(BaseModel):
    name: str = Field(..., description="任务名/权重名（不含扩展名与路径分隔符）")
    url: str = Field(..., description="主下载直链（域名须在白名单内）")
    mirror_url: str | None = Field(None, description="可选镜像直链（同文件的另一通道，主源失败自动回退）")
    sha256: str | None = Field(None, description="可选 SHA256（提供则下载完成后强校验）")
    expected_size: int | None = Field(None, description="可选期望字节数")


@router.get("/market/progress")
def market_progress():
    """当前下载任务进度（无任务时返回 null）。"""
    st = get_manager().progress()
    if st.get("idle") or not st.get("name"):
        return {"task": None}
    return {"task": st}


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
    """取消当前下载（删除 .part，允许开启新任务）。"""
    try:
        st = get_manager().cancel()
    except MarketError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"task": st or None}