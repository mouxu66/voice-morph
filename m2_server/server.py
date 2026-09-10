"""M2 转换服务（FastAPI · 常驻服务）—— 应用装配层。

启动：
    python m2_server/server.py
    默认监听 8000 端口。

本文件只负责：创建 app、可选 Token 鉴权中间件、CORS、注册全部路由、
托管前端静态资源。业务实现按域拆在同目录各模块（行为与拆分前一致）：

    system_api.py        /health /diagnose /system/storage（占用看板+清理）
    voices_api.py        /voices /voicebank（建库/删除/音色包导出导入）
    raw_media_api.py     /raw_videos /upload/video /open/folder
    pipeline_api.py      /pipeline/*（提轨→去BGM→切片，进度轮询）
    clips_api.py         /clips /clips/diarize /clips/qc /export/rvc
    tts_api.py           /tts（Qwen3-TTS 文字→语音）
    mine_api.py          /mine/*（音色挖掘/试听/保存）
    capture_api.py       /capture/loopback（桌宠内录）
    ab_api.py            /ab/run（盲听对比）
    ab_chain.py          /ab/chain（多链路评测：RVC/Seed-VC/Qwen3 + 客观分）
    audio_api.py         /audio/*（设备配置/诊断看板/残留巡检）
    media_api.py         /media/{kind}/{name}（静态音频）
    rvc_dataset_api.py   /rvc/dataset/* /rvc/model
    qwen3_tts.py         Qwen3-TTS 客户端（懒启动子进程 worker）
    qwen3_tts_service.py Qwen3-TTS 常驻 worker（venv312，端口 8001）

共享运行状态（流水线进度/挖掘状态/内录状态/路径常量）在 runtime.py。
"""
import re
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import config as cfg
from ab_api import router as ab_router
from ab_chain import router as ab_chain_router
from audio_api import _start_audio_audit, router as audio_router
from audiobook import router as audiobook_router
from capture_api import router as capture_router
from cascade import router as cascade_router
from clips_api import router as clips_router
from effects import router as effects_router
from finetune import router as ft_router
from history_api import router as history_router
from media_api import router as media_router
from mine_api import router as mine_router
from market_api import router as market_router
from offline_vc import router as offlinevc_router
from pipeline_api import router as pipeline_router
from raw_media_api import router as raw_media_router
from rvc_dataset_api import router as rvc_dataset_router
from rvc_live import router as rvc_live_router
from seed_vc import router as seedvc_router
from system_api import router as system_router
from tts_api import router as tts_router
from voices_api import router as voices_router
from wechat_voice import router as wechat_router
from runtime import ROOT

app = FastAPI(title="变声 · M2 转换服务", version="0.1.0")

# 可选 Token 鉴权：仅当配置了 VM_API_TOKEN 时启用，否则完全不拦截（LAN-only 默认）。
# 设计要点：
# - 本机回环(127.0.0.1/::1)永远放行：PC 前端(Electron/vite proxy)、桌宠、全局热键、
#   子进程都不带 token，启用鉴权不能破坏本机任何链路
# - 局域网请求需通过以下任一方式：X-API-Key 头 / Authorization: Bearer / api_key 查询参数
#   （移动端原生音频播放器请求 URL 时带不了自定义 header，所以必须支持查询参数）
if cfg.API_TOKEN:
    from fastapi.responses import JSONResponse
    from starlette.middleware.base import BaseHTTPMiddleware

    class _TokenMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.method == "OPTIONS":
                return await call_next(request)
            client = request.client.host if request.client else ""
            if client in ("127.0.0.1", "::1", "localhost"):
                return await call_next(request)
            token = cfg.API_TOKEN
            ok = (
                request.headers.get("X-API-Key", "") == token
                or request.headers.get("Authorization", "") == f"Bearer {token}"
                or request.query_params.get("api_key", "") == token
            )
            if not ok:
                return JSONResponse(status_code=401, content={"detail": "unauthorized（需 X-API-Key 头或 api_key 参数）"})
            return await call_next(request)

    app.add_middleware(_TokenMiddleware)
else:
    # 安全告警：0.0.0.0 监听且未设 VM_API_TOKEN，局域网内任意设备可调用全部接口
    # （含 /tts 吃显存、/rvc/live/* 切系统声卡）。暴露到 LAN 前务必设置 VM_API_TOKEN 并收紧 CORS。
    if cfg.SERVER_HOST in ("0.0.0.0", ""):
        import logging
        logging.warning(
            "安全告警: 服务以 0.0.0.0 监听且未设置 VM_API_TOKEN，局域网内任意设备可调用所有接口"
            "（含 /tts 与 /rvc/live）。生产/暴露到 LAN 前请设置 VM_API_TOKEN 并收紧 VM_CORS_ORIGINS。"
        )

# CORS / 跨站守卫
# ---------------------------------------------------------------
# 默认（未显式配置 VM_CORS_ORIGINS）仅放行本机来源：file:// 页面（Electron /
# 浏览器打开 dist，Origin 为 null 或 file://）、开发服务器 localhost:*、
# 以及无 Origin 头的本机调用（curl / RN 原生 / 测试）。任意远程网页
# （https://evil.com）的 fetch 请求会被 _OriginGuardMiddleware 直接 403，
# 杜绝"无鉴权 + CORS 通配"下被跨站调用破坏性接口（DELETE 音色 / 触发下载 /
# /tts 占显存）。显式配置 VM_CORS_ORIGINS 时走用户白名单并跳过守卫。
LOCAL_ORIGIN_RE = re.compile(
    r"^(?:null|file://|https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?)$"
)


class _OriginGuardMiddleware(BaseHTTPMiddleware):
    """默认模式下，拒绝一切非本机来源的跨站调用（预检留给 CORS 层）。"""

    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        origin = request.headers.get("Origin", "")
        if origin and not LOCAL_ORIGIN_RE.match(origin):
            return JSONResponse(status_code=403,
                                content={"detail": f"拒绝跨站来源: {origin}"})
        return await call_next(request)


if cfg.CORS_ORIGINS == ["*"]:
    app.add_middleware(_OriginGuardMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=LOCAL_ORIGIN_RE.pattern,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
else:
    # 显式白名单（逗号分隔）：以用户配置为准，不设守卫
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# ---- 注册路由（原有 9 个能力模块 + server.py 拆出的 11 个）----
app.include_router(rvc_live_router)
app.include_router(ft_router)
app.include_router(audiobook_router)
app.include_router(offlinevc_router)
app.include_router(seedvc_router)
app.include_router(cascade_router)
app.include_router(effects_router)
app.include_router(wechat_router)
app.include_router(history_router)
app.include_router(system_router)
app.include_router(voices_router)
app.include_router(raw_media_router)
app.include_router(pipeline_router)
app.include_router(clips_router)
app.include_router(tts_router)
app.include_router(mine_router)
app.include_router(market_router)

# 音色市场远程图库：启动后台自动同步（VM_MARKET_IMG_REPO 未配置时为 no-op）
try:
    from market_images import start_background_sync
    start_background_sync()
except Exception:
    pass
app.include_router(capture_router)
app.include_router(ab_router)
app.include_router(ab_chain_router)
app.include_router(audio_router)

# 预热 TTS worker + RVC 常驻模型：消除首条几十秒的模型加载
# （实测 TTS 冷 41.8s→2.9s、RVC 24.7s→0.3s，端到端 ~77s→~13s）
try:
    from warmup import start_background
    start_background()
except Exception:
    pass
app.include_router(media_router)
app.include_router(rvc_dataset_router)

# ---------------- 局域网访问：托管前端静态资源（手机浏览器打开 http://<本机IP>:8000） ----------------
# 安装版前端在 app.asar 里不可读，打包时额外放一份到 backend/web_dist；开发态直接用 web/dist。
_web_dist = next((c for c in [ROOT / "web_dist", ROOT / "web" / "dist"]
                  if (c / "index.html").exists()), None)
if _web_dist is not None:

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa(full_path: str):
        cand = (_web_dist / full_path).resolve()
        if full_path and cand.is_file() and str(cand).startswith(str(_web_dist.resolve())):
            return FileResponse(cand)
        return FileResponse(_web_dist / "index.html")


if __name__ == "__main__":
    _start_audio_audit()  # 音频设备残留自动巡检（FRD F4）
    # 打开桌面端 = 拉起后端：自动把 m2_server/tools/web/dist 镜像同步到
    # resources/backend 兜底副本（安装版回退用），详见 backend_autosync.py。
    # 后台线程执行，失败/关闭（VM_BACKEND_AUTOSYNC=0）均不影响启动。
    from backend_autosync import autostart_sync

    autostart_sync(ROOT)
    uvicorn.run(app, host=cfg.SERVER_HOST, port=cfg.SERVER_PORT)
