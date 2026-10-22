"""URL 域名白名单的唯一实现（2026-10-22 从三份分叉副本收敛到 net_guard）。

判据只留一份的代价是「调用方文案各不相同」，所以这里同时钉两件事：
  · 判据本身（精确域 / 点分隔子域 / 回环 / 协议）；
  · 三个调用方仍按各自异常类型与原有文案报错（`UrlRejected.kind` 的用途）。
"""

import net_guard
import pytest

HOSTS = {"hf.co", "huggingface.co", "opengameart.org"}


# --------------------------------------------------------------- 判据


def test_allows_exact_host_and_point_delimited_subdomains():
    assert net_guard.validate_url("https://huggingface.co/a/b", HOSTS) == "huggingface.co"
    # HF 重定向到 Xet CDN：cas-bridge.xethub.hf.co 属于 hf.co 的点分隔子域
    assert net_guard.validate_url("https://cas-bridge.xethub.hf.co/x?y=1", HOSTS) == "cas-bridge.xethub.hf.co"
    assert net_guard.validate_url("https://www.opengameart.org/a.zip", HOSTS) == "www.opengameart.org"


@pytest.mark.parametrize(
    "url",
    [
        "https://evil-hf.co/a.pth",  # 后缀撞名但没有点分隔
        "https://hf.co.evil.com/a.pth",  # 白名单是别人的前缀
        "https://cas-bridge.xethub.hf.co.evil.com/a.pth",
    ],
)
def test_rejects_lookalike_domains(url):
    with pytest.raises(net_guard.UrlRejected, match="白名单") as ei:
        net_guard.validate_url(url, HOSTS)
    assert ei.value.kind == "host"


def test_host_is_normalized_to_lowercase():
    assert net_guard.validate_url("https://HF.CO/x", HOSTS) == "hf.co"


def test_loopback_only_with_explicit_switch():
    for url in ("http://127.0.0.1:8765/x.bin", "http://localhost:8765/x.bin"):
        with pytest.raises(net_guard.UrlRejected):
            net_guard.validate_url(url, HOSTS)
        assert net_guard.validate_url(url, HOSTS, allow_loopback=True) in {"127.0.0.1", "localhost"}


@pytest.mark.parametrize("url", ["ftp://hf.co/a.pth", "file:///D:/a.pth", "/local/path"])
def test_require_http_rejects_non_http_schemes(url):
    with pytest.raises(net_guard.UrlRejected) as ei:
        net_guard.validate_url(url, HOSTS, require_http=True)
    assert ei.value.kind == "scheme"


def test_require_http_is_opt_in():
    """market / pet 历史上不查协议，参数缺省必须保持原语义。"""
    assert net_guard.validate_url("ftp://hf.co/a.pth", HOSTS) == "hf.co"


def test_unparsable_url_is_invalid_kind():
    with pytest.raises(net_guard.UrlRejected) as ei:
        net_guard.validate_url("https://[::1/x", HOSTS)
    assert ei.value.kind == "invalid"


def test_url_rejected_is_value_error_for_legacy_catchers():
    assert issubclass(net_guard.UrlRejected, ValueError)


# --------------------------------------------------------------- 调用方契约


def test_market_and_pet_keep_their_error_types_and_wording():
    from market_download import MarketError
    from market_download import _validate_url as market_validate
    from pet_market import PetMarketError
    from pet_market import _validate_url as pet_validate

    with pytest.raises(MarketError, match="白名单"):
        market_validate("https://evil.example.com/a.pth")
    with pytest.raises(PetMarketError, match="白名单"):
        pet_validate("https://evil.example.com/a.pth")

    # 回环开关仍只在 market 侧生效
    market_validate("http://127.0.0.1:9/x", allow_loopback=True)
    with pytest.raises(PetMarketError):
        pet_validate("http://127.0.0.1:9/x")


def test_sfx_keeps_its_own_wording_for_scheme_and_parse_errors():
    from sfx_packs import PackError
    from sfx_packs import _validate_url as sfx_validate

    with pytest.raises(PackError, match="协议不支持"):
        sfx_validate("ftp://hf-mirror.com/a.zip")
    with pytest.raises(PackError, match="没法解析"):
        sfx_validate("https://[::1/x")
    # 合法地址原样返回（调用方随后直接拿去请求）
    assert sfx_validate("https://hf-mirror.com/a.zip") == "https://hf-mirror.com/a.zip"
