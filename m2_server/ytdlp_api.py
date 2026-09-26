"""「在线扒歌（yt-dlp）」的 HTTP 接口。

    GET  /api/ytdlp/status   这个可选工具在不在、什么版本、支持哪些站点 / 怎么装
    POST /api/ytdlp/fetch    粘一条平台链接 → 拉到会话目录 → 返回可试听地址

与「翻唱」页的关系
------------------
本插件是**取歌的第 3 条路**，和已有的两条并列、互不替代：

    ① 上传文件      —— 本地已有歌
    ② 粘直链        —— 手上有可直下的 URL（`url_fetch`）
    ③ 在线扒歌      —— 手上只有平台分享链接（本模块，外部 yt-dlp）

三条路都落在**同一个会话目录**（`session_out`），产物都"退出即删" ——
所以第 ③ 条拉回来的歌可以直接喂给 `/api/cover/run` 的 `src_name`，
用户不用再下到本地中转一次。

为什么 `status` 单独一条而不用 /api/health
-----------------------------------------
`/api/health` 是内核的（core.system），而 yt-dlp 属于**可关的外部工具**。
把它塞进 health 会让"用户主动关掉这个插件"和"工具没装"混在同一个告警里 ——
manifest 的三态设计（ok/broken/disabled）正是为了避免这个。所以状态由本插件
自己报，前端在「在线扒歌」页里展示。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import ytdlp_fetch
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


class YtdlpFetchReq(BaseModel):
    """要拉的链接（平台分享链接或歌曲页 URL）。"""

    url: str


def _duration_of(path: Path) -> float:
    """读时长（只解析文件头）。读不出来给 0 —— 试听那一行少个数字而已。

    与 `cover_api._duration_of` 同款：这里不 import 它，避免两个插件模块互相
    依赖（`sound.ytdlp` 的 requires 只声明了 core.media，没声明 sound.cover）。
    """
    try:
        import soundfile as sf

        return round(float(sf.info(str(path)).duration), 1)
    except Exception:  # noqa: BLE001 —— 时长只是展示，读不到不该影响拉取结果
        return 0.0


@router.get("/ytdlp/status")
async def ytdlp_status():
    """yt-dlp 的可用状态。**只读、不发网络** —— 页面一打开就能显示。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, ytdlp_fetch.probe)


@router.post("/ytdlp/fetch")
async def ytdlp_fetch_endpoint(req: YtdlpFetchReq):
    """把平台链接对应的音频拉到会话目录，返回可立即试听的地址。

    与 `/cover/fetch` 一样是**只下载不跑链路**：翻唱要几分钟，先花几秒确认
    "拉到的确实是这首歌"，比等三分钟再发现拿错划算。

    阻塞的 subprocess 丢进线程池 —— yt-dlp 跑几十秒，占着事件循环会让整个
    后端（含桌宠轮询）卡住。
    """
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, ytdlp_fetch.fetch, req.url)
    except ytdlp_fetch.YtdlpError as e:
        # 400 而不是 500：这些都是"输入/环境不对"，不是后端崩了。
        # 消息直接透给用户 —— `ytdlp_fetch` 里的文案就是按给人看写的。
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {
        "ok": True,
        "name": info["name"],
        "url": info["url"],
        "bytes": info["bytes"],
        "site": info["site"],
        "duration_s": _duration_of(Path(info["path"])),
    }
