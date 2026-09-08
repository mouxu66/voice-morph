"""market_images：配图解析优先级与远程图库同步（VM_MARKET_IMG_REPO）。"""
import json

import pytest


@pytest.fixture()
def img_dirs(tmp_path, monkeypatch):
    import market_images as mi
    cache = tmp_path / "cache"
    assets = tmp_path / "assets"
    cache.mkdir()
    assets.mkdir()
    monkeypatch.setattr(mi, "CACHE_DIR", cache)
    monkeypatch.setattr(mi, "ASSET_DIR", assets)
    monkeypatch.setattr(mi, "REVISION_FILE", cache / ".revision")
    return mi, cache, assets


def test_local_image_path_priority(img_dirs):
    """远程缓存图 > 打包图；无效/缺失 → None。"""
    mi, cache, assets = img_dirs
    (assets / "v.png").write_bytes(b"asset")
    assert mi.local_image_path("v").read_bytes() == b"asset"
    (cache / "v.jpg").write_bytes(b"remote")          # 缓存层出现后优先
    assert mi.local_image_path("v").read_bytes() == b"remote"
    assert mi.local_image_path("missing") is None
    assert mi.local_image_path("") is None


def test_image_url_shape(img_dirs):
    mi, cache, _ = img_dirs
    (cache / "kiki.png").write_bytes(b"x")
    assert mi.image_url("KIKI") == "/api/market/image/kiki"   # voice_id 归一化小写
    assert mi.image_url("ghost") is None


def test_sync_remote_disabled(img_dirs, monkeypatch):
    mi, _, _ = img_dirs
    monkeypatch.setattr(mi, "_REPO", "")
    assert mi.sync_remote() == {"enabled": False}


def test_sync_remote_downloads_and_ttls(img_dirs, monkeypatch):
    """清单 revision 变更 → 下载新图；TTL 内/revision 未变 → 不重复下载。"""
    mi, cache, _ = img_dirs
    monkeypatch.setattr(mi, "_REPO", "u/r")
    manifest = {"revision": "r1",
                "imgs": {"lanyangyang": "imgs/lanyangyang.png", "bad..name": "imgs/x.png"}}
    calls: list[str] = []

    def fake_get(url, timeout=None):
        calls.append(url)
        if url.endswith("images.json"):
            return json.dumps(manifest).encode()
        return b"PNGDATA"

    monkeypatch.setattr(mi, "_http_get", fake_get)
    st = mi.sync_remote(force=True)
    assert st["ok"] is True and st["downloaded"] == 1          # 非法名被白名单拦掉
    assert (cache / "lanyangyang.png").read_bytes() == b"PNGDATA"
    assert (cache / ".revision").read_text() == "r1"
    n1 = len(calls)

    assert mi.sync_remote().get("skipped") == "ttl"            # TTL 内直接跳过
    assert len(calls) == n1

    st3 = mi.sync_remote(force=True)                           # revision 未变：拉清单但不下载
    assert st3["ok"] is True and st3["downloaded"] == 0
    assert len(calls) == n1 + 1

    manifest["revision"] = "r2"                                # revision 变更 → 重新下载覆盖
    st4 = mi.sync_remote(force=True)
    assert st4["ok"] is True and st4["downloaded"] == 1
    assert len(calls) == n1 + 3


def test_sync_remote_unreachable_is_safe(img_dirs, monkeypatch):
    """远端不可达：ok=False 不抛异常，旧缓存不受影响。"""
    mi, cache, _ = img_dirs
    (cache / "keep.png").write_bytes(b"old")
    monkeypatch.setattr(mi, "_REPO", "u/r")
    monkeypatch.setattr(mi, "_http_get", lambda url, timeout=None: None)
    st = mi.sync_remote(force=True)
    assert st["ok"] is False and st["error"]
    assert (cache / "keep.png").read_bytes() == b"old"
