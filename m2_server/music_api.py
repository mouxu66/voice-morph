"""曲库发现：抖音热歌榜 / B站音乐榜 / B站搜索 → 交给 yt-dlp 取音。

为什么抖音只做「发现」
---------------------
抖音热歌榜接口能拿到歌名、歌手、热度，但 `music_info.play_url` 是空的（版权），
所以它只负责回答「现在流行什么」；真取音统一走 B站。B站这条链路与
`sound.ytdlp` 完全同一条外部工具桥 —— 本模块**不内置任何平台解析**，
也不碰 DRM；yt-dlp 由用户自备，产物落会话目录、退出即删。

为什么挂在 sound.fx-board
-------------------------
面板里这个入口长在「放歌卡」上，而放歌本身是 fx-board 的能力。用 `CAP.fx`
同一道门控：关掉声板 = 搜歌入口一起消失，不会留下一个按了就 404 的按钮。

三条边界
--------
    ① 取音只放行 B站链接（`_assert_bilibili`）。不借这个端点当任意站点的
       下载代理 —— 那是 `ytdlp_fetch` 白名单之外的第二张嘴。
    ② 榜单只做 GET、只读 JSON，不落盘、不转发图片（封面 URL 交给前端展示）。
    ③ 网络失败一律 502 + 人话，不把 `requests` 的英文栈透给用户。
"""

from __future__ import annotations

import html as _html
import re
import threading
import time
import urllib.parse

import requests
import ytdlp_fetch
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/music", tags=["music"])

#: 榜单/搜索是「帮用户找」而不是「替用户抓」 —— 12s 足够，挂住只会让面板转圈。
_TIMEOUT_S = 12.0

#: 榜单缓存：几分钟内反复打开托盘不该重复打平台接口。
#: 搜索结果同样缓存 5 分钟：同一个关键词连打会让 B站 412（风控）。
_CACHE_TTL_S = 300.0

#: 面板一次只展示 4~5 行，榜单多取一点供滚动。
_CHART_LIMIT = 10

#: 歌曲时长过滤：太短多半是切片/广告，太长多半是合集，都不适合「放一首歌」。
_MIN_SONG_S = 15.0
_MAX_SONG_S = 600.0

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_BILI_REFERER = "https://www.bilibili.com/"
_BILI_SEARCH_REFERER = "https://search.bilibili.com/"

_DOUYIN_CHART_URL = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/music/"
_BILI_CHART_URL = "https://api.bilibili.com/x/web-interface/ranking/v2?rid=3&type=all"
_BILI_SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"

_TAG_RE = re.compile(r"<[^>]+>")
_JOB_ID_RE = re.compile(r"^[0-9A-Za-z_-]{8,64}$")

#: 缓存：key -> (写入时刻, 条目列表)。只缓存列表，异常不缓存（下次还能重试）。
_CACHE: dict[str, tuple[float, list[dict]]] = {}
_CACHE_LOCK = threading.Lock()

#: B站搜索/榜单的匿名 cookie（`buvid3` 等，访问首页时由服务端下发）。
#:
#: 为什么必须带（2026-10-23 真机验证）：光秃秃地打 `search/type` 一开始能用，
#: 但同一 IP 多打几次后就开始恒定 412 —— 实测**同一时刻**不带 cookie 412、
#: 带首页下发的 `buvid3` 200。它是匿名设备标识，不是登录态，也不需要账号。
#: 所以：先裸打；只有撞 412 才去首页领一次 cookie，缓存 6 小时后失效重领。
#: 别把这一步挪到每次请求之前 —— 那会让每次搜歌都多一次首页往返。
_COOKIE = {"value": "", "at": 0.0}
_COOKIE_LOCK = threading.Lock()
_COOKIE_TTL_S = 6 * 3600.0
_BILI_HOME = "https://www.bilibili.com/"


def _refresh_bili_cookie() -> str:
    """访问 B站首页领取匿名 cookie，返回可直接放进 `Cookie:` 头的字符串。"""
    try:
        resp = requests.get(_BILI_HOME, headers={"User-Agent": _UA}, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        jar = getattr(resp, "cookies", None)
        pairs = jar.get_dict() if jar is not None else {}
        value = "; ".join(f"{k}={v}" for k, v in pairs.items() if k in ("buvid3", "b_nut"))
    except Exception:  # noqa: BLE001 —— 领不到就按没有重试，原错误更有诊断价值
        return ""
    if value:
        with _COOKIE_LOCK:
            _COOKIE["value"] = value
            _COOKIE["at"] = time.time()
    return value


def _bili_cookie() -> str:
    with _COOKIE_LOCK:
        if _COOKIE["value"] and time.time() - _COOKIE["at"] < _COOKIE_TTL_S:
            return _COOKIE["value"]
    return _refresh_bili_cookie()


def _cached(key: str, build) -> list[dict]:
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < _CACHE_TTL_S:
            return [dict(x) for x in hit[1]]
    items = build()
    with _CACHE_LOCK:
        _CACHE[key] = (now, [dict(x) for x in items])
        # 缓存无上限地长会让一个长跑进程慢慢吃内存；面板的查询模式很集中，
        # 超过 64 条就丢掉最老的一半即可（不值得引第三方 TTL 库）。
        if len(_CACHE) > 64:
            keep = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[-32:]
            _CACHE.clear()
            _CACHE.update(keep)
    return [dict(x) for x in items]


def _get_json(url: str, referer: str = "") -> dict:
    headers = {"User-Agent": _UA, "Accept": "application/json, text/plain, */*"}
    if referer:
        headers["Referer"] = referer
    resp = requests.get(url, headers=headers, timeout=_TIMEOUT_S)
    # B站风控：缺 `buvid3` 时搜索接口回 412（榜单接口同样可能）。领一次 cookie 重试，
    # 只重试一次 —— 再 412 就是真被限了，把原错误交给上层说人话。
    if getattr(resp, "status_code", 200) == 412:
        cookie = _bili_cookie()
        if cookie:
            headers["Cookie"] = cookie
            resp = requests.get(url, headers=headers, timeout=_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("平台返回的不是 JSON 对象")
    return data


def _clean(text) -> str:
    """去掉 HTML 高亮标签与多余空白。B站搜索标题里带 `<em class="keyword">`。"""
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub("", str(text or "")))).strip()


def _duration_s(value) -> float:
    """榜单给秒数，搜索给 `"4:12"` / `"1:02:03"` —— 两种都收。读不出给 0。"""
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    if raw.isdigit():
        return float(raw)
    parts = raw.split(":")
    if 1 < len(parts) <= 3 and all(p.isdigit() for p in parts):
        total = 0
        for part in parts:
            total = total * 60 + int(part)
        return float(total)
    return 0.0


def _cover(url) -> str:
    out = str(url or "").strip()
    return "https:" + out if out.startswith("//") else out


def _bili_item(row: dict) -> dict | None:
    bvid = str(row.get("bvid") or "").strip()
    if not bvid:
        return None
    title = _clean(row.get("title"))
    if not title:
        return None
    dur = _duration_s(row.get("duration"))
    if dur and (dur < _MIN_SONG_S or dur > _MAX_SONG_S):
        return None
    artist = _clean(row.get("author") or (row.get("owner") or {}).get("name"))
    stat = row.get("stat") or {}
    hot = stat.get("view") if stat else row.get("play")
    return {
        "source": "bilibili",
        "title": title,
        "artist": artist,
        "bvid": bvid,
        "url": f"https://www.bilibili.com/video/{bvid}",
        "duration_s": round(dur, 1),
        "cover": _cover(row.get("pic")),
        "hot": int(hot or 0),
    }


def _douyin_items(limit: int) -> list[dict]:
    data = _get_json(_DOUYIN_CHART_URL)
    items: list[dict] = []
    for rank, row in enumerate(data.get("music_list") or [], 1):
        info = row.get("music_info") or {}
        title = _clean(info.get("title"))
        if not title:
            continue
        covers = (info.get("cover_thumb") or {}).get("url_list") or []
        items.append({
            "source": "douyin",
            "rank": rank,
            "title": title,
            "artist": _clean(info.get("author")),
            "hot": int(row.get("hot_value") or 0),
            "cover": _cover(covers[0] if covers else ""),
        })
        if len(items) >= limit:
            break
    return items


def _bili_rank(limit: int) -> list[dict]:
    data = _get_json(_BILI_CHART_URL, referer=_BILI_REFERER)
    rows = (data.get("data") or {}).get("list") or []
    items = [it for row in rows if (it := _bili_item(row))]
    # 榜单里偶尔混进 10 秒预告/几小时合集，过滤后可能不足 limit —— 不强补。
    return items[:limit]


def _bili_search(query: str, limit: int) -> list[dict]:
    # ★ 别加 `page_size`：实测（2026-10-23）带它必回 412 Precondition Failed，
    # 不带就正常 —— 默认每页 20 条，正好够面板取前 8 条。这是 B站的风控脾气，
    # 不是参数写错，加回来会让整个搜歌层直接不可用。
    params = urllib.parse.urlencode({
        "search_type": "video",
        "keyword": query,
        "page": 1,
    })
    data = _get_json(f"{_BILI_SEARCH_URL}?{params}", referer=_BILI_SEARCH_REFERER)
    rows = (data.get("data") or {}).get("result") or []
    items: list[dict] = []
    for row in rows:
        if row.get("type") != "video":
            continue
        item = _bili_item(row)
        if item:
            items.append(item)
        if len(items) >= limit:
            break
    return items


def _fail(source: str, exc: Exception) -> HTTPException:
    return HTTPException(status_code=502, detail=f"{source}拉不到：{exc}")


@router.get("/charts")
def charts(source: str = "douyin", limit: int = _CHART_LIMIT):
    """热歌榜。抖音榜只给歌名/歌手/热度，音频要点进去再从 B站 取。"""
    src = "bilibili" if str(source).strip().lower() == "bilibili" else "douyin"
    limit = max(1, min(int(limit or _CHART_LIMIT), 20))
    key = f"chart:{src}:{limit}"
    try:
        items = _cached(key, lambda: _douyin_items(limit) if src == "douyin" else _bili_rank(limit))
    except Exception as exc:  # noqa: BLE001 —— 网络/平台改版都归「拉不到」，文案给人看
        raise _fail("抖音热歌榜" if src == "douyin" else "B站音乐榜", exc) from exc
    return {"ok": True, "source": src, "items": items}


@router.get("/search")
def search(q: str = "", limit: int = 8):
    """按歌名/歌手搜 B站。返回的是**视频**，取音由 `/fetch` 完成。"""
    query = _clean(q)[:60]
    if not query:
        raise HTTPException(status_code=400, detail="歌名不能为空")
    limit = max(1, min(int(limit or 8), 12))
    key = f"search:{query}:{limit}"
    try:
        items = _cached(key, lambda: _bili_search(query, limit))
    except Exception as exc:  # noqa: BLE001
        raise _fail("B站搜索", exc) from exc
    return {"ok": True, "query": query, "items": items}


class FetchReq(BaseModel):
    """取音请求：一条 B站视频地址 + 前端生成的作业号（用于进度/取消）。"""

    url: str
    job_id: str = ""
    title: str = ""
    artist: str = ""


def _assert_bilibili(url: str) -> None:
    """只放行 B站域名族。别让这个端点变成第二个「任意 URL 下载器」。"""
    target = str(url or "").strip()
    if not target or len(target) > 600:
        raise HTTPException(status_code=400, detail="链接不能为空，且不能超过 600 字符")
    try:
        host = ytdlp_fetch._host_of(target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"链接解析失败：{exc}") from exc
    if not (host == "bilibili.com" or host.endswith(".bilibili.com") or host == "b23.tv"):
        raise HTTPException(status_code=400, detail="只能取 B站 的音频")


def _wav_duration(path: str) -> float:
    try:
        import soundfile as sf

        return round(float(sf.info(path).duration), 1)
    except Exception:  # noqa: BLE001 —— 时长只用于展示，读不到不影响取回成功
        return 0.0


@router.post("/fetch")
def fetch(req: FetchReq):
    """把一条 B站链接交给 yt-dlp 取成 mp3，落会话目录（退出即删）。"""
    _assert_bilibili(req.url)
    job = None
    if req.job_id:
        if not _JOB_ID_RE.match(req.job_id.strip()):
            raise HTTPException(status_code=400, detail="job_id 格式不对")
        job = ytdlp_fetch.new_job(req.job_id)
    try:
        info = ytdlp_fetch.fetch(req.url, job=job)
    except ytdlp_fetch.YtdlpError as exc:
        # 400 而不是 500：没装 yt-dlp / 会员曲 / 版本旧都是「环境或输入」问题。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"取音失败：{exc}") from exc
    finally:
        # 作业只在这条请求的生命周期里存在；前端拿到结果后停止轮询。
        ytdlp_fetch.forget(job)
    return {
        "ok": True,
        "name": info["name"],
        "url": info["url"],
        "duration_s": _wav_duration(info["path"]),
        "title": _clean(req.title),
        "artist": _clean(req.artist),
        "source": "bilibili",
        "source_url": info.get("source_url", ""),
    }


@router.get("/fetch/{job_id}")
def fetch_progress(job_id: str):
    """取音进度。跑完/没这个 id 都返回 `found: false`，前端据此停止轮询。"""
    if not _JOB_ID_RE.match(job_id or ""):
        raise HTTPException(status_code=400, detail="job_id 格式不对")
    job = ytdlp_fetch.job_of(job_id)
    if job is None:
        return {"found": False, "stage": "", "percent": None, "cancelled": False, "elapsed_s": 0.0}
    return {"found": True, **job.snapshot()}
