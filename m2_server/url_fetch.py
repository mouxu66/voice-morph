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
    ② 手动跟随重定向，**每一跳重新过 ①** —— 防 302 逃逸到内网；
       探测/分片/兜底这些**自动跟随**的请求统一走 `_safe_get`，用 response hook
       在 requests 跟下一跳之前审落点主机（CDN 302 常见，不能一禁了之）
    ③ 大小上限：写盘过程中实时累计拦，不只信 Content-Length
    ④ 落盘前嗅探文件头：HTML/JSON 直接拒（粘的是网页链接时给明确文案，
       而不是把 200KB 网页交给 ffmpeg 让它报"解码失败"）
    ⑤ 收全才算数：分片每片读完必须正好等于请求区间、合起来正好全文；
       单连接也要与 Content-Length 对上 —— truncate 预填零（分片）和提前收尾
       （单连接）都产出"能放但坏"的成品，两种都不许静默通过

失败一律抛 `FetchError`（消息是给用户看的），调用方只负责转成 HTTP 状态码。
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import socket
import threading
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

#: 下载请求的 UA。Gopeed 那类下载器也是靠自定义 UA 绕过部分站点的 403。
USER_AGENT = "VoiceMorph/1.0 (local)"

#: 分片并行下载。实测（Jamendo mp3，本机宽带）：
#:   单连接 0.23 MB/s → 16 片 0.42 MB/s = **1.83x**
#: 提速有限的原因**不是片数不够，而是出口链路的瓶颈** —— 别靠调大 PARTS 去搏速度，
#: 那样只会更容易被对方限流。真嫌慢应当是换源（或给下载走直连），见 docs。
PARALLEL_PARTS = 16

#: 并发上限。`PARTS` 是"切几片"，`MAX_PARALLEL` 是"同时跑几片"——
#: 分开是为了将来调参时不必同时改两个语义。
MAX_PARALLEL = 8

#: 小于这个大小就不值得分片（多花一轮 Range 探测的往返时间，收益还不如省下的握手）。
#: 一首 3MB 的 mp3 通常够格；几十 KB 的小文件直接单连接。
PARALLEL_MIN_BYTES = 1 * 1024 * 1024

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


_REDIRECT_STATUSES = (301, 302, 303, 307, 308)


def _redirect_guard(resp, *_args, **_kwargs) -> None:
    """requests 跟随重定向**之前**，对每一跳的落点过 `_check_host`。

    CDN 302 很常见，不能 `allow_redirects=False` 一禁了之（探测/分片/兜底三条路
    都要能跟着跳）；但每一跳的落点都必须过"非公网拒"的口径，否则这些**自动跟随**
    的请求会把 302 带进内网（SSRF）。requests 对每个 hop 的响应都会派发一次
    response hook —— 包括重定向响应本身，所以这里能看到每一次跳转的意图。

    ⚠️ 签名里的 `*_args, **_kwargs` 不是摆设：`dispatch_hook` 会把 send 的
    kwargs（timeout/verify/proxies…）一并转给 hook，只收一个位置参数会 TypeError。

    抛 `FetchError` 会从 `requests.get` 里冒出来，与其它护栏同路。
    """
    if resp.status_code not in _REDIRECT_STATUSES:
        return
    loc = resp.headers.get("Location") or ""
    if not loc:
        return
    nxt = urllib.parse.urljoin(resp.url, loc)
    _check_host(urllib.parse.urlparse(nxt).hostname or "")


def _safe_get(url: str, **kw) -> requests.Response:
    """带逐跳重定向护栏的 GET。

    调用方（Range 探测 / 分片 worker / 单连接兜底）自己管不了重定向细节，
    统一走这里：`allow_redirects` 默认 True（CDN 跳转要能跟），并挂上
    `_redirect_guard` 当 response hook。显式传 `allow_redirects=False` 的调用方
    （探测）不受影响 —— hook 照样会看到 3xx，相当于多审一遍。
    """
    kw.setdefault("allow_redirects", True)
    kw["hooks"] = {"response": _redirect_guard}
    return requests.get(url, **kw)


def _fetch_single(resp, tmp: Path, max_bytes: int, first: bytes, expect: int = 0) -> int:
    """单连接写盘（服务端不支持 Range 时的路径，也是分片不可用时的兜底）。

    `expect`（>0 时生效）是**同一个响应**声明的 Content-Length。收不够就是被截断了
    —— 这条路径没有 truncate 零洞，但"提前收尾"照样产出**短而能播**的文件
    （mp3 尾部缺一段，多数播放器照放），与分片那条是同一类静默坏档。
    `expect=0` 表示调用方拿不到可信长度（例如换了条连接、长度来自上一个响应），
    此时**不猜**、跳过校验。

    ⚠️ 有 `Content-Encoding` 时不能比：requests 会自动解压，而 Content-Length
    是**压缩后**的长度，一比就必然假红。音频直链极少 gzip，但"极少"不是"没有"。
    """
    total = 0
    with tmp.open("wb") as f:
        for chunk in (first, *resp.iter_content(CHUNK_SIZE)):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise FetchError(f"文件超过上限 {max_bytes // 1024 // 1024}MB，已中止")
            f.write(chunk)
    if expect and total != expect and not _is_encoded(resp):
        raise FetchError(f"下载不完整（收到 {total} / 应为 {expect} 字节）—— 连接被提前切断，重试一次")
    return total


def _is_encoded(resp) -> bool:
    """响应体是否被 requests 自动解压过（有的话 Content-Length 与落盘字节数无关）。"""
    enc = (resp.headers.get("Content-Encoding") or "").strip().lower()
    return bool(enc) and enc != "identity"


def _probe_range(cur: str, timeout: tuple[float, float]) -> tuple[bool, int]:
    """问服务端支不支持分片：返回 `(支持, 总字节)`。

    只认 `Accept-Ranges: bytes` + 有 `Content-Length`。任何异常都当作**不支持**
    —— 分片只是提速手段，探测失败绝不能让它变成下载失败。
    """
    try:
        resp = _safe_get(
            cur,
            stream=True,
            timeout=timeout,
            allow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"},
        )
    except requests.RequestException:
        return False, 0
    try:
        if resp.status_code != 206:
            return False, 0
        cr = resp.headers.get("Content-Range") or ""
        # `bytes 0-0/4106028` → 取总长
        if "/" not in cr:
            return False, 0
        size = int(cr.rsplit("/", 1)[1])
        return size > 0, size
    except (ValueError, requests.RequestException):
        return False, 0
    finally:
        resp.close()


def _fetch_parallel(
    cur: str,
    tmp: Path,
    size: int,
    max_bytes: int,
    timeout: tuple[float, float],
    parts: int,
) -> int:
    """按 Range 分片并行拉，写进同一个文件。

    护栏口径与单连接路径**完全一致**，只是每条分片自己重过一遍：
      · 每一片都重新 `_check_host`（防 302 逃逸后拿到的地址被拿来分片）；
        请求走 `_safe_get`，自动跟随的重定向也逐跳审主机
      · 每一片都是加 `Range` 的独立请求，服务端若忽略 Range 会回 200 全量 →
        这种片**直接判失败**，不能把全量内容当分片拼进去（会拼出个坏文件）
      · 累计写入实时比对 `max_bytes`
      · ★ 每片读完必须**正好** `want` 字节、全部合起来**正好** `size` ——
        文件是 truncate 预填零的，少收的字节留在盘上就是零洞：ffmpeg 能打开、
        能播，只是中间缺一段（"能放但坏"），所以这里必须硬拦

    并发数取 `min(parts, MAX_PARALLEL)` —— 再高对家用宽带没意义（实测 16 片
    只为单连接 1.83x，瓶颈在出口链路而不在片数），徒增被封风险。

    `parts` 会按实际大小**自动收窄**：文件小于 `parts × 1MB` 时按 1MB 一片切，
    不要为了凑够片数切出几百字节的碎渣（那些片各自一次握手，净亏）。
    """
    _check_host(urllib.parse.urlparse(cur).hostname or "")

    # 按大小收窄片数：别把 2MB 的文件切成 16 片（每片 128KB，握手开销比传输还大）
    parts = max(1, min(parts, size // PARALLEL_MIN_BYTES or 1))
    seg = size // parts + 1
    written = [0] * parts
    lock = threading.Lock()

    def one(i: int) -> None:
        lo = i * seg
        if lo >= size:
            return
        hi = min((i + 1) * seg - 1, size - 1)
        want = hi - lo + 1
        headers = {"User-Agent": USER_AGENT, "Range": f"bytes={lo}-{hi}"}
        try:
            with _safe_get(cur, stream=True, timeout=timeout, headers=headers) as r:
                if r.status_code != 206:
                    # 服务端没按分片给（回 200 全量）—— 拼进去必然坏档
                    raise FetchError("服务端不支持分片下载（Range 请求未生效）")
                with tmp.open("r+b") as f:
                    f.seek(lo)
                    for chunk in r.iter_content(CHUNK_SIZE):
                        if not chunk:
                            continue
                        with lock:
                            written[i] += len(chunk)
                            if sum(written) > max_bytes:
                                raise FetchError(
                                    f"文件超过上限 {max_bytes // 1024 // 1024}MB，已中止"
                                )
                        f.write(chunk)
                    # ★ 收满才能走。文件是 truncate 预填零的，少收的字节会留在
                    # 文件里当**零洞** —— ffmpeg 照样能打开、能播，只是中间缺一段
                    # （"能放但坏"）。服务端提前收尾 / 连接被掐都会走到这里。
                    if written[i] != want:
                        raise FetchError(
                            f"第 {i + 1} 片下载不完整（收到 {written[i]} / 应为 {want} 字节）"
                        )
        except requests.RequestException as exc:
            raise FetchError(f"下载中断：{exc.__class__.__name__}") from exc

    workers = min(parts, MAX_PARALLEL)
    # 先把文件撑到全长：各片用 seek 定位写，文件必须先有这么长，
    # 否则短片会写到文件尾之外（Windows 上表现为写入被丢弃 → 静默坏档）。
    with tmp.open("wb") as f:
        f.truncate(size)

    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(one, i) for i in range(parts)]
        for fut in concurrent.futures.as_completed(futures):
            fut.result()  # 任一片抛错就整体失败（异常会在这里冒出来）

    total = sum(written)
    # 对总账：各片各自验过"正好 want 字节"，这里再确认合起来就是全文。
    # truncate 预填的零洞不会让任何一步报错 —— 少了它就是"能放但坏"的成品。
    if total != size:
        raise FetchError(f"分片下载不完整（共收到 {total} / 应为 {size} 字节）")
    return total


def fetch_to_session(
    url: str,
    prefix: str = "cover_src",
    max_bytes: int = MAX_BYTES,
    timeout: tuple[float, float] = (CONNECT_TIMEOUT, READ_TIMEOUT),
    parallel: bool = True,
) -> dict:
    """下载 `url` 到会话目录，返回 `{name, path, url, suffix, bytes, content_type}`。

    `name` 是裸名（与 `session_out` 的对外口径一致），`url` 是可直接试听的相对地址。
    调用方拿它去跑链路；**跑完由调用方删**（会话产物本来就随退出清空）。

    `parallel=True`（默认）时，若服务端支持 Range 就走多线程分片 ——
    实测同一首 mp3：单连接 0.23 MB/s → 16 片 0.42 MB/s（1.83x）。
    服务端不支持则**静默退回单连接**（提速是优化，不是前提）。
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
                headers={"User-Agent": USER_AGENT},
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
        # 先读个头用来嗅探（分片路径也要先确认"这是音频不是网页"，
        # 否则会拿 16 条分片去下一个 HTML 页面，最后才报错、白跑一轮）
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

        # 试探能否分片。注意这里要给首连接留出路：先关掉再另起分片请求，
        # 免得同一 URL 挂着两条连接互相抢带宽。
        ok, size = (False, 0)
        if parallel and declared > PARALLEL_MIN_BYTES:
            resp.close()
            ok, size = _probe_range(cur, timeout)
            if ok:
                total = _fetch_parallel(cur, tmp, min(size, declared or size), max_bytes, timeout, PARALLEL_PARTS)
            else:
                # 退回单连接：重新发一次普通请求（重定向逐跳过 _redirect_guard）
                resp = _safe_get(cur, stream=True, timeout=timeout, headers={"User-Agent": USER_AGENT})
                first2 = next(resp.iter_content(CHUNK_SIZE), b"")
                # 长度必须取**这条**响应的 —— 上一条是带 Range 的探测链路，
                # 拿它的 Content-Length 去比会假红
                total = _fetch_single(
                    resp, tmp, max_bytes, first2,
                    expect=int(resp.headers.get("Content-Length") or 0),
                )
        else:
            total = _fetch_single(resp, tmp, max_bytes, first, expect=declared)

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
