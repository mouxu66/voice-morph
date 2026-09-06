"""变声任务历史（作品库）端点（FRD F3 + B1 收藏/标签/批量导出）。

    GET    /api/history              列表（kind/voice_id/时间/收藏/标签 过滤 + 分页）
    GET    /api/history/tags         全部标签及使用次数
    PATCH  /api/history/{item_id}    改收藏 / 改标签
    POST   /api/history/bulk_delete  批量删除
    POST   /api/history/export       勾选导出 zip（POST 便于传长 id 列表）
    DELETE /api/history/{item_id}    删单条
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from history import all_tags as _all_tags
from history import bulk_delete as _bulk_delete
from history import delete as _delete
from history import export_zip as _export_zip
from history import query as _query
from history import set_meta as _set_meta

router = APIRouter(prefix="/api")


class MetaPatch(BaseModel):
    starred: bool | None = Field(None, description="收藏开关（None=不改）")
    tags: list[str] | None = Field(None, description="标签全集（None=不改；传 [] 表示清空）")


class BulkDeleteRequest(BaseModel):
    ids: list[str] = Field(..., description="要删除的记录 id 列表")
    keep_file: bool = Field(False, description="true=只删记录、保留 wav")


class ExportRequest(BaseModel):
    ids: list[str] = Field(..., description="要打包的记录 id 列表")


@router.get("/history/tags")
def history_tags():
    """全部标签 + 使用次数（次数降序）。"""
    return {"tags": _all_tags()}


@router.get("/history")
def history_list(kind: str | None = None, voice_id: str | None = None,
                 from_ts: int | None = None, to_ts: int | None = None,
                 limit: int = 50, offset: int = 0,
                 starred: bool | None = None, tag: str | None = None):
    """查询产出历史：?kind=&voice_id=&from=&to=&starred=&tag=&limit=&offset=，ts 倒序分页。"""
    if limit <= 0 or limit > 100:
        raise HTTPException(status_code=400, detail="limit 须在 1~100 之间")
    return _query(kind=kind, voice_id=voice_id, from_ts=from_ts,
                  to_ts=to_ts, limit=limit, offset=offset,
                  starred=starred, tag=tag)


@router.patch("/history/{item_id}")
def history_patch(item_id: str, body: MetaPatch):
    """改一条记录的收藏 / 标签；两个字段都省略时返回 400。"""
    if body.starred is None and body.tags is None:
        raise HTTPException(status_code=400, detail="至少提供 starred 或 tags")
    rec = _set_meta(item_id, starred=body.starred, tags=body.tags)
    if not rec:
        raise HTTPException(status_code=404, detail="记录不存在")
    if rec.get("error"):
        raise HTTPException(status_code=500, detail=rec["error"])
    return {"ok": True, "item": rec}


@router.post("/history/bulk_delete")
def history_bulk_delete(req: BulkDeleteRequest):
    """批量删除；单条失败不中断，返回失败明细。"""
    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    return _bulk_delete(req.ids, keep_file=req.keep_file)


@router.post("/history/export")
def history_export(req: ExportRequest):
    """把勾选记录的 wav 打包成 zip 下载；文件已不在磁盘的跳过并在头部提示。"""
    from fastapi.responses import Response

    if not req.ids:
        raise HTTPException(status_code=400, detail="ids 不能为空")
    data, picked, missing = _export_zip(req.ids)
    if not picked:
        raise HTTPException(status_code=404, detail="所选产物在磁盘上都不存在了")
    headers = {"Content-Disposition": 'attachment; filename="works.zip"'}
    if missing:
        headers["X-Missing-Files"] = str(len(missing))
    return Response(content=data, media_type="application/zip", headers=headers)


@router.delete("/history/{item_id}")
def history_delete(item_id: str, keep_file: bool = False):
    """删除一条历史记录；keep_file=1 只删记录、保留 wav 文件。"""
    res = _delete(item_id, keep_file=keep_file)
    if not res["ok"]:
        raise HTTPException(status_code=404, detail=res["error"])
    return res
