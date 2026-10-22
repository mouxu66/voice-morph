"""下载源域名白名单校验 —— 全后端唯一实现。

为什么单独成模块（2026-10-22）：`market_download` / `pet_market` / `sfx_packs`
各自写过一份「精确域或子域」判定，三份已经漂了 —— 只有 sfx 检查协议、
只有 market 允许回环。这类安全判据每多一份副本，就多一次「某处忘了补」。

语义差异用参数表达，判据只留一份：
  - `allowed_hosts`：白名单主机集（精确域，或其子域，如 `*.hf.co`）
  - `allow_loopback`：放行 127.0.0.1 / localhost / ::1（本机测试服务器用）
  - `require_http`：只接受 http / https（下载清单等外部输入用）

`UrlRejected.kind` 供调用方翻译自己的文案（`invalid` / `scheme` / `host`），
别靠匹配异常字符串 —— 文案一改就静默失配。
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterable

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class UrlRejected(ValueError):
    """白名单拒绝。是 `ValueError` 的子类，调用方按自己的异常体系包装即可。"""

    def __init__(self, kind: str, url: str, host: str = "", scheme: str = ""):
        self.kind = kind
        self.url = url
        self.host = host
        self.scheme = scheme
        if kind == "invalid":
            msg = f"非法 URL: {url}"
        elif kind == "scheme":
            msg = f"不支持的协议: {scheme or '(空)'}"
        else:
            msg = f"下载域名不在白名单: {host}"
        super().__init__(msg)


def validate_url(
    url: str,
    allowed_hosts: Iterable[str],
    *,
    allow_loopback: bool = False,
    require_http: bool = False,
) -> str:
    """校验 `url` 的主机是否在白名单内 → 返回小写 host；拒绝时抛 `UrlRejected`。

    子域匹配只认「点分隔」：`cas-bridge.xethub.hf.co` 命中 `hf.co`，
    而 `evil-hf.co` / `hf.co.evil.com` 都不会命中（后缀匹配必须带 `.`）。
    """
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
    except ValueError as exc:
        raise UrlRejected("invalid", url) from exc
    if require_http and parsed.scheme not in ("http", "https"):
        raise UrlRejected("scheme", url, host, parsed.scheme)
    if allow_loopback and host in LOOPBACK_HOSTS:
        return host
    for allowed in allowed_hosts:
        if host == allowed or host.endswith("." + allowed):
            return host
    raise UrlRejected("host", url, host)
