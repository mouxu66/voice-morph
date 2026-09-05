"""音色市场下载核心测试：断点续传 / SHA256 校验 / 镜像回退 / 单任务互斥 / 取消 / 进度落盘 / 域名白名单。

用本地 ThreadingHTTPServer 提供真实 HTTP 文件（含 Range/206 与慢速源），
不 mock 网络层，保证续传语义（.part + Range 接续）被真实验证。
"""
import hashlib
import http.server
import os
import threading
import time

import pytest

from market_download import DownloadManager, MarketError, MAX_BYTES

# 假权重必须以 PyTorch 存档文件头开头（\x80\x02 = pickle 协议 2），
# 否则过不了 _torch_header_ok 魔数校验（头部校验按设计拒随机字节当权重）。
_PTH_PREFIX = b"\x80\x02"
DATA = _PTH_PREFIX + os.urandom(1024 * 1024 - len(_PTH_PREFIX))     # 1MB 假权重
SHA = hashlib.sha256(DATA).hexdigest()
MIRROR_DATA = _PTH_PREFIX + os.urandom(256 * 1024 - len(_PTH_PREFIX))  # 镜像假权重


class _Ctx:
    """共享请求状态：文件表 + 收到的 Range 头记录。"""

    def __init__(self):
        self.files = {"/full.bin": DATA, "/mirror.bin": MIRROR_DATA}
        self.ranges = []                  # 收到的 Range 头列表
        self.requests = {"/fail_main.bin": 0}


class RangeHandler(http.server.BaseHTTPRequestHandler):
    ctx = _Ctx()
    slow_mode = False

    def log_message(self, *a):            # 静默访问日志
        pass

    def _serve(self, head_only=False):
        path = self.path.split("?")[0]
        if path == "/fail_main.bin":
            self.ctx.requests["/fail_main.bin"] += 1
            if not head_only and self.ctx.requests["/fail_main.bin"] in (1, 2):
                self.send_error(500)
                return
            data = self.ctx.files["/full.bin"]      # 第 3 次起正常（数据同 full）
        else:
            data = self.ctx.files.get(path)
        if data is None:
            self.send_error(404)
            return
        size = len(data)
        rng = self.headers.get("Range")
        if rng:
            self.ctx.ranges.append(rng)
            start = int(rng.split("=")[1].split("-")[0])
            body = data[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{size-1}/{size}")
        else:
            start = 0
            body = data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if head_only:
            return
        if RangeHandler.slow_mode:
            for i in range(0, len(body), 5120):
                self.wfile.write(body[i:i + 5120])
                self.wfile.flush()
                time.sleep(0.02)
        else:
            self.wfile.write(body)

    do_GET = lambda self: self._serve()              # noqa: E731
    do_HEAD = lambda self: self._serve(head_only=True)  # noqa: E731


@pytest.fixture(scope="module")
def server_url():
    RangeHandler.slow_mode = False
    RangeHandler.ctx = _Ctx()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def mgr(tmp_path):
    return DownloadManager(download_dir=tmp_path / "dl",
                           state_file=tmp_path / "dl" / "downloads.json",
                           allow_loopback=True)


def _wait(mgr, timeout=20.0):
    """轮询到 status 离开 downloading；返回最终状态快照。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = mgr.progress()
        if st.get("status") != "downloading":
            return st
        time.sleep(0.05)
    raise AssertionError("download did not finish within timeout")


def _simulate_interrupted(mgr, name, data):
    """模拟进程中断：状态残留 interrupted + 半截 .part（服务重启场景）。"""
    half = len(data) // 2
    part = mgr.download_dir / f"{name}.pth.part"
    part.write_bytes(data[:half])
    mgr._state = {
        "idle": False, "name": name, "status": "interrupted",
        "done": half, "total": len(data), "error": "",
        "dest": str(mgr.download_dir / f"{name}.pth"), "part": str(part),
        "started_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
        "url": "", "mirror_url": None, "sha256": None, "expected_size": None,
    }


def test_full_download_with_sha256(mgr, server_url):
    st = mgr.start("voice_a", f"{server_url}/full.bin", sha256=SHA)
    assert st["status"] == "downloading"
    st = _wait(mgr)
    assert st["status"] == "done"
    assert (mgr.download_dir / "voice_a.pth").read_bytes() == DATA


def test_resume_from_partial_part_with_sha256(mgr, server_url):
    """半截 .part + interrupted 状态 → start 续传，SHA256 全程累加最终核验通过。"""
    _simulate_interrupted(mgr, "voice_h", DATA)
    mgr.start("voice_h", f"{server_url}/full.bin", sha256=SHA)
    st = _wait(mgr)
    assert st["status"] == "done"
    assert (mgr.download_dir / "voice_h.pth").read_bytes() == DATA


def test_resume_uses_range_header(mgr, server_url):
    """续传必须带 Range 头（服务端收 206 接续），验证真正断点续传而非重下。"""
    _simulate_interrupted(mgr, "voice_r", DATA)
    RangeHandler.ctx.ranges.clear()
    mgr.start("voice_r", f"{server_url}/full.bin")
    st = _wait(mgr)
    assert st["status"] == "done"
    assert any(r.startswith(f"bytes={len(DATA) // 2}-") for r in RangeHandler.ctx.ranges), \
        f"expected resume Range, got {RangeHandler.ctx.ranges}"
    assert (mgr.download_dir / "voice_r.pth").read_bytes() == DATA


def test_sha256_mismatch_fails_clean(mgr, server_url):
    st = mgr.start("bad_hash", f"{server_url}/full.bin", sha256="0" * 64)
    st = _wait(mgr)
    assert st["status"] == "failed"
    assert "SHA256" in st["error"]
    assert not (mgr.download_dir / "bad_hash.pth").exists()
    assert not (mgr.download_dir / "bad_hash.pth.part").exists()


def test_failover_to_mirror(mgr, server_url):
    """主源前 2 次 500 → 自动回退镜像，完成文件取镜像内容。"""
    RangeHandler.ctx.requests["/fail_main.bin"] = 0
    mgr.start("vo", f"{server_url}/fail_main.bin", mirror_url=f"{server_url}/mirror.bin")
    st = _wait(mgr)
    assert st["status"] == "done"
    assert (mgr.download_dir / "vo.pth").read_bytes() == MIRROR_DATA


def test_single_task_mutex(mgr, server_url):
    RangeHandler.slow_mode = True
    try:
        mgr.start("slow", f"{server_url}/full.bin")
        with pytest.raises(MarketError):
            mgr.start("slow2", f"{server_url}/mirror.bin")
        assert mgr.progress()["name"] == "slow"
        _wait(mgr)
    finally:
        RangeHandler.slow_mode = False


def test_cancel_removes_part(mgr, server_url):
    RangeHandler.slow_mode = True
    try:
        mgr.start("big", f"{server_url}/full.bin")
        assert mgr.progress()["status"] == "downloading"
        mgr.cancel()
        for _ in range(200):
            st = mgr.progress()
            if st.get("status") != "downloading":
                break
            time.sleep(0.05)
        st = mgr.progress()
        assert st["status"] == "cancelled"
        assert not (mgr.download_dir / "big.pth").exists()
        assert not (mgr.download_dir / "big.pth.part").exists()
    finally:
        RangeHandler.slow_mode = False


def test_progress_persisted_to_disk(mgr, server_url):
    mgr.start("persist_me", f"{server_url}/mirror.bin",
              sha256=hashlib.sha256(MIRROR_DATA).hexdigest())
    st = _wait(mgr)
    assert st["status"] == "done"
    saved = mgr.state_file.read_text("utf-8")
    assert "persist_me" in saved
    assert '"status": "done"' in saved


def test_domain_whitelist_rejects_untrusted(mgr):
    with pytest.raises(MarketError):
        mgr.start("evil", "http://evil.example.com/a.pth")
    with pytest.raises(MarketError):
        mgr.start("evil2", "https://raw.githubusercontent.com/x/y.pth")


def test_size_limit_enforced_before_request(mgr):
    """expected_size 超上限：不发起网络请求即失败。"""
    mgr.start("huge", "https://huggingface.co/x/huge.pth", expected_size=MAX_BYTES + 1)
    st = _wait(mgr)
    assert st["status"] == "failed"
    assert "上限" in st["error"]


# ---- API 壳（路由存在性 + 错误映射）----
@pytest.fixture()
def iso_api(monkeypatch, tmp_path):
    """把 market_api 的下载/安装单例换成 tmp 隔离实例，避免读到磁盘真实
    outputs/market/downloads.json（曾有 install_lanyangyang/cancelled 残留
    导致 test_api_progress_empty / test_api_cancel_without_task 假红）。"""
    import market_api
    from market_install import InstallManager
    m = DownloadManager(download_dir=tmp_path / "dl",
                        state_file=tmp_path / "dl" / "downloads.json",
                        allow_loopback=True)
    ins = InstallManager(manager=m)
    monkeypatch.setattr(market_api, "get_manager", lambda: m)
    monkeypatch.setattr(market_api, "get_installer", lambda: ins)
    return m, ins


def test_api_progress_empty(iso_api):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server
    resp = TestClient(server.app).get("/api/market/progress")
    assert resp.status_code == 200
    assert resp.json() == {"task": None}


def test_api_rejects_untrusted_domain(iso_api):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server
    resp = TestClient(server.app).post("/api/market/download", json={
        "name": "evil", "url": "https://evil.example.com/a.pth"})
    assert resp.status_code == 409
    assert "白名单" in resp.json().get("detail", "")


def test_api_cancel_without_task(iso_api):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    import server
    resp = TestClient(server.app).post("/api/market/cancel")
    assert resp.status_code == 409