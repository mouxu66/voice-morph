# -*- coding: utf-8 -*-
"""音频溯源水印的 HTTP 接口（`sound.watermark` 插件）。

    GET    /api/watermark/status    权重在位 / 下载进度 / 自动加标识开关
    POST   /api/watermark/download  下载 AudioSeal 权重（约 90MB，幂等；在下的重复请求不炸）
    POST   /api/watermark/embed     植入水印：multipart 文件（或 name=会话/产物裸名）→ 溯源码
    POST   /api/watermark/detect    检出水印：multipart 文件（或 name）→ 是否带码 / 置信度 / 码
    GET    /api/watermark/auto      「合成自动加标识」开关状态
    POST   /api/watermark/auto      设开关 {enabled, code?}（默认关；码 4 位 hex，可留空）

★ 业务逻辑都在 `audio_watermark.py`，本模块只做 HTTP 形状：
  上传落盘、`WatermarkError` → HTTPException、产物 URL 组装（`session_out.rel_url`）。

★ 上传大小：水印页面向是**单条音频**（合成产物 / 分享件），200MB 上限已远超场景
  （500MB 那档是给整段录音上传用的，见 voicepreserve_api）。
"""

from pathlib import Path

import audio_watermark as wm
import session_out
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from runtime import API_PREFIX

router = APIRouter(prefix=API_PREFIX)

MAX_UPLOAD_BYTES = 200 * 1024 * 1024


class AutoReq(BaseModel):
    enabled: bool
    code: str = ""


def _err(e: wm.WatermarkError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=str(e))


def _media_url(name: str) -> str:
    return f"/api/media/outputs/{session_out.rel_url(name)}"


async def _input_audio(
    file: UploadFile | None, name: str, what: str
) -> Path:
    """统一取输入：multipart 文件优先；否则 name=会话/产物裸名（session_out.find）。"""
    if file is not None and (file.filename or "").strip():
        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"音频超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限")
        if not data:
            raise HTTPException(400, "上传的音频是空的")
        import mimetypes

        suffix = Path(file.filename or "").suffix or ".wav"
        if not suffix.lower() in (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".webm"):
            guessed = mimetypes.guess_extension(file.content_type or "") or ".wav"
            suffix = suffix if suffix else guessed
        tmp = session_out.new_path("wm_in", suffix)
        tmp.write_bytes(data)
        return tmp
    bare = session_out.find(name or "")
    if bare is None:
        raise HTTPException(400, f"找不到音频：{name or '(空)'} —— 传文件，或给一条已合成产物的名字")
    return bare


@router.get("/watermark/status")
def watermark_status() -> dict:
    return wm.status()


@router.post("/watermark/download")
def watermark_download() -> dict:
    """下载权重。已装好 → 直接回 done（幂等）；在下 → 回 downloading，前端轮询 status。"""
    try:
        return wm.start_download()
    except wm.WatermarkError as e:
        raise _err(e) from e


@router.post("/watermark/embed")
async def watermark_embed(
    file: UploadFile | None = File(None),
    name: str = Form(""),
    code: str = Form(""),
    alpha: float = Form(1.0),
) -> JSONResponse:
    """植入水印 → 会话产物（默认不落盘，退出即删），返回可播放/下载的 URL 与所用溯源码。"""
    src = await _input_audio(file, name, "植入")
    dst = session_out.new_path("wm_")
    try:
        info = wm.embed_file(src, dst, code=code, alpha=max(0.1, min(alpha, 2.0)))
    except wm.WatermarkError as e:
        raise _err(e) from e
    except Exception as e:  # 模型推理层的意外错误也要给个像样的 500 而不是连接重置
        raise HTTPException(500, f"植入失败：{e}") from e
    return JSONResponse(
        {
            "ok": True,
            "code": info["code"],
            "sr": info["sr"],
            "duration_s": info["seconds"],
            "wav": dst.name,
            "url": _media_url(dst.name),
        }
    )


@router.post("/watermark/detect")
async def watermark_detect(
    file: UploadFile | None = File(None),
    name: str = Form(""),
) -> JSONResponse:
    """检出：返回 watermarked / prob / code。口径：可检出、可溯源 —— 不是防伪。"""
    src = await _input_audio(file, name, "检测")
    try:
        res = wm.detect_file(src)
    except wm.WatermarkError as e:
        raise _err(e) from e
    except Exception as e:
        raise HTTPException(500, f"检测失败：{e}") from e
    return JSONResponse({"ok": True, **res})


@router.get("/watermark/auto")
def watermark_auto_get() -> dict:
    return {"enabled": wm.auto_enabled(), "code": wm.auto_code() if wm.auto_enabled() else ""}


@router.post("/watermark/auto")
def watermark_auto_set(req: AutoReq) -> dict:
    try:
        payload = wm.set_auto(req.enabled, req.code)
    except wm.WatermarkError as e:
        raise _err(e) from e
    return {"ok": True, **payload}
