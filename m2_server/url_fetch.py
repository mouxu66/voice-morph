"""把一条 http(s) 直链拉进**会话目录**（翻唱「粘直链」用，也可以给别的"给个链接用一次"的地方）。

为什么不新造下载器
------------------
分块流式、逐跳校验、大小上限这套口径与 `market_download.py`（音色市场的权重下载）
完全一致 —— 那边已经在生产里跑了。这里只有两处按需求反过来：

  1. **不设域名白名单，改为"拒内网"**。市场只认 HF / 魔搭（服务端可控）；这里用户
     粘什么域名都可能，所以允许公网、拒绝私网/回环/链路本地/保留段 —— 字面 IP 直接判，
     域名要解析一遍看**所有**地址（防“域名指向 169.254.169.254 云元数据”这类 SSRF）。
  2. **产物落会话目录**（`session_out.new_path`）。翻唱跑完即删：用户下次想的多半是
     另一首歌，留着只是占地方（与"即用即删"同一口径）。

护栏顺序（越早拒越好，拒的时候必须给"人话"）
-------------------------------------------
    ① scheme 只认 http/https；主机禁私网/回环（含 DNS 解析后的地址）
    ② 手动跟随重定向，**每一跳重新过 ①** —— 防 302 逃逸到内网
    ③ 大小上限：写盘过程中实时累计拦，不只信 Content-Length
    ④ 落盘前嗅探文件头：HTML/JSON 直接拒（粘的是网页链接时给明确文案，
       而不是把 200KB 网页交给 ffmpeg 让它报"解码失败"）

失败一律抛 `FetchError`（消息是给用户看的），调用方只负责转成 HTTP 状态码。
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from pathlib import Path

import requests

import session_out

#: 单次下载上限。一首歌的 mp3 通常 3~10MB、无损 30~50MB —— 200MB 足够宽容，
#: 同时挡住"用户把整个视频/音乐合集丢进来"（那些不是这个功能的场景）。
MAX_BYTES = 200 * 1024 * 1024

CONNECT_TIMEOUT = 15
READ_TIMEOUT = 60
CHUNK_SIZE = 256 * 1024
MAX_REDIRECTS = 5

#: URL 后缀白名单（有就直接用，别去猜 Content-Type）。
AUDIO_SUFFIXES = {
    ".mp3", ".m4a", ".m4b", ".aac", ".flac", ".wav", ".ogg", ".opus",
    ".wma", ".webm", ".mp4", ".mkv", ".mov",
}

#: Content-Type → 后缀（直链常常没有扩展名，如 Jamendo 的 `?trackid=…&format=mp32`）。
SUFFIX_BY_TYPE = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/x-flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

#: 一眼就认得出来的容器魔数（比 Content-Type 可信 —— 有些 CDN 一律回 octet-stream）。
MAGIC = (
    (b"fLaC", ".flac"),
    (b"OggS", ".ogg"),
    (b"\x1aE\xdf\xa3", ".webm"),  # EBML（webm/mkv）
)


class FetchError(RuntimeError):
    """下载失败（消息直接面向用户，调用方转成 4xx/5xx 即可）。"""


def _ip_forbidden(ip: ipaddress._BaseAddress) -> bool:
    """非公网地址一律不许 —— 私网/回环/链路本地/保留段/CGNAT 都在 `is_global` 之外。"""
    return not ip.is_global


def _check_host(host: str) -> None:
    """主机名要么是公网字面 IP，要么解析出的**所有**地址都是公网。"""
    if not host:
        raise FetchError("链接里没有主机名")
    bare = host.strip("[]").lower()  # IPv6 字面量可能带方括号
    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        ip = None
    if ip is not None:
        if _ip_forbidden(ip):
            raise FetchError(f"这个地址是本机/内网地址（{host}），不接受")
        return

    if bare == "localhost" or bare.endswith(".local") or bare.endswith(".internal"):
        raise FetchError(f"这个地址是本机/内网地址（{host}），不接受")
    try:
        infos = socket.getaddrinfo(bare, None)
    except socket.gaierror as exc:
        raise FetchError(f"域名解析不了：{host}（{exc}）") from exc
    addrs = {info[4][0] for info in infos}
    bad = sorted(a for a in addrs if _ip_forbidden(ipaddress.ip_address(a)))
    if bad:
        raise FetchError(f"域名 {host} 指向内网地址（{bad[0]}），不接受")
    if not addrs:
        raise FetchError(f"域名解析不出地址：{host}")


def validate_url(url: str) -> str:
    """校验并返回去掉首尾空白的 URL；不合规抛 `FetchError`。"""
    u = (url or "").strip()
    if not u:
        raise FetchError("请先粘贴链接")
    try:
        parts = urllib.parse.urlparse(u)
    except ValueError as exc:
        raise FetchError(f"链接格式不对：{u[:120]}") from exc
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"只支持 http/https 链接（收到 {parts.scheme or '空'}）")
    _check_host(parts.hostname or "")
    return u


def _looks_like_text(head: bytes, content_type: str) -> bool:
    """是不是"网页/接口响应"而不是音频。粘错链接时靠它给一句人话。"""
    ct = content_type.split(";")[0].strip().lower()
    if ct.startswith("text/") or ct in ("application/json", "application/xml"):
        return True
    body = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    return body[:1] in (b"<", b"{", b"[")


def _suffix_of(head: bytes, url: str, content_type: str) -> str:
    """定后缀：魔数 → URL 扩展名 → Content-Type → audio/video 兜底 `.bin`。"""
    for magic, ext in MAGIC:
        if head.startswith(magic):
            return ext
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return ".wav"
    if head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return ".mp3"
    if len(head) > 11 and head[4:8] == b"ftyp":
        return ".m4a"

    ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
    if ext in AUDIO_SUFFIXES:
        return ext
    ct = content_type.split(";")[0].strip().lower()
    if ct in SUFFIX_BY_TYPE:
        return SUFFIX_BY_TYPE[ct]
    if ct.startswith(("audio/", "video/")):
        return ".bin"  # 让 ffmpeg 自己嗅探；浏览器试听可能不支持，但链路能跑
    raise FetchError(
        f"这不是音频文件（Content-Type: {ct or '未知'}）—— "
        "粘的可能是网页链接；需要网页里的音频直链（.mp3/.m4a/.flac/.wav 等）"
    )


def _http_error(status: int) -> FetchError:
    if status == 401:
        return FetchError("这个链接需要登录（401），换个公开直链")
    if status == 403:
        return FetchError("下载被拒绝（403）—— 常见于带签名的链接已过期，重新复制一条新的直链")
    if status == 404:
        return FetchError("链接不存在（404）")
    if status == 429:
        return FetchError("对方限流了（429），过一会儿再试")
    return FetchError(f"下载失败（HTTP {status}）")


def fetch_to_session(
    url: str,
    prefix: str = "cover_src",
    max_bytes: int = MAX_BYTES,
    timeout: tuple[float, float] = (CONNECT_TIMEOUT, READ_TIMEOUT),
) -> dict:
    """下载 `url` 到会话目录，返回 `{name, path, url, suffix, bytes, content_type}`。

    `name` 是裸名（与 `session_out` 的对外口径一致），`url` 是可直接试听的相对地址。
    调用方拿它去跑链路；**跑完由调用方删**（会话产物本来就随退出清空）。
    """
    cur = validate_url(url)
    redirects = 0
    while True:
        _check_host(urllib.parse.urlparse(cur).hostname or "")  # 每一跳都重判
        try:
            resp = requests.get(
                cur,
                stream=True,
                timeout=timeout,
                allow_redirects=False,
                headers={"User-Agent": "VoiceMorph/1.0 (local)"},
            )
        except requests.RequestException as exc:
            raise FetchError(f"连不上：{exc.__class__.__name__}") from exc
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            resp.close()
            if not loc:
                raise FetchError("对方回了个没有目标的重定向")
            cur = urllib.parse.urljoin(cur, loc)
            redirects += 1
            if redirects > MAX_REDIRECTS:
                raise FetchError(f"重定向次数太多（>{MAX_REDIRECTS}）")
            continue
        if resp.status_code >= 400:
            resp.close()
            raise _http_error(resp.status_code)
        break

    content_type = resp.headers.get("Content-Type") or ""
    declared = int(resp.headers.get("Content-Length") or 0)
    if declared > max_bytes:
        resp.close()
        raise FetchError(f"文件太大（{declared // 1024 // 1024}MB，上限 {max_bytes // 1024 // 1024}MB）")

    tmp: Path | None = None
    try:
        first = next(resp.iter_content(CHUNK_SIZE), b"")
        if not first:
            raise FetchError("对方没有返回任何内容")
        if _looks_like_text(first, content_type):
            raise FetchError(
                f"下到的是网页不是音频（Content-Type: {content_type or '未知'}）—— "
                "需要网页里的音频直链"
            )
        suffix = _suffix_of(first, cur, content_type)

        dst = session_out.new_path(prefix, suffix)
        tmp = dst.with_suffix(dst.suffix + ".part")
        total = 0
        with tmp.open("wb") as f:
            for chunk in (first, *resp.iter_content(CHUNK_SIZE)):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError(f"文件超过上限 {max_bytes // 1024 // 1024}MB，已中止")
                f.write(chunk)
        tmp.replace(dst)
    except requests.RequestException as exc:
        raise FetchError(f"下载中断：{exc.__class__.__name__}") from exc
    finally:
        resp.close()
        if tmp is not None:
            tmp.unlink(missing_ok=True)

    return {
        "name": dst.name,
        "path": str(dst),
        "url": session_out.rel_url(dst.name),
        "suffix": dst.suffix,
        "bytes": total,
        "content_type": content_type,
    }
