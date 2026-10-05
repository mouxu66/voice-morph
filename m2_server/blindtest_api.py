# -*- coding: utf-8 -*-
"""盲听评测的 HTTP 接口（`sound.blindtest` 插件）。

桌面端（本机发起、看结果）：
    GET    /api/blindtest/sessions                会话列表（摘要）
    POST   /api/blindtest/sessions                建会话：multipart 上传 或 outputs 产物引用
    GET    /api/blindtest/sessions/{id}           详情：映射 + 汇总 + 逐条打分
    POST   /api/blindtest/sessions/{id}/close     收卷 / 重开 {closed}
    DELETE /api/blindtest/sessions/{id}           删会话（连样本副本一起删）

手机端（listen.html，token 即凭证，路径里含 token）：
    GET    /api/blindtest/t/{token}               会话形状（顺序样本，**无 label**）
    GET    /api/blindtest/t/{token}/audio/{sid}   样本音频
    POST   /api/blindtest/t/{token}/submit        交卷 {scores:{A:4,...}, note}
    GET    /api/blindtest/t/{token}/results       收卷后的盲汇总（有排名无身份）

★ 业务逻辑都在 `blindtest.py`，本模块只做 HTTP 形状：multipart 解包、临时文件、
  `BlindtestError` → HTTPException。手机端点返回前不再加工 —— 「不漏 label」由
  核心模块的形状函数保证，测试钉死。

★ 为什么手机端点不做单独鉴权：token（128 位随机）就是鉴权；桌面端拼二维码时
  会把 VM_API_TOKEN（若设了）一并拼进链接，server.py 的 TokenMiddleware 照常管
  所有 /api 请求 —— 两层互不掺和。
"""

import json
from pathlib import Path

import blindtest as bt
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from runtime import API_PREFIX

router = APIRouter(prefix=API_PREFIX)


class CloseReq(BaseModel):
    closed: bool


class SubmitReq(BaseModel):
    scores: dict
    note: str = ""


def _err(e: bt.BlindtestError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


def _labels_of(labels_raw: str, n: int) -> list[str]:
    """前端把标签打成 JSON 数组传上来；解析失败当空标签（核心层会给默认值）。"""
    try:
        arr = json.loads(labels_raw or "[]")
        return arr if isinstance(arr, list) else []
    except Exception:  # noqa: BLE001
        return []


def _sample_files(files: list[UploadFile]) -> tuple[list[Path], list[str]]:
    """multipart 上传的样本落临时文件，返回 (路径列表, 用完待清理的路径列表)。"""
    out: list[Path] = []
    tmps: list[Path] = []
    import session_out

    for f in files:
        if not (f.filename or "").strip():
            continue
        data = f.file.read()
        if not data:
            raise HTTPException(400, f"上传样本为空：{f.filename}")
        if len(data) > bt.MAX_SAMPLE_BYTES:
            raise HTTPException(
                413, f"样本过大（>{bt.MAX_SAMPLE_BYTES // 1024 // 1024}MB）：{f.filename}"
            )
        suffix = Path(f.filename).suffix.lower() or ".wav"
        tmp = session_out.new_path("blind_in", suffix)
        tmp.write_bytes(data)
        out.append(tmp)
        tmps.append(tmp)
    return out, tmps


@router.get("/blindtest/sessions")
def blindtest_sessions() -> dict:
    return {"sessions": bt.list_sessions()}


@router.post("/blindtest/sessions")
async def blindtest_create(
    title: str = Form(""),
    labels: str = Form(""),
    refs: str = Form(""),
    files: list[UploadFile] = File(None),
) -> JSONResponse:
    """建盲听会话。样本两种来源可混用：
    - `files`：multipart 上传的音频（评审现场录音、外部文件）；
    - `refs`：outputs/ 相对路径的 JSON 数组（如 ["audition/aud_xxx.wav"]，试音间产物）。
    `labels` 与样本一一对应（主办方视角的名字，手机端永远看不到）。
    """
    try:
        ref_paths = [r for r in (json.loads(refs) if (refs or "").strip() else [])]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"refs 必须是 JSON 数组：{e}") from e
    up, tmps = _sample_files(files or [])
    try:
        try:
            sess = bt.create(
                title,
                _labels_of(labels, len(up) + len(ref_paths)),
                files=up,
                refs=ref_paths,
            )
        except bt.BlindtestError as e:
            raise _err(e) from e
    finally:
        for t in tmps:
            try:
                t.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    return JSONResponse({"ok": True, "id": sess["id"], "token": sess["token"]})


@router.get("/blindtest/sessions/{session_id}")
def blindtest_detail(session_id: str) -> dict:
    try:
        return bt.detail(session_id)
    except bt.BlindtestError as e:
        raise _err(e) from e


@router.post("/blindtest/sessions/{session_id}/close")
def blindtest_close(session_id: str, req: CloseReq) -> dict:
    try:
        return bt.set_closed(session_id, req.closed)
    except bt.BlindtestError as e:
        raise _err(e) from e


@router.delete("/blindtest/sessions/{session_id}")
def blindtest_delete(session_id: str) -> dict:
    try:
        return bt.delete(session_id)
    except bt.BlindtestError as e:
        raise _err(e) from e


# ---------------------------------------------------------------- 手机端


@router.get("/blindtest/t/{token}")
def blindtest_phone_session(token: str) -> dict:
    try:
        return bt.phone_session(token)
    except bt.BlindtestError as e:
        raise _err(e) from e


@router.get("/blindtest/t/{token}/audio/{sid}")
def blindtest_phone_audio(token: str, sid: str) -> FileResponse:
    try:
        p = bt.audio_path(token, sid)
    except bt.BlindtestError as e:
        raise _err(e) from e
    return FileResponse(str(p), media_type=bt.audio_mime(p))


@router.post("/blindtest/t/{token}/submit")
def blindtest_phone_submit(token: str, req: SubmitReq, request: Request) -> dict:
    try:
        return bt.submit(
            token,
            req.scores,
            req.note,
            ua=request.headers.get("user-agent", ""),
        )
    except bt.BlindtestError as e:
        raise _err(e) from e


@router.get("/blindtest/t/{token}/results")
def blindtest_phone_results(token: str) -> dict:
    try:
        return bt.blind_overview(token)
    except bt.BlindtestError as e:
        raise _err(e) from e
