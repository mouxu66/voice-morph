"""M2 转换服务（FastAPI · 常驻服务）—— 应用装配层。

启动：
    python m2_server/server.py
    默认监听 8000 端口。

本文件只负责：创建 app、可选 Token 鉴权中间件、CORS、注册全部路由、
托管前端静态资源。业务实现按域拆在同目录各模块（行为与拆分前一致）。

路由**容错注册**（某个能力不可用不能拖垮整个后端）在 plugin_loader.py，
启动时会 print 一行「路由模块 N/M 个已加载」及逐条失败原因。

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
    openai_compat.py     /v1/*（OpenAI 兼容：audio/speech、models；供既有 SDK 零改动接入）
    qwen3_tts.py         Qwen3-TTS 客户端（懒启动子进程 worker）
    qwen3_tts_service.py Qwen3-TTS 常驻 worker（venv312，端口 8001）

共享运行状态（流水线进度/挖掘状态/内录状态/路径常量）在 runtime.py。
"""

import re
import secrets

import config as cfg
import plugin_loader
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from runtime import ROOT
from starlette.middleware.base import BaseHTTPMiddleware

app = FastAPI(title="变声 · M2 转换服务", version="0.1.0")


# 可选 Token 鉴权：仅当配置了 VM_API_TOKEN 时启用，否则完全不拦截（LAN-only 默认）。
# 设计要点：
# - 本机回环(127.0.0.1/::1)永远放行：PC 前端(Electron/vite proxy)、桌宠、全局热键、
#   子进程都不带 token，启用鉴权不能破坏本机任何链路
# - 局域网请求需通过以下任一方式：X-API-Key 头 / Authorization: Bearer / api_key 查询参数
#   （移动端原生音频播放器请求 URL 时带不了自定义 header，所以必须支持查询参数）
def _token_eq(given: str, expected: str) -> bool:
    """常量时间比较。

    不能用 `==`：字符串比较一旦在某字节不等就返回，局域网内可被按前缀逐字节爆破
    （2026-09-13 安全审查）。比 bytes 而非 str —— compare_digest 对含非 ASCII 的 str
    会直接抛 TypeError，而我们不想因为用户设了个中文 token 就让鉴权 500。
    """
    return secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


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
                _token_eq(request.headers.get("X-API-Key", ""), token)
                or _token_eq(request.headers.get("Authorization", ""), f"Bearer {token}")
                or _token_eq(request.query_params.get("api_key", ""), token)
            )
            if not ok:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "unauthorized（需 X-API-Key 头或 api_key 参数）"},
                )
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
            return JSONResponse(status_code=403, content={"detail": f"拒绝跨站来源: {origin}"})
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

# ---- 注册路由：容错加载（顺序与拆分前逐条一致）----
# 为什么不再直接 `from xxx import router`：那是 26 处**模块级**导入，任意一处失败
# （缺 torch / 缺权重 / 缺可选依赖 / 一个笔误）都会让整个后端 `ImportError` 起不来，
# 而「用户没装这个能力」本来就是正常状态，不该表现为「软件打不开」。
# 记账口径、为什么不吞 `BaseException` 见 plugin_loader.py 的模块注释。
#
# ⚠️ 两点别动：
#   1. **顺序有语义** —— FastAPI 按注册顺序匹配路由，重排会让重叠路径换一个 handler 接；
#   2. 拆成三段 `_register(...)` 是**照搬**原来的位置：中间那两个启动副作用
#      （pet_market / market_images）原本就夹在 pet_market_api 与 capture_api 之间。
_ROUTER_ORDER: list[str] = []


def _register(*module_names: str) -> None:
    """按给定顺序容错注册：导不进来的只跳过它自己，并在 registry 里留下原因。"""
    _ROUTER_ORDER.extend(module_names)
    for name in module_names:
        router = plugin_loader.load_router(name)
        if router is not None:
            app.include_router(router)


_register(
    "rvc_live",
    "finetune",
    "audiobook",
    "offline_vc",
    "seed_vc",
    "cascade",
    "effects",
    "wechat_voice",
    "history_api",
    "system_api",
    "voices_api",
    "raw_media_api",
    "pipeline_api",
    "clips_api",
    "tts_api",
    "mine_api",
    "market_api",
    "pet_market_api",
)

# 人偶市场：确保默认内置皮肤（芙宁娜）物化到 outputs（幂等，失败不阻塞）
plugin_loader.call_hook("pet_market", "ensure_default_bundle")

# 音色市场远程图库：启动后台自动同步（VM_MARKET_IMG_REPO 未配置时为 no-op）
plugin_loader.call_hook("market_images", "start_background_sync")

_register(
    "capture_api",
    "ab_api",
    "ab_chain",
    "audio_api",
)
# 试音间：一个声音 × 多个音色（复用 offline_vc / market_preview / ab_chain 的链路与尺子）
_register("audition_api")

# 预热 TTS worker + RVC 常驻模型：消除首条几十秒的模型加载
# （实测 TTS 冷 41.8s→2.9s、RVC 24.7s→0.3s，端到端 ~77s→~13s）
plugin_loader.call_hook("warmup", "start_background")

# OpenAI 兼容层：prefix=/v1（**不是** /api/v1）——SDK 的 base_url="…/v1" 语义要求如此，
# 挂到 /api/v1 会让用户按官方文档写反而打不通。详见 openai_compat.py 模块注释。
_register("media_api", "rvc_dataset_api", "openai_compat")

# 启动横幅：把容错结果说清楚。“哪个能力不可用”必须是**可见的**，
# 否则这次容错只是把“崩溃”换成了“静默”，反而更难查。
print(plugin_loader.report(_ROUTER_ORDER))

# ---------------- 局域网访问：托管前端静态资源（手机浏览器打开 http://<本机IP>:8000） ----------------
# 安装版前端在 app.asar 里不可读，打包时额外放一份到 backend/web_dist；开发态直接用 web/dist。
_web_dist = next(
    (c for c in [ROOT / "web_dist", ROOT / "web" / "dist"] if (c / "index.html").exists()), None
)
if _web_dist is not None:

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa(full_path: str):
        web_root = _web_dist.resolve()
        cand = (_web_dist / full_path).resolve()
        # is_relative_to 而非字符串 startswith：后者会放行同前缀的兄弟目录
        # （如 web_dist_backup），与 media_api.py 的穿越防护保持一致
        if full_path and cand.is_file() and cand.is_relative_to(web_root):
            return FileResponse(cand)
        return FileResponse(_web_dist / "index.html")


if __name__ == "__main__":
    # 音频设备残留自动巡检（FRD F4）。audio_api 导不进来时静默跳过 ——
    # 巡检是「附加保险」，不该因为它连累整个后端起不来。
    plugin_loader.call_hook("audio_api", "_start_audio_audit")
    # 打开桌面端 = 拉起后端：自动把 m2_server/tools/web/dist 镜像同步到
    # resources/backend 兜底副本（安装版回退用），详见 backend_autosync.py。
    # 后台线程执行，失败/关闭（VM_BACKEND_AUTOSYNC=0）均不影响启动。
    from backend_autosync import autostart_sync

    autostart_sync(ROOT)
    uvicorn.run(app, host=cfg.SERVER_HOST, port=cfg.SERVER_PORT)
