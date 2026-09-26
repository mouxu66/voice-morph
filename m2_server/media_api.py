"""静态音频访问接口：clips / outputs / voicebank 三类产物 + 会话产物的保存/清空。

自 server.py 拆出（行为不变）；app 装配见 server.py。

为什么 `session/save` 与 `session/purge` 挂在**这里**（2026-09-25）：会话目录
（`outputs/.session/`，见 `session_out.py`）是这套媒体目录的一部分，`media()` 的
播放白名单本来就涵盖它；而本模块属于 `core.media` —— **核心插件、永远挂载**。
Electron 退出前要发 purge 请求，那条链路不该因为用户关掉了某个可选能力就 404。
"""

import config as cfg
import session_out
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from runtime import API_PREFIX, OUT

router = APIRouter(prefix=API_PREFIX)

#: 按后缀给内容类型。以前一律 `audio/wav` —— 会话里的产物确实都是 wav，所以没暴露；
#: 但「粘直链」下下来的源是 mp3/m4a/flac（浏览器靠嗅探才放，有些直接拒），
#: 而试听走的就是这个端点。不认识的后缀保持旧行为（audio/wav），零回归。
_MEDIA_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".m4b": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".webm": "audio/webm",
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
}


@router.get("/media/{kind}/{name:path}")
def media(kind: str, name: str):
    """静态音频访问。clips/voicebank 在 media/ 下，outputs 在项目根下。
    voicebank 的参考音频在 <voice_id>/reference.wav，故 name 允许多层路径。"""
    if kind not in ("clips", "outputs", "voicebank"):
        raise HTTPException(404, "invalid kind")
    base = (OUT if kind == "outputs" else cfg.MEDIA_DIR / kind).resolve()
    p = (base / name).resolve()
    # 防止 ../ 之类的路径穿越（is_relative_to 精确判断层级归属）
    if not p.is_relative_to(base) or not p.is_file():
        raise HTTPException(404, f"{kind}/{name} 不存在")
    return FileResponse(str(p), media_type=_MEDIA_TYPES.get(p.suffix.lower(), "audio/wav"))


class SaveReq(BaseModel):
    wav: str
    kind: str = "tts"
    voice_id: str = ""
    input_text: str = ""


@router.post("/session/save")
def session_save(req: SaveReq):
    """把一条会话产物存进作品库（复制到 outputs 根 + 登记历史）。幂等。

    这是"默认不落盘"里**唯一**让文件留下来的入口 —— 除了用户点这个按钮，后端没有
    任何地方会自动把会话产物搬进作品库（`history.register` 的调用点已全部移除或
    改到保存这一步）。
    """
    res = session_out.save(
        req.wav, kind=req.kind, voice_id=req.voice_id, input_text=req.input_text
    )
    if not res.get("ok"):
        raise HTTPException(400 if "非法" in str(res.get("error")) else 404, res.get("error"))
    return res


@router.post("/session/purge")
def session_purge():
    """清空会话目录（退出即删）。Electron `before-quit` 与后端启动都会打它。"""
    return {"ok": True, "removed": session_out.purge()}

