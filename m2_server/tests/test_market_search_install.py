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

# 伪权重须过 _torch_header_ok 魔数校验：以 pickle 协议 2 头部 \x80\x02 开头
PTH_DATA = b"\x80\x02" + os.urandom(512 * 1024 - 2)          # 512KB 伪权重
IDX_DATA = os.urandom(64 * 1024)           # 64KB 伪索引
MIRROR_DATA = b"\x80\x02" + os.urandom(128 * 1024 - 2)       # 镜像文件（内容刻意不同，也过魔数校验）


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
    assert 12 <= len(items) <= 40  # 2026-09-07 扩充后 18 条
    # voice_id 不重复（安装按 voice_id 落位，重名会互相覆盖）
    vids = [it["voice_id"] for it in items]
    assert len(vids) == len(set(vids))
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


def test_strip_markdown_and_readme_summary(monkeypatch):
    """README 纯文本化与摘要（frontmatter/图片/链接剥除 + 10 分钟缓存）。"""
    md = ("---\nlanguage: zh\nlibrary_name: rvc\n---\n# 懒羊羊音色\n\n"
          "![banner](https://x/y.png)\n这是一个 [懒羊羊](https://example.com) 的 RVC 音色。\n\n"
          "## 使用说明\n\n1. 下载 .pth\n2. 放进 weights")
    clean = ms._strip_markdown(md)
    assert "banner" not in clean and "y.png" not in clean
    assert "懒羊羊" in clean
    assert "language:" not in clean, "frontmatter 应被剥除"

    calls = {}

    def fake_raw(platform, repo):
        calls[repo] = calls.get(repo, 0) + 1
        return md

    monkeypatch.setattr(ms, "_readme_raw", fake_raw)
    first = ms.readme_summary("someone/lazy-voice", "hf")
    assert first and "懒羊羊" in first and "RVC 音色" in first
    monkeypatch.setattr(ms, "_readme_raw", lambda p, r: (_ for _ in ()).throw(AssertionError("不应二次拉取")))
    second = ms.readme_summary("someone/lazy-voice", "hf")
    assert second == first and calls.get("someone/lazy-voice") == 1, "应命中缓存"
    assert ms.readme_summary("someone/nonexistent", "hf") is None


@pytest.fixture()
def readme_cache(monkeypatch):
    """隔离 readme_summary 的全局内存缓存，避免用例间互相污染。"""
    monkeypatch.setattr(ms, "_README_CACHE", {})


class _FakeResp:
    def __init__(self, status: int, text: str = ""):
        self.status_code = status
        self.text = text


def test_strip_markdown_variants():
    """markdown 剥除：frontmatter/图片/链接/强调/表格/代码块/空行压缩。"""
    md = ("---\nlanguage: zh\n---\n"
          "# 标题一\n\n"
          "## 标题二\n\n"
          "粗体 **强调** 与 *斜体*\n\n"
          "- 无序一\n- 无序二\n\n"
          "| 列A | 列B |\n| --- | --- |\n| a1 | b1 |\n\n"
          "```python\nprint('hi')\n```\n\n"
          "链接 [说明文字](https://x/y) 保留文本\n\n"
          "图片 ![alt](https://x/img.png) 应消失\n\n"
          "多行\n\n\n\n空行压缩")
    clean = ms._strip_markdown(md)
    assert "language:" not in clean, "frontmatter 应剥除"
    assert "banner" not in clean
    assert "img.png" not in clean and "![alt]" not in clean
    assert "(https://x/y)" not in clean and "说明文字" in clean
    assert "粗体" in clean and "强调" in clean
    assert "#" not in clean and "**" not in clean
    assert "|" not in clean and "```" not in clean
    assert "\n\n\n\n" not in clean
    compact = " ".join(clean.split())
    assert "多行" in compact and "空行压缩" in compact


def test_strip_markdown_missing_frontmatter_close():
    """无闭合 frontmatter 不误删正文（正则要求成对 ---）。"""
    md = "---\nlanguage: zh\n正文第一行\n正文第二行"   # 只有开头 ---，无闭合
    clean = ms._strip_markdown(md)
    assert "正文第一行" in clean and "正文第二行" in clean


def test_readme_raw_hf_fallback_master(monkeypatch):
    """HF：main 分支 404 → 回退 master 并返回；请求顺序可断言。"""
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if url.endswith("/raw/main/README.md"):
            return _FakeResp(404)
        if url.endswith("/raw/master/README.md"):
            return _FakeResp(200, "# Old readme\nmaster content")
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(ms.requests, "get", fake_get)
    text = ms._readme_raw("hf", "some/repo")
    assert text == "# Old readme\nmaster content"
    assert calls == [f"{ms.HF_API}/some/repo/raw/main/README.md",
                     f"{ms.HF_API}/some/repo/raw/master/README.md"]


def test_readme_raw_hf_none_on_all_404(monkeypatch):
    monkeypatch.setattr(ms.requests, "get",
                        lambda *a, **k: _FakeResp(404))
    assert ms._readme_raw("hf", "some/repo") is None


def test_readme_raw_modelscope_structures(monkeypatch):
    """魔搭 readme API 的多种返回结构解析。"""
    monkeypatch.setattr(ms, "_get_json",
                        lambda url, **kw: {"Code": 200, "Data": {"ModelReadme": "# 模型说明\n说明文本"}})
    assert "说明文本" in ms._readme_raw("modelscope", "a/b")
    monkeypatch.setattr(ms, "_get_json", lambda url, **kw: {"Data": "裸字符串正文"})
    assert ms._readme_raw("modelscope", "a/b") == "裸字符串正文"
    monkeypatch.setattr(ms, "_get_json", lambda url, **kw: {"Code": 200, "Data": {}})
    assert ms._readme_raw("modelscope", "a/b") is None


def test_readme_summary_truncate_and_negative(monkeypatch, readme_cache):
    """摘要截断到 max_chars；空白/异常 → None 且负结果进缓存。"""
    monkeypatch.setattr(ms, "_readme_raw", lambda p, r: "# 标题\n\n" + "word " * 500)
    assert len(ms.readme_summary("some/long", "hf", max_chars=600)) <= 600
    monkeypatch.setattr(ms, "_readme_raw", lambda p, r: "   \n\t ")
    assert ms.readme_summary("some/blank", "hf") is None

    counter = {"n": 0}

    def boom(p, r):
        counter["n"] += 1
        raise OSError("network down")

    monkeypatch.setattr(ms, "_readme_raw", boom)
    assert ms.readme_summary("some/broken", "hf") is None
    assert ms.readme_summary("some/broken", "hf") is None
    assert counter["n"] == 1, "失败结果也应进缓存，绝不重复拉取"


def test_readme_cache_key_isolation(monkeypatch, readme_cache):
    """不同 repo 与不同 platform 的缓存键互不污染。"""
    monkeypatch.setattr(ms, "_readme_raw", lambda p, r: r)
    assert ms.readme_summary("repo-a", "hf") == "repo-a"
    assert ms.readme_summary("repo-b", "hf") == "repo-b"
    assert ms.readme_summary("repo-a", "hf") == "repo-a"
    monkeypatch.setattr(ms, "_readme_raw", lambda p, r: f"{p}:{r}")
    assert ms.readme_summary("repo-a", "modelscope") == "modelscope:repo-a"
    assert ms.readme_summary("repo-a", "hf") == "repo-a", "platform 键独立"


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


# ---------------- A1 溯源与卸载 ----------------
def test_install_writes_source_json(server_url, mgr, fake_rvc):
    """安装完成后落 source.json，记录市场来源与 manifest 溯源。"""
    import json as _json
    ins = InstallManager(manager=mgr)
    ins.run("src_voice", download={"url": f"{server_url}/v.pth"},
            index={"url": f"{server_url}/v.index"}, display_name="演示音色",
            manifest_id="demo/001")
    _wait_install(ins)
    src = _json.loads((fake_rvc / "logs" / "src_voice" / "source.json").read_text("utf-8"))
    assert src["source"] == "market"
    assert src["manifest_id"] == "demo/001"
    assert src["display_name"] == "演示音色"
    assert ins._is_market_installed("src_voice")
    assert not ins._is_market_installed("self_trained_no_marker")


def test_uninstall_market_voice_removes_everything(server_url, mgr, fake_rvc):
    """市场音色卸载：logs 目录 + weights + 下载缓存全部清除。"""
    ins = InstallManager(manager=mgr)
    ins.run("kill_me", download={"url": f"{server_url}/v.pth"},
            index={"url": f"{server_url}/v.index"}, manifest_id="demo/002")
    _wait_install(ins)
    cache_pth = mgr.download_dir / "kill_me.pth"
    assert cache_pth.exists()                  # 下载缓存落盘
    res = ins.uninstall("kill_me")
    assert not (fake_rvc / "logs" / "kill_me").exists()
    assert not (fake_rvc / "assets" / "weights" / "kill_me.pth").exists()
    assert not cache_pth.exists()
    assert "kill_me" not in ins.installed_ids()
    assert res["removed"], "应返回被删除的路径列表"


def test_uninstall_rejects_self_trained(server_url, mgr, fake_rvc):
    """自训产物（logs/<id>/<id>.pth 无 source.json）拒绝被市场卸载误删。"""
    d = fake_rvc / "logs" / "selftrained"
    d.mkdir(parents=True)
    (d / "selftrained.pth").write_bytes(PTH_DATA)
    ins = InstallManager(manager=mgr)
    with pytest.raises(InstallError):
        ins.uninstall("selftrained")
    assert (d / "selftrained.pth").exists(), "非市场来源不可被卸载删除"


def test_uninstall_rejects_unknown(mgr, fake_rvc):
    ins = InstallManager(manager=mgr)
    with pytest.raises(InstallError, match="不存在"):
        ins.uninstall("never_existed")


def test_uninstall_after_market_uninstall_reinstallable(server_url, mgr, fake_rvc):
    """卸载后可重新安装（去重：旧安装状态不阻塞）。"""
    ins = InstallManager(manager=mgr)
    ins.run("circular", download={"url": f"{server_url}/v.pth"}, manifest_id="demo/003")
    _wait_install(ins)
    ins.uninstall("circular")
    ins.run("circular", download={"url": f"{server_url}/v.pth"}, manifest_id="demo/003")
    st = _wait_install(ins)
    assert st["install"]["status"] == "installed"
    assert (fake_rvc / "logs" / "circular" / "source.json").exists()


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


def test_api_market_repo_returns_readme(monkeypatch, readme_cache):
    """/market/repo 端到端返回 README 摘要 + 文件列表，且失败静默不 500。"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server
    import market_api
    # market_api 是 `from market_search import ...` 名字绑定，须 monkeypatch 到 market_api 模块
    monkeypatch.setattr(market_api, "repo_files_hf",
                        lambda repo, recursive=False: [{"name": "v.pth", "path": "v.pth",
                                                        "size": 10, "type": "file", "url": None}])
    monkeypatch.setattr(ms, "_readme_raw",
                        lambda p, r: "# 仓库标题\n\n这是仓库的 README 说明文字。")
    resp = TestClient(server.app).get("/api/market/repo?repo=some/repo&platform=hf")
    assert resp.status_code == 200
    body = resp.json()
    assert body["readme"] and "说明文字" in body["readme"]
    assert body["files"][0]["path"] == "v.pth"

    # readme 拉取异常 → readme 为 null，接口仍 200
    monkeypatch.setattr(ms, "_readme_raw",
                        lambda p, r: (_ for _ in ()).throw(OSError("down")))
    resp2 = TestClient(server.app).get("/api/market/repo?repo=other/repo&platform=hf")
    assert resp2.status_code == 200
    assert resp2.json()["readme"] is None

# ---------------- 权重落位：硬链接省一份，失败回退复制 ----------------

def test_link_or_copy_content_identical(tmp_path):
    """_link_or_copy 后 dst 内容与 src 一致（无论走硬链接还是复制）。"""
    from market_install import _link_or_copy
    src = tmp_path / "a.pth"
    src.write_bytes(PTH_DATA)
    dst = tmp_path / "b.pth"
    _link_or_copy(src, dst)
    assert dst.read_bytes() == PTH_DATA


def test_link_or_copy_overwrites_existing(tmp_path):
    """目标已存在（覆盖重装场景）时先清掉再落，不抛异常。"""
    from market_install import _link_or_copy
    src = tmp_path / "a.pth"
    src.write_bytes(PTH_DATA)
    dst = tmp_path / "b.pth"
    dst.write_bytes(b"\x80\x02stale-old-content")
    _link_or_copy(src, dst)
    assert dst.read_bytes() == PTH_DATA


def test_link_or_copy_falls_back_to_copy(monkeypatch, tmp_path):
    """os.link 抛 OSError（跨盘/非 NTFS）→ 静默回退复制，结果仍正确。"""
    import market_install as mi
    monkeypatch.setattr(mi.os, "link", lambda s, d: (_ for _ in ()).throw(OSError("cross-device")))
    src = tmp_path / "a.pth"
    src.write_bytes(PTH_DATA)
    dst = tmp_path / "b.pth"
    assert mi._link_or_copy(src, dst) == "copy"
    assert dst.read_bytes() == PTH_DATA


def test_stage_writes_both_locations(monkeypatch, tmp_path, mgr, fake_rvc):
    """_stage 仍把权重落满 logs/ 与 assets/weights/ 两处（实时变声两处都要）。"""
    ins = InstallManager(manager=mgr)
    src = mgr.download_dir / "dual.pth"
    mgr.download_dir.mkdir(parents=True, exist_ok=True)
    src.write_bytes(PTH_DATA)
    ins._stage("dual")
    log_pth = fake_rvc / "logs" / "dual" / "dual.pth"
    w_pth = fake_rvc / "assets" / "weights" / "dual.pth"
    assert log_pth.read_bytes() == PTH_DATA
    assert w_pth.read_bytes() == PTH_DATA


# ---------------- A6 覆盖重装 .old 备份与回滚 ----------------

@pytest.fixture()
def old_dir(tmp_path):
    return tmp_path / "old"


@pytest.fixture()
def fake_out(tmp_path, monkeypatch):
    """隔离 preview/qc 产物目录，避免测试污染真实 outputs。"""
    out = tmp_path / "out"
    out.mkdir(parents=True)
    monkeypatch.setattr(config, "OUTPUTS_DIR", out)
    return out


def _install_v1(ins, voice_id, server_url):
    ins.run(voice_id, download={"url": f"{server_url}/v.pth"},
            index={"url": f"{server_url}/v.index"})
    return _wait_install(ins)


def test_overwrite_backs_up_old_version(server_url, mgr, fake_rvc, old_dir):
    """覆盖重装：旧版本先归档到 .old/<id>/，备份内容即旧版 pth。"""
    ins = InstallManager(manager=mgr, old_dir=old_dir)
    _install_v1(ins, "roll_me", server_url)
    ins.run("roll_me", download={"url": f"{server_url}/mirror.pth"},
            index={"url": f"{server_url}/v.index"}, overwrite=True)
    st = _wait_install(ins)
    assert st["install"]["status"] == "installed"
    assert (fake_rvc / "logs" / "roll_me" / "roll_me.pth").read_bytes() == MIRROR_DATA
    snaps = list((old_dir / "roll_me").iterdir())
    assert len(snaps) == 1
    assert (snaps[0] / "roll_me.pth").read_bytes() == PTH_DATA    # 归档的是 v1
    assert "roll_me" in ins.backup_ids()


def test_rollback_restores_previous_version(server_url, mgr, fake_rvc, old_dir, fake_out):
    """回滚：恢复 v1 到 logs + assets，消费备份，并清失效的试听/质检产物。"""
    ins = InstallManager(manager=mgr, old_dir=old_dir)
    _install_v1(ins, "rb_voice", server_url)
    ins.run("rb_voice", download={"url": f"{server_url}/mirror.pth"},
            index={"url": f"{server_url}/v.index"}, overwrite=True)
    _wait_install(ins)
    log_pth = fake_rvc / "logs" / "rb_voice" / "rb_voice.pth"
    assert log_pth.read_bytes() == MIRROR_DATA
    # 预置新版本生成的失效产物（preview / qc）
    (fake_out / "market").mkdir(parents=True, exist_ok=True)
    (fake_out / "qc").mkdir(parents=True, exist_ok=True)
    (fake_out / "market" / "rb_voice_preview.json").write_text("{}", encoding="utf-8")
    (fake_out / "qc" / "rb_voice.json").write_text("{}", encoding="utf-8")
    res = ins.rollback("rb_voice")
    assert res["voice_id"] == "rb_voice"
    assert log_pth.read_bytes() == PTH_DATA
    assert (fake_rvc / "assets" / "weights" / "rb_voice.pth").read_bytes() == PTH_DATA
    assert not (old_dir / "rb_voice").exists()                    # 备份被消费
    assert "rb_voice" not in ins.backup_ids()
    assert not (fake_out / "market" / "rb_voice_preview.json").exists()
    assert not (fake_out / "qc" / "rb_voice.json").exists()


def test_rollback_rejects_without_backup(server_url, mgr, fake_rvc, old_dir):
    """首次安装（无历史备份）不可回滚。"""
    ins = InstallManager(manager=mgr, old_dir=old_dir)
    _install_v1(ins, "no_bak", server_url)
    with pytest.raises(InstallError, match="没有可回滚"):
        ins.rollback("no_bak")


def test_rollback_rejects_self_trained(mgr, fake_rvc, old_dir):
    """非市场来源（无 source.json）拒绝回滚，避免误动自训产物。"""
    d = fake_rvc / "logs" / "selftrained_rb"
    d.mkdir(parents=True)
    (d / "selftrained_rb.pth").write_bytes(PTH_DATA)
    ins = InstallManager(manager=mgr, old_dir=old_dir)
    with pytest.raises(InstallError, match="不是市场安装来源"):
        ins.rollback("selftrained_rb")
    assert (d / "selftrained_rb.pth").exists()


def test_rollback_rejects_when_installing(server_url, mgr, fake_rvc, old_dir):
    """覆盖重装进行中拒绝回滚（与卸载同样的互斥）。"""
    ins = InstallManager(manager=mgr, old_dir=old_dir)
    _install_v1(ins, "busy_rb", server_url)
    ins.run("busy_rb", download={"url": f"{server_url}/mirror.pth"}, overwrite=True)
    with pytest.raises(InstallError, match="进行中"):
        ins.rollback("busy_rb")
    _wait_install(ins)


def test_uninstall_cleans_old_backups(server_url, mgr, fake_rvc, old_dir):
    """卸载连带清 .old 历史备份（卸载 = 彻底移除）。"""
    ins = InstallManager(manager=mgr, old_dir=old_dir)
    _install_v1(ins, "kill_bak", server_url)
    ins.run("kill_bak", download={"url": f"{server_url}/mirror.pth"}, overwrite=True)
    _wait_install(ins)
    assert (old_dir / "kill_bak").exists()
    ins.uninstall("kill_bak")
    assert not (old_dir / "kill_bak").exists()
    assert "kill_bak" not in ins.backup_ids()


def test_install_failure_auto_rolls_back(server_url, mgr, fake_rvc, old_dir):
    """覆盖重装下载失败 → 自动回滚到旧版本；备份不消费（仍可手动回滚）。"""
    ins = InstallManager(manager=mgr, old_dir=old_dir)
    _install_v1(ins, "auto_rb", server_url)
    ins.run("auto_rb", download={"url": f"{server_url}/missing.pth"}, overwrite=True)
    st = _wait_install(ins)
    assert st["install"]["status"] == "failed"
    assert "已自动回滚" in st["install"]["message"]
    assert (fake_rvc / "logs" / "auto_rb" / "auto_rb.pth").read_bytes() == PTH_DATA
    assert (fake_rvc / "assets" / "weights" / "auto_rb.pth").read_bytes() == PTH_DATA
    assert "auto_rb" in ins.backup_ids()


def test_prune_old_keeps_newest(tmp_path):
    """备份裁剪：超出 OLD_KEEP 份时删最旧，保留最新 3 份。"""
    from market_install import OLD_KEEP
    old_dir = tmp_path / "old"
    base = old_dir / "v"
    for i in range(5):
        (base / f"20260101_0000{i}").mkdir(parents=True)
    ins = InstallManager(manager=None, old_dir=old_dir)
    ins._prune_old("v")
    names = sorted(p.name for p in base.iterdir())
    assert names == [f"20260101_0000{i}" for i in range(5 - OLD_KEEP, 5)]


# ---------------- A6 API 壳 ----------------

def test_api_backups_and_rollback_validation():
    resp = _api_client().get("/api/market/backups")
    assert resp.status_code == 200
    assert "backups" in resp.json()
    resp = _api_client().post("/api/market/rollback", json={"voice_id": "never_existed"})
    assert resp.status_code == 409


# ---------------- A7 信号量并发：pth 与 index 并行下载 ----------------

class _ParallelCtx:
    def __init__(self):
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.files = {}


class _ParallelHandler(http.server.BaseHTTPRequestHandler):
    """慢速 + 并发计数 handler：用于证明安装内 pth/index 同时在线下载。"""

    ctx = _ParallelCtx()

    def log_message(self, *a):
        pass

    def do_GET(self):
        with self.ctx.lock:
            self.ctx.active += 1
            self.ctx.peak = max(self.ctx.peak, self.ctx.active)
        try:
            path = self.path.split("?")[0]
            data = self.ctx.files.get(path)
            if data is None:
                self.send_error(404)
                return
            time.sleep(0.15)                 # 拉长下载窗口，让并发可观测
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        finally:
            with self.ctx.lock:
                self.ctx.active -= 1


@pytest.fixture(scope="module")
def parallel_server():
    _ParallelHandler.ctx = _ParallelCtx()
    _ParallelHandler.ctx.files = {"/v.pth": PTH_DATA, "/v.index": IDX_DATA}
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ParallelHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_install_downloads_pth_and_index_in_parallel(parallel_server, mgr, fake_rvc):
    """安装内 pth 与 index 并行下载（信号量文件级并发），最终全部装好。"""
    ins = InstallManager(manager=mgr)
    ins.run("para", download={"url": f"{parallel_server}/v.pth"},
            index={"url": f"{parallel_server}/v.index"})
    st = _wait_install(ins)
    assert st["install"]["status"] == "installed", st
    assert (fake_rvc / "logs" / "para" / "para.pth").read_bytes() == PTH_DATA
    assert (fake_rvc / "logs" / "para" / "added_para.index").read_bytes() == IDX_DATA
    assert _ParallelHandler.ctx.peak >= 2, \
        f"pth 与 index 应并行下载，实际峰值并发 {_ParallelHandler.ctx.peak}"


# ---------------- 搜索翻页（skip / next_skip，2026-09-07） ----------------

def test_search_hf_window_offset(monkeypatch):
    """翻页 = 窗口切片：向 HF 拉 offset+limit 条再切，深页不做文件探测。"""
    calls = []

    def fake(url, **params):
        calls.append(dict(params))
        return [{"id": f"u/model-{i}"} for i in range(120)]

    monkeypatch.setattr(ms, "_get_json", fake)
    items = ms.search_hf("q", limit=50, offset=50)
    assert [i["repo"] for i in items] == [f"u/model-{i}" for i in range(50, 100)]
    assert calls[0]["limit"] == 100, "窗口应为 offset+limit"
    assert all(i["files"] == [] for i in items), "深页不做 tree 探测"
    assert ms.search_hf("q", limit=50, offset=200) == [], "超过 HF_FETCH_MAX 为空"
    assert calls[-1]["limit"] == ms.HF_FETCH_MAX


def test_search_pagination_next_skip(monkeypatch):
    monkeypatch.setattr(ms, "_get_json",
                        lambda url, **params: [{"id": f"u/m-{i}"} for i in range(80)])
    r1 = ms.search("hf", "q", limit=50, skip=0)
    assert len(r1["items"]) == 50 and r1["next_skip"] == 50
    r2 = ms.search("hf", "q", limit=50, skip=50)
    assert r2["items"][0]["repo"] == "u/m-50"
    assert r2["next_skip"] is None, "末页（30 条 < 50）→ 没有更多"


def test_search_skip_beyond_ms_block(monkeypatch):
    """平台=all：结果流 = 魔搭块（首页计入）+ HF 续流，skip 跨块不重不漏。"""
    monkeypatch.setattr(ms, "search_ms",
                        lambda q, limit: {"items": [{"id": f"ms-{i}"} for i in range(3)],
                                          "note": ""})
    monkeypatch.setattr(ms, "_get_json",
                        lambda url, **params: [{"id": f"hf-{i}"} for i in range(60)])
    r1 = ms.search("all", "q", limit=50, skip=0)
    assert r1["items"][0]["id"] == "ms-0" and r1["items"][3]["id"] == "hf-0"
    assert r1["next_skip"] == 50
    r2 = ms.search("all", "q", limit=50, skip=50)
    assert r2["items"][0]["id"] == "hf-47", "skip=50 = 3 魔搭 + 47 HF"
