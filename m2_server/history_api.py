"""变声任务历史查询/删除端点（FRD F3）。"""
from fastapi import APIRouter, HTTPException

from history import delete as _delete
from history import query as _query

router = APIRouter(prefix="/api")


@router.get("/history")
def history_list(kind: str | None = None, voice_id: str | None = None,
                 from_ts: int | None = None, to_ts: int | None = None,
                 limit: int = 50, offset: int = 0):
    """查询产出历史：?kind=&voice_id=&from=&to=&limit=&offset=，ts 倒序分页。"""
    if limit <= 0 or limit > 100:
        raise HTTPException(status_code=400, detail="limit 须在 1~100 之间")
    return _query(kind=kind, voice_id=voice_id, from_ts=from_ts,
                  to_ts=to_ts, limit=limit, offset=offset)


@router.delete("/history/{item_id}")
def history_delete(item_id: str, keep_file: bool = False):
    """删除一条历史记录；keep_file=1 只删记录、保留 wav 文件。"""
    res = _delete(item_id, keep_file=keep_file)
    if not res["ok"]:
        raise HTTPException(status_code=404, detail=res["error"])
    return res
