"""静态音频访问接口：clips / outputs / voicebank 三类产物。

自 server.py 拆出（行为不变）；app 装配见 server.py。
"""

import config as cfg
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from runtime import API_PREFIX, OUT

router = APIRouter(prefix=API_PREFIX)


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
    return FileResponse(str(p), media_type="audio/wav")
