"""把一条**平台链接**交给外部的 yt-dlp 取回音频（「在线扒歌」插件用）。

这个模块做什么 / 不做什么
------------------------
**做**：找到用户自备的 yt-dlp，用它把网易云 / QQ音乐 / B站 等站点的音频拉进会话目录，
返回可试听的相对地址与时长，让上游走和「粘直链」完全一样的翻唱链路。

**不做**：不内置 yt-dlp、不代管它的更新、不解析任何平台接口。
它是**外部工具桥** —— 用户自己装、自己用、自己 `yt-dlp -U` 升级。

为什么是"桥"而不是"内置解析"
----------------------------
曾评估过三类方案（2026-09-26 调研结论）：

    · 第三方 PHP 解析接口（各种 `music.xxx.php?msg=`）—— 无维护者、平均几个月失效，
      且它绕的就是平台 DRM，写进源码里是开源/比赛的硬伤；
    · 社区 NeteaseCloudMusicApi 及其 fork —— 原版作者已**主动删库停更**
      （README 只剩"保护版权，此仓库不再维护"），活着的 fork 也随时可能步后尘；
    · **yt-dlp** —— 官方维护、更新极勤（2026-08 仍在发版），`netease:*` 与 `qqmusic:*`
      是一等公民 extractor，且**平台改版由整个社区一起修**。

选第三条，但仍然**不把它的逻辑抄进来**：它就是立可执行文件，调用即可。
这样既拿到了"活人维护"的好处，又让本仓库里**没有一行绕 DRM 的代码** ——
要为比赛/开源脱敏时，这块天然是干净的（用户自己装工具、自己承担使用边界）。

护栏为什么不能省
----------------
与 `url_fetch` 不同，这里**不自己发 HTTP**，所以拦不住 yt-dlp 内部的网络行为。
能守的两条边界必须守住：

    ① **站点白名单**（`_SUPPORTED_HOSTS`）。没有它，这个端点就是一个"拿 url 参数
       让服务器跑任意 yt-dlp"的万能入口 —— 最差情况变成内网探测/任意下载的工具。
       yt-dlp 自己支持上千个站点，这里只放行「歌词场景真的会用到」的那些。
    ② **产物必须落会话目录**，且只收 `.opus` 之外能喂给 demucs 的音频格式。
       "随用随删"是这个功能的核心诉求（用户的语音场景不可能存一堆歌），
       所以路径由 yt-dlp 的 `-P`/`-o` 钉死，不接受它自己挑地方。

失败一律抛 `YtdlpError`，消息是给用户看的人话（尤其"没装 yt-dlp"这一条 ——
静默失败会让人以为是歌的问题）。

第三条护栏：**分享链接规范化**（`normalize`）
--------------------------------------------
从 App 点「分享」拿到的链接，往往不是 yt-dlp 认的那个地址。2026-09-27 实测
QQ 音乐这条链路（用户给的真实链接）：

    用户粘的      https://c6.y.qq.com/base/fcgi-bin/u?__=yY3vbmLH9kYO
      ↓ 302（在服务端跳，用户看不见）
    跳到          https://i2.y.qq.com/n3/.../playsong/index.html?songmid=0023jgxa0Ym5yo
    再跳          https://y.qq.com/n/ryqq_v2/songDetail/0023jgxa0Ym5yo
                  ↑ yt-dlp 报 `Unsupported URL`（它的 qqmusic extractor 只认旧的
                    `/n/ryqq/songDetail/<mid>`，插件 2026.03.17 版本如此）

所以"粘进去没反应"里有一大类根本不是版权问题，是**地址形式**问题。`normalize`
的做法是：从链接里抠出稳定 ID（QQ 的 `songmid`、网易云的 `id`），再用 ID
**自己拼**官方规范地址。拼出来的地址由我们构造，绝不把跳转目标原样交给 yt-dlp
—— 那是 SSRF 口子。

第四条：**登录态是可选的显式开关**（`VM_YTDLP_COOKIES` / `VM_YTDLP_COOKIES_BROWSER`）
-----------------------------------------------------------------------------
QQ 音乐几乎全曲库都要登录才给音频流（实测那首歌报的是
`only available for registered users`，不是付费墙）。但**读浏览器 cookie 库是
侵入性操作**，所以默认一律不开：不设环境变量就发匿名请求。要用，用户自己指。

★ 注意 `--cookies-from-browser` 在 Windows 上经常不灵，且原因是环境而非代码：
    · Chrome ≥ v127 走 App-Bound Encryption → `Failed to decrypt with DPAPI`
      （yt-dlp issue #10927，本机实测复现）；
    · Edge/Chrome **自身在运行**时 sqlite 库被锁 → `Could not copy Chrome
      cookie database`（issue #7271，本机实测复现）。
所以文档里推的是"自己导出 cookies.txt 再指 `VM_YTDLP_COOKIES`"，浏览器那条只
当方便时能用就用。失败信息必须把这两条讲明白，否则用户只会看到一句"拉取失败"。

第五条：**取回必须能停**（`FetchJob` + 空闲看门狗 + 进程树杀）
------------------------------------------------------------
原来的样子：`subprocess.run(timeout=600)`。粘一条坏链接，前端只能干等
"取回中…"最多 10 分钟，用户唯一能做的是**关页面** —— 而后台 yt-dlp 还在跑。
更要紧的是 `run` 的 timeout 只 kill **直接子进程**：yt-dlp 自己会再拉起 ffmpeg，
超时后 ffmpeg 变成孤儿继续往会话目录里写，紧接着 `_cleanup` 的 `unlink`
撞占用文件**静默失败**（2026-09-27 实测）。三条一起修：

    ① **能取消**：`FetchJob` 是一张可撤销的作业票，前端拿 `job_id` 随时喊停；
       前端关掉页面时 HTTP 连接断开，`ytdlp_api` 那边也会把作业取消掉。
    ② **别等 10 分钟**：`600s` 的墙钟上限拦不住"挂着不动"，而单看总时长又会误杀
       慢网下真在下载的大文件。加 `--newline` 后 yt-dlp 每次进度更新占一行，
       所以**"静默"才是卡住的可靠信号** → 连续 `_IDLE_TIMEOUT_S` 秒无输出即中止。
    ③ **按进程树杀**：Windows 走 `taskkill /F /T`，POSIX 走 `killpg`；
       并且 `_cleanup` 改成**带重试**的删除（刚被杀的进程可能还占着句柄）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

import requests

import session_out

#: 找 yt-dlp 的顺序：环境变量 → PATH → 与项目同目录的常见位置。
#: 不写死盘符（换机器就废），不假设 Python 环境里有 `yt_dlp` 包 ——
#: 两种形态（独立 exe / pip 装的模块）都接受。
ENV_VAR = "VM_YTDLP"

#: 登录态（可选，**默认全关**）。指向一个 Netscape 格式的 cookies.txt。
#: 为什么不默认开：读用户的浏览器 cookie 库是侵入性操作，`--cookies-from-browser`
#: 会去解密 Chrome/Edge 的登录态数据库。这个插件是"外部工具桥"，不该顺手摸用户的
#: 浏览器凭据 —— 要用必须是用户自己显式打开。
ENV_COOKIES = "VM_YTDLP_COOKIES"
#: 退路：让 yt-dlp 自己去读某个浏览器的登录态（`edge` / `chrome` / `firefox` …）。
#: Windows 上经常因 App-Bound Encryption 或浏览器在运行而失败，失败信息在 `_explain` 里讲清。
ENV_COOKIES_BROWSER = "VM_YTDLP_COOKIES_BROWSER"

#: 精确放行的主机名。
_SUPPORTED_HOSTS: dict[str, str] = {
    "music.migu.cn": "咪咕音乐",
    # B站：官方另有 BilibiliAudio extractor，且大量歌曲只有 B 站有官方音源
    "b23.tv": "哔哩哔哩（短链）",
    # 合法自由音源（无版权风险，但中文歌少 —— 兜底用）
    "freemusicarchive.org": "Free Music Archive",
    "www.jamendo.com": "Jamendo",
}

#: 按**域名族**放行：命中 `host == 域` 或 `host.endswith("." + 域)`。
#:
#: 为什么需要：这些平台的分享链接在自家子域之间跳，逐个列举永远列不完 ——
#: QQ 音乐短链是 `c6.y.qq.com`（还有 c1~c9），落地页是 `i2.y.qq.com`，
#: 网易云分享是 `y.music.163.com`。2026-09-27 就是被 `c6.y.qq.com` 挡在外面。
#:
#: ★ 点号锚定是这里唯一的安全性质，别改成 `in` / `startswith`：
#:   `.y.qq.com` **不**匹配 `y.qq.com.evil.example`（攻击者买个子域就能绕过的
#:   最常见写法），也**不**匹配 `evilqq.com`。回归测试 `test_host_match_is_exact_not_prefix`
#:   显式钉住这一点。
_SUPPORTED_FAMILIES: dict[str, str] = {
    "y.qq.com": "QQ音乐",
    "music.163.com": "网易云音乐",
    "bilibili.com": "哔哩哔哩",
    "ximalaya.com": "喜马拉雅",
}

#: 拉取超时（**墙钟总上限**）。整首歌（含音视频分离后的音频流）通常几秒到几十秒；
#: 600s 是给"网络很慢的大文件"留的余量，不是给"挂着不动"的 —— 后者由
#: `_IDLE_TIMEOUT_S` 负责拦（那才是用户真正会撞上的那一种）。
_TIMEOUT_S = 600

#: 「卡住」判定：连续这么久**一行输出都没有**就中止。
#:
#: 为什么不只看墙钟：600s 的墙钟拦不住"挂着不动"（用户就是干等十分钟），
#: 而收紧墙钟又会误杀慢网下真在下载的大文件。yt-dlp 加了 `--newline` 之后
#: 每次进度更新占一行，所以**静默**才是卡住的可靠信号。
#: 90s 是给"解析页面 / ffmpeg 转码"这类本来就会安静一阵的阶段留的余量。
_IDLE_TIMEOUT_S = 90

#: `[download]  42.3% of ...` → 42.3。只用于给前端显示进度，解析不到不算错。
_PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")

#: 看门狗轮询间隔。0.5s 足够跟上"点取消立刻停"的手感，又不会空转。
_POLL_S = 0.5

#: `--audio-format` 转出来的容器。mp3 最通用，demucs / ffmpeg 都直接吃。
_AUDIO_FORMAT = "mp3"

#: yt-dlp 自己的网络行为没法从 Python 侧逐跳校验，但可以让它别把探测结果写到会话外。
_DENY_EXTRA = ("--no-playlist", "--no-warnings", "--no-part", "--newline")

#: 「抠 ID」用的模式。
#:
#: ★ 字符集**本身就是安全性质**：抠出来的值会被拼进 URL 交给 yt-dlp，
#: 是本模块唯一手工拼字符串的地方。这里排掉了 `/` `.` `?` `#` `%`，所以
#: 拼不出跨路径 / 改查询串的地址。长度上限只是防呆。
#: QQ 的 songmid 真实形态是 14 位 base62（如 `0023jgxa0Ym5yo`）。
_QQ_MID_RE = re.compile(r"^[0-9A-Za-z_-]{3,64}$")
#: 网易云的歌曲 id 是纯数字。
_NETEASE_ID_RE = re.compile(r"^\d{1,20}$")

#: 解短链的超时与最大跳数。这一步只为了**抠 ID**，正文(body)读完就丢。
_RESOLVE_TIMEOUT_S = 15
_MAX_REDIRECTS = 5
#: 与 `url_fetch` 保持一致的 UA —— 平台对陌生 UA 会返 403/空页，
#: 而这一步拿不到落地地址就等于没做。
_RESOLVE_UA = "VoiceMorph/1.0 (local)"

#: 常见音频容器。yt-dlp 的 `--audio-format mp3` 会把它们统一转成 mp3，
#: 但转码失败（缺 ffmpeg）时它会保留原容器 —— 两者都要能收。
_AUDIO_SUFFIXES = (".mp3", ".m4a", ".wav", ".flac", ".opus", ".webm", ".aac", ".ogg")


class YtdlpError(RuntimeError):
    """给用户看的错误（消息即文案）。"""


class FetchJob:
    """一次取回的**可撤销作业票**。

    存在的理由只有一个：`fetch` 是同步阻塞的（前端那边就是一个 await），
    没有别的地方能让"用户改主意了"传进来。所以前端拿一个 id，作业在跑的时候
    随时能 `cancel()`；`ytdlp_api` 检测到连接断开时也会调同一个 `cancel()`。

    进度字段（`stage` / `percent`）只做展示：解析不到就是 `None`，
    **绝不允许**因为"没读到进度行"而影响取回本身。
    """

    __slots__ = ("id", "cancelled", "_lock", "started_at", "stage", "percent")

    def __init__(self, job_id: str):
        self.id = job_id
        self.cancelled = False
        self.started_at = time.time()
        self.stage = "准备中"
        self.percent: float | None = None
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            self.cancelled = True

    def note(self, stage: str | None = None, percent: float | None = None) -> None:
        with self._lock:
            if stage is not None:
                self.stage = stage
            if percent is not None:
                self.percent = percent

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "stage": self.stage,
                "percent": self.percent,
                "cancelled": self.cancelled,
                "elapsed_s": round(time.time() - self.started_at, 1),
            }


#: 正在跑的作业（job_id → FetchJob）。**只在这一层**，不落盘、不跨进程。
#: 值里唯一会被别处读到的是 `cancelled`，所以用锁保护的那几个字段足够了。
_JOBS: dict[str, FetchJob] = {}
_JOBS_LOCK = threading.Lock()


def new_job(job_id: str | None) -> FetchJob:
    """登记一张作业票。同一个 id 重复登记返回**同一个**对象。

    ★ "可重复"是刻意的：前端可能在取消请求之后再重试一次同一个 id，
    若这里换了个新对象，那次取消就落在了**已经被丢弃的旧票**上 —— 看起来像
    "取消按钮偶尔不灵"，而且只在慢请求上偶现。
    """
    key = (job_id or "").strip()
    if not key:
        key = f"anon-{int(time.time() * 1000)}-{os.getpid()}"
    with _JOBS_LOCK:
        job = _JOBS.get(key)
        if job is None:
            job = FetchJob(key)
            _JOBS[key] = job
        return job


def job_of(job_id: str) -> FetchJob | None:
    with _JOBS_LOCK:
        return _JOBS.get((job_id or "").strip())


def cancel_job(job_id: str) -> bool:
    """喊停一张作业票。返回"找到没找到" —— 找不到就是跑完了（或 id 不对）。"""
    job = job_of(job_id)
    if job is None:
        return False
    job.cancel()
    return True


def forget(job: FetchJob | None) -> None:
    """作业结束后把它从表里摘掉（**只摘自己那张**，别误伤同名的后来者）。"""
    if job is None:
        return
    with _JOBS_LOCK:
        if _JOBS.get(job.id) is job:
            _JOBS.pop(job.id, None)


def _candidates() -> list[Path]:
    """列出可能放 yt-dlp 的路径，按可信度排序。"""
    out: list[Path] = []
    env = os.environ.get(ENV_VAR)
    if env:
        out.append(Path(env))
    # PATH（含 .exe / 无扩展名两种写法，Windows 上都有）
    for name in ("yt-dlp.exe", "yt-dlp"):
        found = shutil.which(name)
        if found:
            out.append(Path(found))
    # 与项目同级的常见落点（用户"丢进任意文件夹"的典型位置）
    root = Path(__file__).resolve().parents[2]
    out.extend([root / "tools" / "yt-dlp.exe", root / "yt-dlp.exe"])
    return out


def locate() -> Path | None:
    """找 yt-dlp。找不到返回 `None`（调用方负责给"怎么装"的话）。"""
    for path in _candidates():
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _host_of(url: str) -> str:
    parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower()


def _site_of(host: str) -> str | None:
    """主机名 → 站点显示名。命中不了返回 `None`（调用方负责报"暂不支持"）。

    顺序：精确表 → 域名族。族匹配必须用 `endswith("." + 域)`，理由见
    `_SUPPORTED_FAMILIES` 的注释（点号锚定 vs 子域伪装）。
    """
    if host in _SUPPORTED_HOSTS:
        return _SUPPORTED_HOSTS[host]
    for domain, name in _SUPPORTED_FAMILIES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def _all_site_names() -> list[str]:
    """报错文案里"目前只放了……"那句用的站点名。"""
    return sorted(set(_SUPPORTED_HOSTS.values()) | set(_SUPPORTED_FAMILIES.values()))


def _check_supported(url: str) -> str:
    """站点必须属于白名单，且必须是 http(s)。返回站点显示名。"""
    if not isinstance(url, str) or not url.strip():
        raise YtdlpError("请先粘一条歌曲链接")
    raw = url.strip()
    scheme = urllib.parse.urlparse(raw if "://" in raw else f"https://{raw}").scheme
    if scheme not in ("http", "https"):
        raise YtdlpError("只支持 http/https 链接")
    host = _host_of(raw)
    if not host:
        raise YtdlpError("这条链接看不出站点，请粘完整的分享链接")
    site = _site_of(host)
    if site is None:
        names = "、".join(_all_site_names())
        raise YtdlpError(f"暂不支持 {host}。目前只放了：{names}。其它来源请用「翻唱」页的上传或粘直链。")
    return site


def _site_list() -> list[dict]:
    """给前端展示的"支持哪些站点"（按站点名去重，取一个示例域名）。

    注意元组顺序：`_SUPPORTED_HOSTS` 是 `{host: name}`，去重后要输出
    `{name: host}` —— 搞反了前端会显示"music.163.com：网易云音乐"这种倒装。

    域名族优先当示例：`y.qq.com` 比 `c6.y.qq.com` 更像"这个平台的地址"，
    而用户要粘的可能是短链、可能是 App 内的分享页，给个主干域名才不会误导。
    """
    seen: dict[str, str] = {}
    for domain, name in _SUPPORTED_FAMILIES.items():
        seen.setdefault(name, domain)
    for host, name in _SUPPORTED_HOSTS.items():
        seen.setdefault(name, host)
    return [{"name": name, "example": host} for name, host in sorted(seen.items())]


def _find_qq_songmid(parsed: urllib.parse.ParseResult) -> str | None:
    """从一条 QQ 音乐链接里抠 `songmid`，抠不出返回 `None`。

    要认的形态（都是实测见过的）：
        · `/n/ryqq/songDetail/<mid>`          规范页
        · `/n/ryqq_v2/songDetail/<mid>`       **新版**规范页 —— yt-dlp 恰恰不认这个
        · `/n/yqq/song/<mid>.html`            老页
        · `?songmid=<mid>`                    `i.y.qq.com/v8/playsong.html?songmid=…`
        · `/#/songDetail/<mid>`               App 内分享的 hash 形式
    """
    # 路径与 fragment 都可能是 `…/songDetail/<mid>`，同一条正则扫两遍
    for blob in (parsed.path, parsed.fragment):
        if not blob:
            continue
        for m in re.finditer(r"/(?:songDetail|song)/([^/?#]+)", blob):
            mid = m.group(1)
            if mid.endswith(".html"):
                mid = mid[: -len(".html")]
            if _QQ_MID_RE.match(mid):
                return mid
    q = urllib.parse.parse_qs(parsed.query)
    for key in ("songmid", "songMid", "song_id"):
        v = (q.get(key) or [""])[0]
        if _QQ_MID_RE.match(v):
            return v
    return None


def _find_netease_id(parsed: urllib.parse.ParseResult) -> str | None:
    """从一条网易云链接里抠歌曲 id。

    网易云的 id 藏在各种地方：`/song?id=1`、`/#/song?id=1`（hash 路由）、
    `/m/song/1` … 所以把 path/query/fragment 拼成一串再扫 `id=`，
    比逐种形态写分支耐改版。
    """
    blob = f"{parsed.path}?{parsed.query}#{parsed.fragment}"
    m = re.search(r"[?&#/]id=(\d{1,20})", blob)
    if m and _NETEASE_ID_RE.match(m.group(1)):
        return m.group(1)
    return None


def _canonical_for(host: str, url: str) -> str | None:
    """把一条**已知站点**的链接改写成 yt-dlp 一定认的规范地址；做不到返回 `None`。

    只做"抠出稳定 ID → 用 ID 拼官方规范地址"，不碰任何平台接口。
    """
    parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")

    if host == "y.qq.com" or host.endswith(".y.qq.com"):
        mid = _find_qq_songmid(parsed)
        if mid:
            # 必须落回 y.qq.com 主干 + 旧路径：新路径 `ryqq_v2` 实测 yt-dlp 不认。
            return f"https://y.qq.com/n/ryqq/songDetail/{mid}"

    if host == "music.163.com" or host.endswith(".music.163.com"):
        song_id = _find_netease_id(parsed)
        if song_id:
            return f"https://music.163.com/song?id={song_id}"

    return None


def _resolve_redirect(url: str) -> str:
    """跟着短链跳到最终地址，只用于**抠 ID**。

    ★ 返回值**不会**被直接交给 yt-dlp —— 调用方只会拿它去 `_canonical_for` 抠 ID，
    再用抠到的 ID 拼一条自己构造的地址。所以"跳到一个坏地方"最多让规范化失效，
    不会变成"让 yt-dlp 去访问那个坏地方"。

    `stream=True` + 立即 close：只要响应头里的最终地址，正文一个字节都不要。
    """
    with requests.Session() as s:
        s.max_redirects = _MAX_REDIRECTS
        r = s.get(
            url,
            timeout=_RESOLVE_TIMEOUT_S,
            allow_redirects=True,
            stream=True,
            headers={"User-Agent": _RESOLVE_UA},
        )
        try:
            return r.url or url
        finally:
            r.close()


def normalize(url: str) -> str:
    """把分享链接改写成 yt-dlp 认得出的规范地址。**尽力而为，失败就原样返回。**

    为什么是"尽力而为"而不是"硬失败"：这一步只是提高命中率，不是关卡。
    b23.tv 这类短链 yt-dlp 自己就会跳，硬失败反而会把本来能用的链接挡掉。

    为什么要有这一步：App 分享出来的链接和 yt-dlp 认的地址经常不是同一个
    （QQ 音乐那条 302 三级跳的案例见模块 docstring）。这类"没反应"最容易被
    误判成版权问题 —— 其实只是地址形式不对。

    安全：只在**入参 host 已过白名单**的前提下才可能去解短链（调用方 `fetch`
    的顺序保证了这点），且跳转目标还要再验一次站点。
    """
    host = _host_of(url)
    canon = _canonical_for(host, url)
    if canon:
        return canon

    # 本地抠不出 ID 才去解短链 —— 这一步会发一次 HTTP，所以排在后面
    try:
        final = _resolve_redirect(url)
    except Exception:
        # 网络/超时/跳数超限……都不该让整次拉取失败：原样交给 yt-dlp 自己认。
        return url
    final_host = _host_of(final)
    if final_host == host or _site_of(final_host) is None:
        # 没跳（或跳到名单外）：不采信跳转结果，原样交出去。
        return url
    return _canonical_for(final_host, final) or url


def _cookie_args() -> list[str]:
    """登录态参数。**默认返回空列表** —— 不设环境变量就发匿名请求。

    优先级：显式 cookies.txt 文件 > 浏览器登录态。两个都没配 → 匿名。
    文件配了却不存在 → 直接报错（用户是**故意**配的，静默忽略会让人以为配好了）。
    """
    f = (os.environ.get(ENV_COOKIES) or "").strip()
    if f:
        p = Path(f).expanduser()
        if not p.is_file():
            raise YtdlpError(
                f"{ENV_COOKIES} 指向的 cookies 文件不存在：{f}。"
                "它应该是一个 Netscape 格式的 cookies.txt（用浏览器扩展导出的那种）。"
            )
        return ["--cookies", str(p)]
    b = (os.environ.get(ENV_COOKIES_BROWSER) or "").strip()
    if b:
        return ["--cookies-from-browser", b]
    return []


def cookie_status() -> dict:
    """当前登录态配置（给前端/排查用）。**不读任何文件内容，只看配了没。**"""
    f = (os.environ.get(ENV_COOKIES) or "").strip()
    b = (os.environ.get(ENV_COOKIES_BROWSER) or "").strip()
    if f:
        return {"mode": "file", "detail": f, "ok": Path(f).expanduser().is_file()}
    if b:
        return {"mode": "browser", "detail": b, "ok": True}
    return {"mode": "none", "detail": "", "ok": True}


def probe() -> dict:
    """只读状态：yt-dlp 在不在、什么版本、支持哪些站点。**不跑网络。**"""
    path = locate()
    if path is None:
        return {
            "available": False,
            "path": "",
            "version": "",
            "sites": _site_list(),
            "cookie": cookie_status(),
            "hint": (
                f"没找到 yt-dlp。装好之后用环境变量 {ENV_VAR} 指定它的完整路径"
                "（或直接放进 PATH），然后重启后端。"
            ),
        }
    version = ""
    try:
        # `--version` 是纯本地输出，不发网络请求 —— 拿它做探活是安全的
        r = subprocess.run(  # noqa: S603 —— 路径来自白名单式的本地探测，非用户输入
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        version = (r.stdout or "").strip().splitlines()[0] if r.stdout else ""
    except (OSError, subprocess.SubprocessError):
        version = ""
    return {
        "available": True,
        "path": str(path),
        "version": version,
        "sites": _site_list(),
        "cookie": cookie_status(),
        "hint": "",
    }


def _kill_tree(proc: subprocess.Popen) -> None:
    """按**进程树**中止 `proc`。

    ★ 只 `proc.kill()` 是不够的：yt-dlp 会把 ffmpeg 拉成自己的子进程，杀了父亲
    不管儿子 —— 孤儿 ffmpeg 会继续往会话目录里写它那份转码产物，随后 `_cleanup`
    的 `unlink` 撞上被占用的文件**静默失败**（`except OSError: pass`）。
    表现是"取消了，但文件还在长"。

    Windows 用 `taskkill /T`（按父子链遍历），POSIX 用进程组。
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, check=False, timeout=15,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass  # 杀不动就退回到 kill()，别在收尾路径上再抛一个错盖住真正的原因
    try:
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _note_progress(job: FetchJob | None, line: str) -> None:
    """从 yt-dlp 的输出里抽一眼进度。**纯装饰**：抽不到就算了，不许影响取回。"""
    if job is None:
        return
    m = _PROGRESS_RE.search(line)
    if m:
        try:
            job.note(stage="下载中", percent=float(m.group(1)))
        except ValueError:  # 理论到不了（正则只放数字/点），但展示字段不值得抛
            pass
        return
    if "ExtractAudio" in line or "Extracting audio" in line:
        job.note(stage="转码中")
    elif "Extracting URL" in line or "Downloading webpage" in line or "Downloading API" in line:
        job.note(stage="解析页面")
    elif line.startswith("[") and "] " in line and "Deleting original" in line:
        job.note(stage="收尾")


def _run_tool(
    cmd: list[str],
    timeout_s: int | None,
    job: FetchJob | None,
    idle_s: float | None = None,
) -> tuple[int, str]:
    """跑 yt-dlp 并盯住它，返回 `(退出码, 合并后的全部输出)`。

    三条停止条件，任一满足即**按进程树**中止并抛 `YtdlpError`：
      · 作业被取消（用户点了取消 / 前端关了页面）
      · 连续 `idle_s` 秒无输出（卡住）
      · 超过 `timeout_s`（墙钟总上限）

    `idle_s` 默认取模块级的 `_IDLE_TIMEOUT_S`（**在函数体里取**，不是写在默认值上
    —— 写成默认值会是"定义时求值"，之后调参/打桩都改不动它）。`timeout_s` 同理：
    收 `None` 时在函数体里落到 `_TIMEOUT_S`。

    stderr 并进 stdout 读：yt-dlp 的进度和报错交替刷屏，两条流分开读要额外开一个
    线程，而合并后 `_explain` 照样能用关键词归因（反正是给"人话"用的，不是给机器解析的）。
    读取放独立线程 —— 主线程要留着一个能"到点就动手"的位置，阻塞在 readline 上就没法杀了。
    """
    if idle_s is None:
        idle_s = _IDLE_TIMEOUT_S
    if timeout_s is None:
        timeout_s = _TIMEOUT_S  # 同上：在函数体里取，别写进默认值
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        # 独立进程组：不让 Ctrl+C 之类的信号顺手带走它；taskkill /T 照样认父子链
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True  # 给 killpg 一个自己的进程组
    try:
        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 —— exe 来自本地探测；参数已过白名单/由本模块拼出
    except OSError as exc:
        raise YtdlpError(f"启动 yt-dlp 失败：{exc}") from exc

    state = {"last": time.monotonic(), "lines": []}

    def pump() -> None:
        try:
            for raw in proc.stdout or ():
                state["last"] = time.monotonic()
                line = raw.rstrip("\r\n")
                state["lines"].append(line)
                _note_progress(job, line)
        except (OSError, ValueError):
            pass  # 管道被关（进程被杀）时正常收场

    reader = threading.Thread(target=pump, name="ytdlp-reader", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout_s
    try:
        while proc.poll() is None:
            if job is not None and job.cancelled:
                _kill_tree(proc)
                raise YtdlpError("已取消取回（yt-dlp 已中止，临时文件在收尾清理）")
            now = time.monotonic()
            if now - state["last"] > idle_s:
                _kill_tree(proc)
                raise YtdlpError(
                    f"yt-dlp 连续 {int(idle_s)} 秒没有任何输出，判定卡住了，已中止。"
                    "换条链接再试，或用「翻唱」页的上传 / 粘直链。"
                )
            if now > deadline:
                _kill_tree(proc)
                raise YtdlpError(f"yt-dlp 超过 {timeout_s} 秒没拉完，已中止。网络慢或这首歌太大。")
            time.sleep(_POLL_S)
    finally:
        # 先把输出收干净再决定结果 —— 不然失败原因那一行可能还在管道里没读到
        reader.join(timeout=3)
        if proc.stdout is not None:
            proc.stdout.close()
    return proc.returncode or 0, "\n".join(state["lines"])


def _tail_line(text: str) -> str:
    """取"最有信息量的最后一行"。进度行会把真正的报错挤到上面去，所以跳过它们。"""
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s and not _PROGRESS_RE.search(s):
            return s
    return ""


_STEM_LOCK = threading.Lock()
_LAST_STEM_MS = 0


def _new_stem() -> str:
    """给本次取回造一个**唯一**的产物前缀。

    ★ 为什么不能只用 `int(time.time())`（秒级）：`stem` 不只是文件名，它同时是
    `_probe_audio_ext` 的**过滤键**与 `_cleanup` 的**删除键**。两次取回落在同一秒
    （同一首歌连点两次、或两个标签页同时扒）就会共用前缀，于是：
      · 各自 `_probe_audio_ext` 会**看到对方的产物**，多个时取最大的那个 ——
        用户拿到的是另一条链接的音频，而且它确实是个合法音频，不报错；
      · 先跑完的那次 `_cleanup` 会把**后跑那次的半截文件**按前缀一起删掉，
        表现成"另一个窗口莫名其妙失败了"。
    秒级精度在人工操作下不算小概率（连点两下就在同一秒内），所以必须唯一。

    不用 uuid：它要进文件名，短一点更便于排查。毫秒 + **同毫秒内的进程内序号**
    就够 —— 同一进程里两次调用不可能拿到同一个值（锁 + 单调递增计数）。
    """
    global _LAST_STEM_MS
    with _STEM_LOCK:
        ms = int(time.time() * 1000)
        if ms <= _LAST_STEM_MS:  # 同毫秒（或时钟回拨）：顺延，保证单调
            ms = _LAST_STEM_MS + 1
        _LAST_STEM_MS = ms
    return f"ytdlp_{ms}"


def _probe_audio_ext(path: Path, stem: str) -> str | None:
    """在会话目录里找出**本次**产出的音频文件。多个时取最大的（合并产物）。

    ★ 必须按 `stem` 前缀过滤，不能扫全目录。会话目录里可能躺着上一次刚扒好的
    另一首歌 —— 扫全目录的话，"这次什么都没产出"会被上一次的残留伪装成成功，
    用户拿到的是**上一首**（而且他多半不会发现）。多首歌连着扒时这个 bug 必现。
    """
    if not path.exists():
        return None
    cands = [
        p
        for p in path.iterdir()
        if p.is_file() and p.name.startswith(stem) and p.suffix.lower() in _AUDIO_SUFFIXES
    ]
    if not cands:
        return None
    return str(max(cands, key=lambda p: p.stat().st_size))


def fetch(url: str, timeout_s: int | None = None, job: FetchJob | None = None) -> dict:
    """把 `url` 对应的音频拉进会话目录，返回与 `url_fetch.fetch_to_session` **同形**的结果。

    同形很重要：上游（`cover_api`）拿它当"这首歌从哪来"的一种答案，
    字段一致就不用改链路。

    为什么不走 `url_fetch`：那个是"自己发 HTTP 的直链下载器"，面对的是已经能直接
    下载的 URL；这里面对的是**平台页面链接**，取回流地址要跑平台的页面逻辑 ——
    那正是 yt-dlp 干的活。两者的护栏口径不同（那边是逐跳校验 IP），不能混。

    `job` 是可选的取消票（见 `FetchJob`）；不传就是"没人在看，跑完为止"——
    CLI 的 `python ytdlp_fetch.py fetch <url>` 就是这种用法。

    `timeout_s=None` 表示取模块级的 `_TIMEOUT_S`。**同样在函数体里取**，理由与
    `_run_tool` 的 `idle_s` 一致：写成默认参数值会在定义时把 600 求值进去，
    之后调参/打桩一律无效（这条踩过第二次了，两处都按同一口径写成 `None`）。
    """
    site = _check_supported(url)
    # 规范化放在白名单之后：只有"站点已放行"的链接才允许去解短链。
    target = normalize(url)

    exe = locate()
    if exe is None:
        raise YtdlpError(
            f"没找到 yt-dlp，拿不到这条链接的音频。装好后用环境变量 {ENV_VAR} 指向它，"
            "或直接放进 PATH，然后重启后端。"
        )

    # 产物路径由 `-P`/`-o` 钉死，不接受 yt-dlp 自己挑地方（"随用随删"的前提）。
    out_dir = session_out.session_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _new_stem()

    cmd = [
        str(exe),
        # 只取音频，转成 mp3；不下载视频流（省带宽，也避免留下用不上的文件）
        "-x",
        "--audio-format", _AUDIO_FORMAT,
        "--audio-quality", "0",
        # ★ 输出落会话目录，文件名固定前缀 —— 复现「会话目录 + 退出即删」口径
        "-P", str(out_dir),
        "-o", f"{stem}.%(ext)s",
        # 单曲语义：粘贴的如果是歌单/合集链接，只取第一条，别把整个歌单拉下来
        *_DENY_EXTRA,
        # 登录态（默认空 = 匿名请求，见 `_cookie_args`）
        *_cookie_args(),
        "--",
        target,
    ]

    if job is not None:
        job.note(stage="启动 yt-dlp")
    rc, output = _run_tool(cmd, timeout_s, job)
    # 从"启动到跑完"这一段里任何一步失败，本轮自己的残渣都要收掉（只按 stem 删）
    if rc != 0:
        _cleanup(out_dir, stem)
        raise YtdlpError(_explain(rc, output, site))

    got = _probe_audio_ext(out_dir, stem)
    if got is None:
        # `.ytdl` 之类也在这里被收掉，别留给下一次
        _cleanup(out_dir, stem)
        raise YtdlpError(_no_audio_message(site))

    src = Path(got)
    # 产物必须真的在会话目录里 —— 别信"它说写在那儿"，实测过 md5sum 那类假绿。
    try:
        src.resolve().relative_to(out_dir.resolve())
    except ValueError as exc:
        raise YtdlpError(f"yt-dlp 把文件写到了会话目录之外（{src}），已拒绝使用") from exc

    size = src.stat().st_size
    if size <= 0:
        _cleanup(out_dir, stem)
        raise YtdlpError("拉回来的文件是空的，可能是这首歌有版权限制（付费曲常见）。")

    return {
        "name": src.name,
        "path": str(src),
        "url": f"/media/session/{src.name}",
        "suffix": src.suffix.lower(),
        "bytes": size,
        "content_type": "audio/mpeg" if src.suffix.lower() == ".mp3" else "audio/*",
        "site": site,
        # 实际交给 yt-dlp 的地址。和用户粘进来的可能不同（短链被规范化过），
        # 排查"为什么这条链接不行"时这一项比任何日志都直接。
        "source_url": target,
    }


def _cleanup(out_dir: Path, stem: str, attempts: int = 6) -> None:
    """失败/取消时收尾：删掉这次留下的半截文件（含 yt-dlp 的中间产物）。

    必须按 `stem` 前缀删，**不能清空整个会话目录** —— 那里面有用户刚下好、
    还没跑翻唱的那首歌。这个函数被调用时是"这次失败了"，不是"清理场地"。

    ★ 为什么要**重试**：超时/取消那条路上，我们刚 `taskkill /T` 掉进程树，
    而 Windows 上被杀进程持有的文件句柄不是立刻释放的 —— 一次 `unlink` 常常
    撞上 `WinError 32`。原来那句 `except OSError: pass` 会把它变成一个
    **静默的假成功**：用户看到"已取消"，会话目录里却躺着一个还在长的半截文件，
    而且它带着本轮的 stem 前缀，下一次 `_probe_audio_ext` 真有可能把它当产物。

    仍然不抛异常：收尾失败不该盖掉真正的原因（那是用户唯一能看懂的那句话）。
    """
    for i in range(max(1, attempts)):
        try:
            left = [
                p for p in out_dir.glob(f"{stem}*")
                if p.is_file() and not _unlink_quietly(p)
            ]
        except OSError:
            return
        if not left:
            return
        time.sleep(0.15 * (i + 1))  # 退避一下再试，给系统时间释放句柄


def _unlink_quietly(p: Path) -> bool:
    """删一个文件，返回"删干净了没"。"""
    try:
        p.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _cookie_hint(site: str) -> str:
    """"要登录态"时的完整指引。按**当前是否已配**给不同的话 —— 已经配了还被拒，
    多半是 cookie 过期，而不是没配。区分开用户才知道下一步做什么。"""
    st = cookie_status()
    if st["mode"] == "file":
        return (
            f"已配了 cookies 文件（{st['detail']}）但 {site} 仍然拒绝，"
            "多半是这个 cookie 过期了 —— 重新导出一份覆盖它，再重启后端。"
        )
    if st["mode"] == "browser":
        return (
            f"已让 yt-dlp 去读 {st['detail']} 的登录态，但没成功。"
            "Windows 上常见两种原因：浏览器还开着（cookie 库被锁）、"
            "或 Chrome ≥ v127 的新加密读不出来。改用导出的 cookies.txt 最稳。"
        )
    return (
        f"{site} 这首歌要给**登录态**才发音频流（免费账号通常就够，不一定要会员）。"
        f"做法：用浏览器扩展把该站点的登录态导出成 cookies.txt，"
        f"再把环境变量 {ENV_COOKIES} 指到那个文件，重启后端。"
        f"想省事也可以设 {ENV_COOKIES_BROWSER}=edge，"
        "但要先把浏览器完全退出（Windows 上 Chrome 那套新加密多半读不了）。"
    )


def _no_audio_message(site: str) -> str:
    """"跑完了但什么都没产出"的人话。

    注意这条和 `rc != 0` 是两回事：这是 yt-dlp **自认为成功**却没落文件。
    真实成因按概率排：登录态被静默跳过（`--no-warnings` 会吞掉提示）、
    付费/独家曲、平台改版让取流拿到空。所以三件事都要提。
    """
    return (
        f"yt-dlp 跑完了但没拿到音频文件。常见三种原因："
        f"① {site} 这首歌要登录态而当前没配（见「要登录」那条的处理办法）；"
        f"② 它是付费/独家曲，拿不到音频流；"
        f"③ 平台改版了，升级一下 yt-dlp（`yt-dlp -U`）再试。"
    )


def _explain(returncode: int, output: str, site: str) -> str:
    """把 yt-dlp 的输出翻译成人话。

    直接甩原始输出给用户是没用的（那是几百行下载日志）。按最常见的几类
    归因，剩下的才回落到最后一行。

    ★ 收的是**合并后**的输出（stdout+stderr 一起）：关键词照样命中，
    但取"最后一行"时要跳过进度行 —— 否则失败现场会显示成
    `[download]  37.2% of ...`，比不显示还糟（见 `_tail_line`）。
    """
    err = (output or "").strip()
    low = err.lower()
    # "registered users" 是 QQ 音乐的原文；"sign in"/"login" 是别家的说法。
    # "cookie" 单列是因为 yt-dlp 的提示句里带 `--cookies-from-browser`。
    if "registered users" in low or "sign in" in low or "login" in low or "cookie" in low:
        return _cookie_hint(site)
    if "unsupported url" in low or "no suitable extractor" in low:
        return (
            f"yt-dlp 认不出这条 {site} 链接。先试**升级**它（`yt-dlp -U`）—— "
            "平台改版后旧版本的 extractor 会失效。若升级还不行，多半是这种页面形式它还没支持，"
            "换成该站点**分享出来的歌曲链接**再试，或到「翻唱」页用上传/粘直链。"
        )
    if "copyright" in low or "not available" in low or "vip" in low:
        return f"{site} 这首有版权/付费限制，拿不到音频流。换一首或换来源。"
    if "403" in err or "forbidden" in low:
        return f"{site} 拒绝了请求（403）。升级 yt-dlp 或配上登录态再试。"
    tail = _tail_line(err) or f"退出码 {returncode}"
    return f"yt-dlp 失败：{tail[:300]}"


def _main(argv: list[str]) -> int:
    """命令行入口（排查用）：`python m2_server/ytdlp_fetch.py probe|resolve|fetch <url>`。"""
    if not argv or argv[0] == "probe":
        print(json.dumps(probe(), ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "resolve" and len(argv) > 1:
        # 只做白名单 + 规范化，**不跑 yt-dlp、不落盘** —— 排查"链接为什么被拒/没反应"用
        try:
            site = _check_supported(argv[1])
            target = normalize(argv[1])
        except YtdlpError as e:
            print(f"错误：{e}")
            return 1
        print(json.dumps({"site": site, "given": argv[1], "to_ytdlp": target}, ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "fetch" and len(argv) > 1:
        try:
            print(json.dumps(fetch(argv[1]), ensure_ascii=False, indent=2))
        except YtdlpError as e:
            print(f"错误：{e}")
            return 1
        return 0
    print("用法：ytdlp_fetch.py [probe | resolve <url> | fetch <url>]")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
