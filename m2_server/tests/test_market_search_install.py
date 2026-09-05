"""音色市场 搜索 + 推荐清单 + 安装编排 测试。

- 清单/直链构造/搜索解析：纯本地（monkeypatch requests.get，不碰真实网络）
- 安装编排：本地 ThreadingHTTPServer 提供 pth/index，验证落位到模拟 RVC 目录
"""
import http.server
import os
import threading
import time
import urllib.parse

import pytest

from market_download import ALLOWED_HOSTS, DownloadManager, MarketError
from market_install import InstallError, InstallManager
from market_manifest import get_manifest
import market_search
import market_search as ms
import config

PTH_DATA = os.urandom(512 * 1024)          # 512KB 伪权重
IDX_DATA = os.urandom(64 * 1024)           # 64KB 伪索引
MIRROR_DATA = os.urandom(128 * 1024)       # 镜像文件（内容刻意不同）


class _Ctx:
    def __init__(self):
        self.files = {"/v.pth": PTH_DATA, "/v.index": IDX_DATA, "/mirror.pth": MIRROR_DATA}


class RangeHandler(http.server.BaseHTTPRequestHandler):
    ctx = _Ctx()

    def log_message(self, *a):
        pass

    def _serve(self):
        path = self.path.split("?")[0]
        data = self.ctx.files.get(path)
        if data is None:
            self.send_error(404)
            return
        size = len(data)
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{size-1}/{size}")
        else:
            start, body = 0, data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = lambda self: self._serve()      # noqa: E731


@pytest.fixture(scope="module")
def server_url():
    RangeHandler.ctx = _Ctx()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def mgr(tmp_path):
    return DownloadManager(download_dir=tmp_path / "dl",
                           state_file=tmp_path / "dl" / "downloads.json",
                           allow_loopback=True)


@pytest.fixture()
def fake_rvc(tmp_path, monkeypatch):
    rvc = tmp_path / "rvc"
    (rvc / "logs").mkdir(parents=True)
    (rvc / "assets" / "weights").mkdir(parents=True)
    monkeypatch.setattr(config, "RVC_ROOT", rvc)
    return rvc


def _wait_install(ins, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = ins.progress()
        s = (st.get("install") or {}).get("status")
        if s not in ("queued", "downloading_pth", "downloading_index", "staging", None):
            return st
        time.sleep(0.05)
    raise AssertionError(f"install did not finish within timeout: {ins.progress()}")


# ---------------- 精选清单 ----------------
def test_manifest_structure_and_whitelist():
    items = get_manifest()
    assert 5 <= len(items) <= 10
    for it in items:
        assert it["voice_id"] and it["voice_id"].replace("_", "").isalnum()
        assert it["name"] and it["platform"] in ("hf", "modelscope")
        assert it["download"]["url"]
        for key in ("url", "mirror_url"):
            u = it["download"].get(key)
            if u:
                host = urllib.parse.urlparse(u).hostname
                assert host in ALLOWED_HOSTS, f"{key} 不在白名单: {u}"
        if it["platform"] == "hf":
            assert it["index"]["url"], "HF 条目必须带 index"


def test_manifest_both_sources_present():
    platforms = {it["platform"] for it in get_manifest()}
    assert "hf" in platforms and "modelscope" in platforms


# ---------------- 直链构造 ----------------
def test_hf_resolve_url():
    u = ms.hf_resolve("owner/repo", "weights/卡通-懒羊羊.pth", "https://hf-mirror.com")
    assert "owner/repo" in u and "/resolve/main/" in u
    assert urllib.parse.urlparse(u).hostname == "hf-mirror.com"


def test_ms_resolve_url():
    u = ms.ms_resolve("hudddd/Retrieval-based-Voice", "models/kiki/kiki.pth")
    assert "hudddd/Retrieval-based-Voice" in u
    assert "FilePath=models%2Fkiki%2Fkiki.pth" in u or "FilePath=models/kiki/kiki.pth" in u
    assert urllib.parse.urlparse(u).hostname == "modelscope.cn"


# ---------------- 搜索（monkeypatch 掉网络） ----------------
def _fake_get_json(url, **params):
    if "/api/models" in url and params.get("search"):
        return [{"id": "juuxn/rvc-demo", "downloads": 5, "likes": 2, "tags": ["rvc"],
                 "cardData": {"language": ["zh"]}, "lastModified": "2026-01-01T00:00:00"},
                {"id": "other/filler", "downloads": 0, "likes": 0, "tags": [], "lastModified": ""}]
    if "/api/models/juuxn/rvc-demo/tree/main" in url:
        return [{"type": "file", "path": "demo.pth", "size": 100, "lfs": {"sha256": "abc"}},
                {"type": "file", "path": "added.index", "size": 50, "lfs": {}}]
    if "/tree/main" in url:
        return []
    raise AssertionError(f"unexpected request: {url} params={params}")


def test_search_hf_parses_items(monkeypatch, server_url):
    monkeypatch.setattr(ms, "_get_json", _fake_get_json)
    items = ms.search_hf("rvc demo", limit=5)
    assert items and items[0]["repo"] == "juuxn/rvc-demo"
    assert items[0]["files"], "前 3 条应做文件探测"
    assert items[0]["files"][0]["sha256"] == "abc"


def test_hf_desc_zh_helpers():
    """英文元数据 → 中文简介（类型/能力/协议/语言）。"""
    zh = ms._zh_tags(["rvc", "license:mit", "en", "ja", "region:us",
                      "text-generation-inference", "arxiv:2409.123"])
    assert "RVC 变声" in zh and "MIT 协议" in zh and "地区 · 美国" in zh
    assert all("english" != s for s in zh), "语言标签不应进标签数组"
    m = {"pipeline_tag": "audio-to-audio", "tags": ["rvc", "license:mit", "en", "ja"],
         "cardData": {"description": "RVC voice model pack"}}
    d = ms.make_hf_desc(m, zh)
    assert "音频转换" in d and "语言 英语/日语" in d and "RVC voice model pack" in d


def test_search_ms_path_resolution(monkeypatch):
    seen = {}

    def fake(url, **params):
        if "/api/v1/models/" in url and "/repo" not in url:
            return {"Code": 200, "Data": {"Path": "someone/demo-voice",
                                          "Name": "demo-voice", "ChineseName": "演示音色",
                                          "Downloads": 9, "Likes": 1, "Task": "text-to-speech"}}
        if "/repo/files" in url:
            return {"Code": 200, "Data": {"Files": [
                {"Path": "models/demo/demo.pth", "Type": "blob", "Size": 100, "Sha256": "x"}]}}
        raise AssertionError(f"unexpected: {url}")

    monkeypatch.setattr(ms, "_get_json", fake)
    r = ms.search_ms("someone/demo-voice")
    assert r["items"] and r["items"][0]["id"] == "someone/demo-voice"
    assert "note" in r


def test_search_ms_fallback_to_manifest(monkeypatch):
    def boom(url, **params):
        raise MarketError("x")
    monkeypatch.setattr(ms, "_get_json", boom)
    r = ms.search_ms("懒羊羊")
    assert r["items"], "清单过滤应能命中魔搭懒羊羊条目"
    assert all(i["platform"] == "modelscope" for i in r["items"])


def test_search_all_merges(monkeypatch):
    monkeypatch.setattr(ms, "_get_json", _fake_get_json)
    r = ms.search("all", "rvc", limit=4)
    assert r["items"]


# ---------------- 安装编排（端到端本地 HTTP） ----------------
def test_install_full_flow(server_url, mgr, fake_rvc):
    ins = InstallManager(manager=mgr)
    st = ins.run("test_voice", download={"url": f"{server_url}/v.pth"},
                 index={"url": f"{server_url}/v.index"}, display_name="测试音色")
    assert st["install"]["status"] in ("queued", "downloading_pth")
    st = _wait_install(ins)
    assert st["install"]["status"] == "installed", st
    log_dir = fake_rvc / "logs" / "test_voice"
    assert (log_dir / "test_voice.pth").read_bytes() == PTH_DATA
    assert (fake_rvc / "assets" / "weights" / "test_voice.pth").read_bytes() == PTH_DATA
    assert (log_dir / "added_test_voice.index").read_bytes() == IDX_DATA
    assert "test_voice" in ins.installed_ids()
    # 下载任务结束后 install 状态留在 downloads.json
    saved = mgr.state_file.read_text("utf-8")
    assert '"installed"' in saved


def test_install_without_index(server_url, mgr, fake_rvc):
    ins = InstallManager(manager=mgr)
    ins.run("no_idx", download={"url": f"{server_url}/v.pth"}, display_name="无索引")
    st = _wait_install(ins)
    assert st["install"]["status"] == "installed"
    assert not (fake_rvc / "logs" / "no_idx" / "added_no_idx.index").exists()


def test_install_resumes_partial_part(server_url, mgr, fake_rvc):
    """半截 .part + 中断状态 → 重新 run → DownloadManager 断点续传最终装成。"""
    half = len(PTH_DATA) // 2
    part = mgr.download_dir / "resume_voice.pth.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(PTH_DATA[:half])
    mgr.set_meta(install={"voice_id": "resume_voice", "status": "downloading_pth",
                          "message": "中断", "phase": "下载权重", "percent": 30.0,
                          "started_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
                          "error": ""})
    ins = InstallManager(manager=mgr)
    ins.run("resume_voice", download={"url": f"{server_url}/v.pth"},
            index={"url": f"{server_url}/v.index"})
    st = _wait_install(ins)
    assert st["install"]["status"] == "installed"
    assert (fake_rvc / "logs" / "resume_voice" / "resume_voice.pth").read_bytes() == PTH_DATA


def test_install_mutex_and_busy_download(server_url, mgr, fake_rvc):
    ins = InstallManager(manager=mgr)
    ins.run("mutex", download={"url": f"{server_url}/v.pth"})
    with pytest.raises(InstallError):
        ins.run("other", download={"url": f"{server_url}/v.pth"})
    _wait_install(ins)
    # 纯下载进行中再发起安装 → InstallError（复用 DownloadManager 互斥）
    assert mgr.progress().get("status") != "downloading"
    mgr.start("busy_dl", f"{server_url}/v.pth")
    with pytest.raises(InstallError):
        ins.run("busy_install", download={"url": f"{server_url}/v.pth"})
    _wait(mgr)


def _wait(mgr, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mgr.progress().get("status") != "downloading":
            return mgr.progress()
        time.sleep(0.05)
    raise AssertionError("download stuck")


def test_install_invalid_voice_id(mgr, fake_rvc):
    ins = InstallManager(manager=mgr)
    with pytest.raises(InstallError):
        ins.run("bad/id", download={"url": "http://127.0.0.1:1/v.pth"})
    with pytest.raises(InstallError):
        ins.run("..", download={"url": "http://127.0.0.1:1/v.pth"})


def test_install_faileld_cleans_part(server_url, mgr, fake_rvc):
    ins = InstallManager(manager=mgr)
    ins.run("gone", download={"url": f"{server_url}/missing.pth"})
    st = _wait_install(ins)
    assert st["install"]["status"] == "failed"
    assert not (mgr.download_dir / "gone.pth").exists()
    assert not (mgr.download_dir / "gone.pth.part").exists()


# ---------------- API 壳 ----------------
def _api_client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)


def test_api_manifest_ok():
    resp = _api_client().get("/api/market/manifest")
    assert resp.status_code == 200
    assert resp.json()["items"]


def test_api_installed_ok():
    resp = _api_client().get("/api/market/installed")
    assert resp.status_code == 200
    assert "installed" in resp.json()


def test_api_search_requires_q():
    from fastapi.testclient import TestClient
    import server
    assert TestClient(server.app).get("/api/market/search").status_code == 400
    assert TestClient(server.app).get("/api/market/repo").status_code == 400