"""「在线扒歌（yt-dlp）」的 HTTP 接口。

    GET  /api/ytdlp/status                这个可选工具在不在、什么版本、支持哪些站点 / 怎么装
    POST /api/ytdlp/fetch                 粘一条平台链接 → 拉到会话目录 → 返回可试听地址
    GET  /api/ytdlp/fetch/{job_id}        这次取回到哪一步了（进度/阶段/被取消没）
    POST /api/ytdlp/fetch/{job_id}/cancel 喊停这次取回

与「翻唱」页的关系
------------------
本插件是**取歌的第 3 条路**，和已有的两条并列、互不替代：

    ① 上传文件      —— 本地已有歌
    ② 粘直链        —— 手上有可直下的 URL（`url_fetch`）
    ③ 在线扒歌      —— 手上只有平台分享链接（本模块，外部 yt-dlp）

三条路都落在**同一个会话目录**（`session_out`），产物都"退出即删" ——
所以第 ③ 条拉回来的歌可以直接喂给 `/api/cover/run` 的 `src_name`，
用户不用再下到本地中转一次。

会员曲的兜底（可选开关，默认关）
--------------------------------
QQ 音乐几乎所有曲库都要登录，其中一部分是**会员曲**：`vkey` 接口对它们返回
`purl` 空 + `fnameHitCa`（Copyright authority 不通过），yt-dlp 只能报
`No video formats found!`。实测证据（2026-09-27，《唯一》）：

    pay_play=1  pay_month=1  price_track=200  pay_status=0   ← 会员曲
    匿名 vkey → purl 空 ；配了真登录态 → 仍 purl 空（Ca = 版权不通过）

但用户发现了一件事：**QQ 音乐的 MV 一直是免费能看的**。顺着实测下来，MV 播放页
内部吐的是**明文 MPEG-TS 分片**，不校验登录态、不加密；差异在于 MV 音轨是混过的
（192kbps AAC，含影像声音），不是母带。

所以加一条**显式开关的兜底**：yt-dlp 失败 → 若是 QQ 音乐链接 → 找关联 MV → 抽音轨。
默认关（`VM_MV_FALLBACK=1` 才开），理由与 `VM_YTDLP_COOKIES` 一致：这是"绕过平台
会员授权"的取音路径，用户自己开、自己承担使用边界。实现与全部实测坑在
`mv_audio_fallback` 模块里。**兜底成功时结果里会带 `quality_note`，上游必须透给用户。**

为什么 `status` 单独一条而不用 /api/health
-----------------------------------------
`/api/health` 是内核的（core.system），而 yt-dlp 属于**可关的外部工具**。
把它塞进 health 会让"用户主动关掉这个插件"和"工具没装"混在同一个告警里 ——
manifest 的三态设计（ok/broken/disabled）正是为了避免这个。所以状态由本插件
自己报，前端在「在线扒歌」页里展示。

为什么取回要带一个 `job_id`
--------------------------
取回是**同步阻塞**的（前端一个 await 等到底），没有人能中途把"用户改主意了"
送进去。所以由前端生成一个 id 随请求带来，取消/查进度都按这个 id 找作业
（`ytdlp_fetch.FetchJob`）。两条额外的兜底：

    · `job_id` 是**前端生成**的，所以第一次请求就能带上它，不存在
      "请求发出去了但还不知道自己叫啥"的竞态；
    · 前端**关掉页面**时 HTTP 连接会断，这里也顺手取消 —— 否则用户以为关页面
      就等于停了，而后台 yt-dlp 还在跑（这正是改动前的实际行为）。
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import urllib.parse
from pathlib import Path

import mv_audio_fallback
import ytdlp_fetch
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

#: `job_id` 由前端给，所以**必须校验**：它会被当作字典键用，也是取消操作的凭据。
#: 只放行 uuid / base36 这类字符，长度给足（uuid4 是 36 字符）。
_JOB_ID_RE = re.compile(r"^[0-9A-Za-z_-]{8,64}$")

#: 轮询断连的间隔。比 `ytdlp_fetch._POLL_S` 松一档：这一步只是为了"关页面也能停"，
#: 没必要和看门狗同频。
_DISCONNECT_POLL_S = 0.5


class YtdlpFetchReq(BaseModel):
    """要拉的链接（平台分享链接或歌曲页 URL）。"""

    url: str
    """
    可选的作业号（前端生成）。给了就能取消 / 查进度；不给也照常工作
    （CLI、脚本那种"跑完为止"的用法）。
    """
    job_id: str = ""


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
    out = await loop.run_in_executor(None, ytdlp_fetch.probe)
    # MV 兜底是同一页上的第二个开关，状态并进去 —— 前端一个请求拿全，
    # 不用为它单独开一条路由。
    out["mv_fallback"] = mv_audio_fallback.status()
    return out


def _qq_songmid_of(url: str) -> str | None:
    """这条链接是 QQ 音乐的吗？是的话返回 songmid，否则 `None`。

    只认 `y.qq.com` 域名族 —— 别家没有等价的免费 MV 通路，所以兜底不越界。
    抠 ID 复用 `ytdlp_fetch._find_qq_songmid`（它已经处理了分享短链、新旧路径、
    hash 路由等形态），这里不重复实现。

    ★ 短链要额外解一跳。App 里点「分享」出来的就是
    `https://c6.y.qq.com/base/fcgi-bin/u?__=…` 这种形式，`songmid` 在 302 之后
    才出现 —— 这里复用 `ytdlp_fetch._resolve_redirect`（它 `stream=True`、
    只要最终地址、正文一字节不读）。解不出来就返回 `None`（兜底不适用），
    不抛 —— 调用方拿 `None` 就已经能正确决策了。
    """
    try:
        host = ytdlp_fetch._host_of(url)
    except Exception:  # noqa: BLE001 —— 解析失败就是不适用，不该影响主流程
        return None
    if not (host == "y.qq.com" or host.endswith(".y.qq.com")):
        return None

    def _mid(u: str) -> str | None:
        try:
            parsed = urllib.parse.urlparse(u if "://" in u else f"https://{u}")
            return ytdlp_fetch._find_qq_songmid(parsed)
        except Exception:  # noqa: BLE001
            return None

    hit = _mid(url)
    if hit:
        return hit

    # 本地抠不到 → 解一跳短链再抠（与 `ytdlp_fetch.normalize` 同一策略）。
    # 解出来的地址**只用来抠 ID**，绝不交给任何下载器 —— 那是 SSRF 口子。
    try:
        final = ytdlp_fetch._resolve_redirect(url)
    except Exception:  # noqa: BLE001 —— 网络/超时/跳数超限都当作"不适用"
        return None
    return _mid(final)


def _try_mv_fallback(url: str, reason: str) -> dict | None:
    """yt-dlp 失败后，试一次「MV 抽音轨」兜底。拿不到返回 `None`。

    **不抛异常**：兜底本身失败不该盖掉 yt-dlp 那句错误 —— 用户要看到的是
    "为什么没成"（yt-dlp 的归因更准），而不是"兜底也失败了"。
    兜底失败的原因只进日志。
    """
    if not mv_audio_fallback.enabled():
        return None
    songmid = _qq_songmid_of(url)
    if not songmid:
        return None
    LOG.info("yt-dlp 失败（%s），试 MV 兜底：songmid=%s", reason[:80], songmid)
    try:
        return mv_audio_fallback.fetch_from_mv(songmid)
    except mv_audio_fallback.MvFallbackError as e:
        LOG.info("MV 兜底也没成：%s", e)
        return None
    except Exception:  # noqa: BLE001 —— 兜底是"多做一次尝试"，任何意外都不该外溢
        LOG.exception("MV 兜底出现意外错误")
        return None


def _result_payload(info: dict) -> dict:
    """把 `ytdlp_fetch.fetch` / `mv_audio_fallback.fetch_from_mv` 的同形结果
    转成 HTTP 响应体。

    两者字段一致（这是刻意的契约），只多一个可选的 `quality_note` ——
    兜底产物必须让用户看到"音质打折了"，不能混在正版音源里默认当成一样的东西。
    """
    out = {
        "ok": True,
        "name": info["name"],
        "url": info["url"],
        "bytes": info["bytes"],
        "site": info["site"],
        # 规范化之后的地址（用户粘短链时和输入不同）。放出来是为了让"这条链接
        # 为什么不行"可排查 —— 前端把它展示在结果里，比看后端日志直接得多。
        "source_url": info.get("source_url", ""),
        "duration_s": _duration_of(Path(info["path"])),
    }
    note = info.get("quality_note")
    if note:
        out["quality_note"] = note
        out["via"] = "mv_fallback"
    else:
        out["via"] = "ytdlp"
    return out


@router.post("/ytdlp/fetch")
async def ytdlp_fetch_endpoint(req: YtdlpFetchReq, request: Request):
    """把平台链接对应的音频拉到会话目录，返回可立即试听的地址。

    与 `/cover/fetch` 一样是**只下载不跑链路**：翻唱要几分钟，先花几秒确认
    "拉到的确实是这首歌"，比等三分钟再发现拿错划算。

    阻塞的 subprocess 丢进线程池 —— yt-dlp 跑几十秒，占着事件循环会让整个
    后端（含桌宠轮询）卡住。

    ★ 等结果的同时**盯着连接还在不在**：用户关掉页面（或前端 abort）时把作业
    一起取消。不盯的话，请求虽然断了，后台的 yt-dlp 还会把整首歌拉完 ——
    而用户那边的观感是"已经不用了，它却还在下载"。
    """
    job = None
    if req.job_id:
        if not _JOB_ID_RE.match(req.job_id.strip()):
            raise HTTPException(status_code=400, detail="job_id 格式不对")
        job = ytdlp_fetch.new_job(req.job_id)

    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(
        None, functools.partial(ytdlp_fetch.fetch, req.url, job=job)
    )
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=_DISCONNECT_POLL_S)
            if done:
                break
            if await _client_gone(request):
                LOG.info("客户端已断开，取消取回作业 %s", getattr(job, "id", ""))
                ytdlp_fetch.cancel_job(getattr(job, "id", ""))
        info = task.result()
    except ytdlp_fetch.YtdlpError as e:
        # ★ 主路失败 → 试一次 MV 兜底（默认关，见模块注释「会员曲的兜底」）。
        # 兜底放在这里而不是 `ytdlp_fetch` 内部，是为了让那个模块保住
        # "没有一行绕 DRM 的代码"这条性质。
        ytdlp_fetch.forget(job)
        fallen = await loop.run_in_executor(
            None, functools.partial(_try_mv_fallback, req.url, str(e))
        )
        if fallen is not None:
            return _result_payload(fallen)
        # 兜底没开 / 不适用 / 也没成 —— 抛 yt-dlp 的原话，那才是用户要的归因。
        # 400 而不是 500：这些都是"输入/环境不对"，不是后端崩了。
        # 消息直接透给用户 —— `ytdlp_fetch` 里的文案就是按给人看写的。
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        ytdlp_fetch.forget(job)

    return _result_payload(info)


async def _client_gone(request: Request) -> bool:
    """连接断了没。**尽力而为**：任何异常都当成"还在"（宁可多跑一次，也别误杀）。"""
    try:
        return await request.is_disconnected()
    except Exception:  # noqa: BLE001 —— 见 docstring：探测失败不该影响取回
        return False


@router.get("/ytdlp/fetch/{job_id}")
async def ytdlp_fetch_progress(job_id: str):
    """这次取回到哪一步了。**只读内存**，跑完/没这个 id 都返回 `found: false`。

    前端在取回期间按秒轮询它 —— 有了进度，"这条链接是不是卡住了"用户自己就能判断，
    不用靠干等。
    """
    job = ytdlp_fetch.job_of(job_id)
    if job is None:
        return {"found": False, "stage": "", "percent": None, "cancelled": False, "elapsed_s": 0.0}
    return {"found": True, **job.snapshot()}


@router.post("/ytdlp/fetch/{job_id}/cancel")
async def ytdlp_fetch_cancel(job_id: str):
    """喊停。`found: false` 表示这次取回已经不在了（跑完了 / id 不对），不是错误。"""
    return {"ok": True, "found": ytdlp_fetch.cancel_job(job_id)}
