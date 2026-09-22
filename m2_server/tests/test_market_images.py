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
    (cache / "v.jpg").write_bytes(b"remote")  # 缓存层出现后优先
    assert mi.local_image_path("v").read_bytes() == b"remote"
    assert mi.local_image_path("missing") is None
    assert mi.local_image_path("") is None


def test_image_url_shape(img_dirs):
    """URL 形状：voice_id 归一化小写 + 带内容指纹（?v=size-mtime）。"""
    mi, cache, _ = img_dirs
    (cache / "kiki.png").write_bytes(b"x")
    url = mi.image_url("KIKI")
    base, _, stamp = url.partition("?")
    assert base == "/api/market/image/kiki"          # voice_id 归一化小写
    assert stamp.startswith("v=") and stamp != "v="  # 指纹非空
    assert mi.image_url("ghost") is None


def test_image_url_stamp_changes_with_content(img_dirs):
    """★ 内容一变，指纹必须跟着变。

    这是"远程图库换图能立刻可见"的唯一保障：配图端点带
    `Cache-Control: max-age=86400`，URL 不含指纹的话，作者推了新图、
    客户端也同步到了，Chromium 仍会拿旧图顶 24h（端口固定 → 源不变）。
    """
    mi, cache, _ = img_dirs
    f = cache / "kiki.png"
    f.write_bytes(b"old-content")
    first = mi.image_url("kiki")
    f.write_bytes(b"new-content-much-longer")   # 换图（大小与 mtime 都变）
    second = mi.image_url("kiki")
    assert first != second, "换图后 URL 未变 → 浏览器会继续用 24h 缓存的旧图"
    # 内容不变则 URL 稳定，24h 缓存照旧生效（不白白重复传输）
    assert mi.image_url("kiki") == second


def test_sync_remote_disabled(img_dirs, monkeypatch):
    mi, _, _ = img_dirs
    monkeypatch.setattr(mi, "_REPO", "")
    assert mi.sync_remote() == {"enabled": False}


def test_sync_remote_downloads_and_ttls(img_dirs, monkeypatch):
    """清单 revision 变更 → 下载新图；TTL 内/revision 未变 → 不重复下载。"""
    mi, cache, _ = img_dirs
    monkeypatch.setattr(mi, "_REPO", "u/r")
    manifest = {
        "revision": "r1",
        "imgs": {"lanyangyang": "imgs/lanyangyang.png", "bad..name": "imgs/x.png"},
    }
    calls: list[str] = []

    def fake_get(url, timeout=None):
        calls.append(url)
        if url.endswith("images.json"):
            return json.dumps(manifest).encode()
        return b"PNGDATA"

    monkeypatch.setattr(mi, "_http_get", fake_get)
    st = mi.sync_remote(force=True)
    assert st["ok"] is True and st["downloaded"] == 1  # 非法名被白名单拦掉
    assert (cache / "lanyangyang.png").read_bytes() == b"PNGDATA"
    assert (cache / ".revision").read_text() == "r1"
    n1 = len(calls)

    assert mi.sync_remote().get("skipped") == "ttl"  # TTL 内直接跳过
    assert len(calls) == n1

    st3 = mi.sync_remote(force=True)  # revision 未变：拉清单但不下载
    assert st3["ok"] is True and st3["downloaded"] == 0
    assert len(calls) == n1 + 1

    manifest["revision"] = "r2"  # revision 变更 → 重新下载覆盖
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


# ------------------------------------------------ 补拉（2026-09-22 新增）
#
# 同步的三种触发条件**作用不同**，各有专门用例钉住：
#   ① revision 变了           → 全量重下（换图常常不改文件名！）
#   ② revision 没变 + 本地缺   → 只补缺的（上次下到一半的自愈）
#   ③ revision 没变 + 不缺     → 快速返回，一张都不请求


def test_sync_repairs_partial_download(img_dirs, monkeypatch):
    """★ 核心回归：上次"下到一半"，revision 已写入 —— 下次必须**自动补上**缺的那几张。

    这是 2026-09-18 实测的现场（安装版缓存长期停在 12/30 张）：下载循环里
    单个文件失败是 `continue` 跳过，但循环结束后**仍然写入新 revision** →
    下次 revision 相同就整体 return → 缺的文件永远补不上。
    而因为 `local_image_path()` 是缓存优先，其余静默回退到打包图，
    表面上完全看不出来。

    把 `todo = entries if revision_changed else _missing(entries)`
    改成 `todo = entries if revision_changed else []` 本用例会红。
    """
    mi, cache, _ = img_dirs
    monkeypatch.setattr(mi, "_REPO", "u/r")
    manifest = {
        "revision": "r1",
        "imgs": {"a": "imgs/a.png", "b": "imgs/b.png", "c": "imgs/c.png"},
    }
    broken = {"b.png", "c.png"}   # 模拟网络抖动：这两张下不动

    def fake_get(url, timeout=None):
        if url.endswith("images.json"):
            return json.dumps(manifest).encode()
        return None if url.rsplit("/", 1)[-1] in broken else b"DATA"

    monkeypatch.setattr(mi, "_http_get", fake_get)

    st1 = mi.sync_remote(force=True)
    assert st1["downloaded"] == 1 and st1["failed"] == 2
    assert st1["ok"] is False, "没同步完整就该如实报 False"
    assert sorted(p.name for p in cache.glob("*.png")) == ["a.png"]

    broken.clear()   # 网络恢复 → 下一次同步必须自愈
    st2 = mi.sync_remote(force=True)
    assert st2["downloaded"] == 2, "缺的两张没被补上（这正是 12/30 张那个 bug）"
    assert st2["failed"] == 0 and st2["ok"] is True
    assert sorted(p.name for p in cache.glob("*.png")) == ["a.png", "b.png", "c.png"]


def test_sync_redownloads_all_on_revision_change(img_dirs, monkeypatch):
    """★ revision 变了 → **全量重下**，即使本地文件都在。

    为什么不能优化成"本地已有就跳过"：作者换图**常常不改文件名**
    （改的是同一张 `diyin.png` 的内容）。若按存在性跳过，同名新图永远
    盖不掉本地旧文件 → 用户端"换图"看起来完全没生效。
    把 `todo = entries if revision_changed else _missing(entries)`
    改成 `todo = _missing(entries)` 本用例会红。
    """
    mi, cache, _ = img_dirs
    monkeypatch.setattr(mi, "_REPO", "u/r")
    manifest = {"revision": "r1", "imgs": {"a": "imgs/a.png"}}
    payload = {"v": b"OLD"}

    def fake_get(url, timeout=None):
        if url.endswith("images.json"):
            return json.dumps(manifest).encode()
        return payload["v"]

    monkeypatch.setattr(mi, "_http_get", fake_get)

    mi.sync_remote(force=True)
    assert (cache / "a.png").read_bytes() == b"OLD"

    payload["v"] = b"NEW-CONTENT-LONGER"   # 作者换图：文件名不变，只换内容
    manifest["revision"] = "r2"
    st = mi.sync_remote(force=True)
    assert st["downloaded"] == 1, "revision 变了却没重下 → 同名新图永远盖不掉旧文件"
    assert (cache / "a.png").read_bytes() == b"NEW-CONTENT-LONGER"


def test_sync_permanent_failure_retries_only_that_one(img_dirs, monkeypatch):
    """某张图**永久**失败时，每次同步只重试它自己 —— 不能退化成每次全量重下。

    这正是"有失败就不写 revision"那种看似严谨的写法会踩的坑：
    永久失败 → revision 永远写不进去 → 每次同步都当"首次" → 每次都下全部图。
    （所以实现里**照写** revision，让补拉机制接管。）
    """
    mi, cache, _ = img_dirs
    monkeypatch.setattr(mi, "_REPO", "u/r")
    manifest = {
        "revision": "r1",
        "imgs": {f"v{i}": f"imgs/v{i}.png" for i in range(5)},
    }
    calls: list[str] = []

    def fake_get(url, timeout=None):
        calls.append(url)
        if url.endswith("images.json"):
            return json.dumps(manifest).encode()
        return None if url.endswith("v4.png") else b"DATA"   # v4 永久失败

    monkeypatch.setattr(mi, "_http_get", fake_get)

    st1 = mi.sync_remote(force=True)
    assert st1["downloaded"] == 4 and st1["failed"] == 1
    assert (cache / ".revision").read_text() == "r1", "照写 revision 才能走补拉路径"

    calls.clear()
    st2 = mi.sync_remote(force=True)
    tried = {c.rsplit("/", 1)[-1] for c in calls if not c.endswith("images.json")}
    assert tried == {"v4.png"}, f"应只重试失败的那张，实际请求了 {sorted(tried)}"
    assert st2["downloaded"] == 0 and st2["failed"] == 1


def test_sync_partial_failure_error_is_actionable(img_dirs, monkeypatch):
    """部分失败时 error 要提到"补拉" —— 状态是给人看的，得能看出会自愈。"""
    mi, _, _ = img_dirs
    monkeypatch.setattr(mi, "_REPO", "u/r")
    manifest = {"revision": "r1", "imgs": {"a": "imgs/a.png"}}
    monkeypatch.setattr(
        mi, "_http_get",
        lambda url, timeout=None: json.dumps(manifest).encode()
        if url.endswith("images.json") else None,
    )
    st = mi.sync_remote(force=True)
    assert st["failed"] == 1 and st["ok"] is False
    assert "补拉" in st["error"]
