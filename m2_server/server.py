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
import plugin_manifest
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

# ---- 注册路由：**由插件清单驱动**（第 3 步）----
# 为什么不再直接 `from xxx import router`：那是 26 处**模块级**导入，任意一处失败
# （缺 torch / 缺权重 / 缺可选依赖 / 一个笔误）都会让整个后端 `ImportError` 起不来，
# 而「用户没装这个能力」本来就是正常状态，不该表现为「软件打不开」。
# 记账口径、为什么不吞 `BaseException` 见 plugin_loader.py 的模块注释。
#
# 为什么也不在这里手写模块名：手写 = 第二份真相源。清单（`plugins/*/plugin.json`
# 的 `routers`）已经是「哪个能力有哪些模块」的唯一出处，挂载照着它走即可。
# 模块清单不在这里列了；有一条容易踩的记一下：`openai_compat` 的 prefix 是 `/v1`
# （**不是** `/api/v1`）—— SDK 的 base_url="…/v1" 语义要求如此，挂到 /api/v1
# 会让用户按官方文档写反而打不通（详见 openai_compat.py 模块注释）。
#
# 顺序 = 清单 `order` 升序（core 10-70 → sound 100-180 → pet 200-210 → hook 220），
# 插件内按 `routers` 声明序。原注释写着「顺序有语义，别动」，2026-09-20 第 3 步
# 把这句话**量了一遍**（139 条真实路由）：
#     · 完全相同的 (method, path)              0 条
#     · router 之间互相遮蔽                     0 条
#     · 65 条遮蔽关系全部是 router vs SPA 兜底  且全部「先注册、无害」
#     · 低优先级路由 / websocket / Mount        0 个
# 所以「历史交错序 → 清单 order」这次搬家是**行为等价**的，而原来那句警告守的
# 是「将来有人加了一对重叠路径」—— 那件事现在由 `tests/test_route_shadowing.py`
# 直接断言（不许出现重叠，出现就点名哪两条、属于哪个插件），比冻结一份没人
# 解释得清的历史顺序更管用。
#
# 原来的三段 `_register(...)` 之所以是三段，是为了把两个启动副作用
# （pet_market / market_images）夹在指定位置；现在副作用由清单的 `hooks` 驱动，
# 分段的历史理由也就消失了。
#
# 注意这里**会**因为清单坏掉而启动失败（`ManifestError`）。这是刻意的：清单既然
# 是挂载的唯一来源，读不出来就没有「退而求其次」的答案 —— 退化成「什么都不挂」
# 只会得到一个没有路由的后端，比大声报错更难查。清单是随包发的静态文件，
# 且 `tests/test_plugin_manifest.py` 全程校验它。
_ROUTER_ORDER: list[str] = []


def _mount_all() -> None:
    """按清单挂载全部 router，再跑 `when="import"` 的启动副作用。

    副作用**等全部 router 挂完再跑**，不随各自的插件穿插执行：这样保住原来
    「先装配完、再起副作用」的性质 —— warmup 会起一个加载 4.9G 模型的线程，
    让它和其它模块的 import 抢着跑没有好处。
    """
    # `mount_plan()` 是模块名的唯一来源，且**第 6 步起默认跳过被关掉的能力** ——
    # 关掉就要真的不 import（不拉 torch/CUDA 上下文、不吃显存），
    # 只让前端不显示的话是「省了个入口，没省资源」。
    for _plugin_id, module in plugin_manifest.mount_plan():
        _ROUTER_ORDER.append(module)
        router = plugin_loader.load_router(module)
        if router is not None:
            app.include_router(router)

    # 人偶市场默认皮肤物化（幂等）、音色市场远程图库后台同步（未配置时为 no-op）、
    # TTS worker + RVC 常驻模型预热（消除首条几十秒的模型加载：实测 TTS 冷
    # 41.8s→2.9s、RVC 24.7s→0.3s，端到端 ~77s→~13s）—— 都在清单里声明。
    for hook in plugin_manifest.hooks_for("import"):
        plugin_loader.call_hook(hook["module"], hook["attr"])


_mount_all()

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
        # `/api/*` 与 `/v1/*` **不是**前端路由，绝不允许兜到 SPA 首页。
        #
        # 2026-09-21 在已安装副本上实测到的：关掉一个能力 → 它的端点不再挂载 →
        # 本该 404，实际却是 `200 text/html`（index.html 被当成响应发回去）。
        # 后果比 404 难查得多：前端 `jsonFetch` 抛 `Unexpected token '<'`（看着像
        # 前端 bug），Network 面板里连红都没有，而验收清单里「点一下应该 404」那条
        # 判据**永远不成立** —— 一条测不出自己目标的判据比没有判据更坏。
        # 顺带也修掉一个更早就存在的问题：拼错任何 `/api/...` 路径都返回 200 HTML。
        # `/v1` 是 OpenAI 兼容面（`openai_compat` 的 prefix），同理。
        if full_path.startswith(("api/", "v1/")):
            return JSONResponse(
                status_code=404,
                content={"detail": f"无此接口: /{full_path}（路径可能拼错，或其所属能力已被关闭）"},
            )
        web_root = _web_dist.resolve()
        cand = (_web_dist / full_path).resolve()
        # is_relative_to 而非字符串 startswith：后者会放行同前缀的兄弟目录
        # （如 web_dist_backup），与 media_api.py 的穿越防护保持一致
        if full_path and cand.is_file() and cand.is_relative_to(web_root):
            return FileResponse(cand)
        return FileResponse(_web_dist / "index.html")


if __name__ == "__main__":
    # `when="main"` 的启动副作用：只在真入口跑。放这里而不是模块级是刻意的 ——
    # `import server`（打包探测、测试、工具脚本）不该产生这些副作用。
    # 目前唯一一条是 audio_api 的音频设备残留自动巡检（FRD F4）；导不进来时静默跳过，
    # 因为巡检是「附加保险」，不该因为它连累整个后端起不来。
    for _hook in plugin_manifest.hooks_for("main"):
        plugin_loader.call_hook(_hook["module"], _hook["attr"])
    # 打开桌面端 = 拉起后端：自动把 m2_server/tools/web/dist 镜像同步到
    # resources/backend 兜底副本（安装版回退用），详见 backend_autosync.py。
    # 后台线程执行，失败/关闭（VM_BACKEND_AUTOSYNC=0）均不影响启动。
    from backend_autosync import autostart_sync

    autostart_sync(ROOT)
    uvicorn.run(app, host=cfg.SERVER_HOST, port=cfg.SERVER_PORT)
