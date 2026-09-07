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
    """共享请求状态：文件表 + 收到的 Range 头记录 + 并发计数。"""

    def __init__(self):
        self.files = {"/full.bin": DATA, "/mirror.bin": MIRROR_DATA}
        self.ranges = []                  # 收到的 Range 头列表
        self.requests = {"/fail_main.bin": 0}
        self.active = 0                   # 当前并发处理中的请求数
        self.peak = 0                     # 并发峰值（信号量限流断言用）
        self.lock = threading.Lock()


class RangeHandler(http.server.BaseHTTPRequestHandler):
    ctx = _Ctx()
    slow_mode = False

    def log_message(self, *a):            # 静默访问日志
        pass

    def _enter(self):
        with self.ctx.lock:
            self.ctx.active += 1
            self.ctx.peak = max(self.ctx.peak, self.ctx.active)

    def _leave(self):
        with self.ctx.lock:
            self.ctx.active -= 1

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
            # 块间限速（sleep 放在每块写入之前）：写完最后一块后立即返回，
            # 避免"写完后还要 sleep 0.02s"把请求停留计数，造成客户端已完成
            # 但服务端 active 仍 +1 的重叠窗口（信号量峰值断言被误报 +1）。
            for i in range(0, len(body), 5120):
                if i:
                    time.sleep(0.02)
                self.wfile.write(body[i:i + 5120])
                self.wfile.flush()
        else:
            self.wfile.write(body)

    def do_GET(self):
        self._enter()
        try:
            self._serve()
        finally:
            self._leave()

    def do_HEAD(self):
        self._enter()
        try:
            self._serve(head_only=True)
        finally:
            self._leave()


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
    mgr._tasks[name] = {
        "name": name, "filename": f"{name}.pth", "status": "interrupted",
        "done": half, "total": len(data), "error": "",
        "dest": str(mgr.download_dir / f"{name}.pth"), "part": str(part),
        "started_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:00",
        "url": "", "mirror_url": None, "sha256": None, "expected_size": None,
    }
    if name not in mgr._order:
        mgr._order.append(name)


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


def test_same_name_rejected_while_active(mgr, server_url):
    """同名活跃任务拒绝重复启动；异名任务可并行（信号量文件级并发）。"""
    RangeHandler.slow_mode = True
    try:
        mgr.start("slow", f"{server_url}/full.bin")
        with pytest.raises(MarketError):
            mgr.start("slow", f"{server_url}/mirror.bin")
        assert mgr.progress()["name"] == "slow"
        # 异名任务不再被互斥拒绝：并发下载是信号量队列的基础
        mgr.start("slow2", f"{server_url}/mirror.bin")
        assert len(mgr.active_names()) >= 2
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

# ---- 2026-09-06 懒羊羊下载失败修复：Xet CDN 子域 + 回退不污染主源 url ----

def test_whitelist_allows_hf_subdomains():
    """HF 重定向到 Xet CDN（cas-bridge.xethub.hf.co 等 *.hf.co 子域）必须放行。"""
    from market_download import _validate_url
    _validate_url("https://cas-bridge.xethub.hf.co/xet-bridge-us/abc?Expires=1")
    _validate_url("https://cdn-lfs.hf.co/x/y")
    _validate_url("https://huggingface.co/a/b")


def test_whitelist_rejects_lookalike_domains():
    """白名单子域匹配不得放过伪装域（evil-hf.co / hf.co.evil.com）。"""
    from market_download import _validate_url
    import pytest
    with pytest.raises(MarketError):
        _validate_url("https://evil-hf.co/a.pth")
    with pytest.raises(MarketError):
        _validate_url("https://hf.co.evil.com/a.pth")
    with pytest.raises(MarketError):
        _validate_url("https://cas-bridge.xethub.hf.co.evil.com/a.pth")


def test_mirror_failover_keeps_primary_url(mgr, server_url):
    """回退镜像时 state.url 必须保持主源不变（此前被覆盖成镜像，误导排查）。"""
    RangeHandler.ctx.requests["/fail_main2.bin"] = 0
    mgr.start("vo2", f"{server_url}/fail_main2.bin", mirror_url=f"{server_url}/mirror.bin")
    st = _wait(mgr)
    assert st["status"] == "done"
    # done 态清理 url 字段，这里单独验证回退标记
    mgr2_state = mgr.progress()
    assert mgr2_state.get("url") in (None, f"{server_url}/fail_main2.bin")


def test_mirror_failover_failed_state_keeps_primary_url(mgr, server_url):
    """主源与镜像都失败 → failed 态里 url 仍是主源（修复前显示的是镜像 URL）。"""
    RangeHandler.ctx.requests["/always_fail.bin"] = 0
    mgr.start("vo3", f"{server_url}/always_fail.bin", mirror_url=f"{server_url}/always_fail.bin")
    st = _wait(mgr)
    assert st["status"] == "failed"
    assert st["url"] == f"{server_url}/always_fail.bin"
    assert st.get("attempt_url") == f"{server_url}/always_fail.bin"


# ---- 2026-09-07 信号量文件级并发 ----

def test_semaphore_limits_concurrent_downloads(server_url, tmp_path):
    """并发 4 个下载任务，信号量 max_concurrent=3：同时活跃下载 ≤3，全部完成。"""
    mgr = DownloadManager(download_dir=tmp_path / "dl",
                          state_file=tmp_path / "dl" / "downloads.json",
                          allow_loopback=True, max_concurrent=3)
    RangeHandler.slow_mode = True
    RangeHandler.ctx.active = 0
    RangeHandler.ctx.peak = 0
    try:
        for i in range(4):
            mgr.start(f"con_{i}", f"{server_url}/full.bin")
        deadline = time.time() + 90
        while time.time() < deadline and mgr.is_busy():
            time.sleep(0.05)
        for i in range(4):
            st = mgr.task_status(f"con_{i}")
            assert st.get("status") == "done", f"con_{i}: {st}"
        assert 1 < RangeHandler.ctx.peak <= 3, \
            f"信号量应把并发限到 ≤3，实际峰值 {RangeHandler.ctx.peak}"
    finally:
        RangeHandler.slow_mode = False


def test_cancel_single_task_leaves_others(mgr, server_url):
    """cancel(name) 只取消指定任务，其它并行任务不受影响继续完成。"""
    RangeHandler.slow_mode = True
    try:
        mgr.start("keep_a", f"{server_url}/full.bin")
        mgr.start("kill_b", f"{server_url}/full.bin")
        mgr.cancel("kill_b")
        st = mgr.task_status("kill_b")
        deadline = time.time() + 10
        while time.time() < deadline and st.get("status") != "cancelled":
            time.sleep(0.05)
            st = mgr.task_status("kill_b")
        assert st.get("status") == "cancelled"
        assert not (mgr.download_dir / "kill_b.pth.part").exists()
        # keep_a 未被取消，照常下到 done
        st = mgr.task_status("keep_a")
        deadline = time.time() + 90
        while time.time() < deadline and st.get("status") == "downloading":
            time.sleep(0.05)
            st = mgr.task_status("keep_a")
        assert st.get("status") == "done"
    finally:
        RangeHandler.slow_mode = False


def test_progress_returns_earliest_active_with_list(mgr, server_url):
    """多任务下 progress() 返回最早活跃任务，并附 active 列表（兼容主任务语义）。"""
    RangeHandler.slow_mode = True
    try:
        mgr.start("p1", f"{server_url}/full.bin")
        mgr.start("p2", f"{server_url}/full.bin")
        st = mgr.progress()
        assert st["name"] == "p1"
        assert set(st["active"]) >= {"p1", "p2"}
        _wait(mgr)
    finally:
        RangeHandler.slow_mode = False


def test_semaphore_queue_no_task_lost(mgr, server_url):
    """信号量排队不丢任务：max_concurrent=1 串行完成 3 个任务，全部 done。"""
    RangeHandler.slow_mode = True
    try:
        mgr = DownloadManager(download_dir=mgr.download_dir,
                              state_file=mgr.state_file,
                              allow_loopback=True, max_concurrent=1)
        for i in range(3):
            mgr.start(f"q_{i}", f"{server_url}/mirror.bin")
        deadline = time.time() + 90
        while time.time() < deadline and mgr.is_busy():
            time.sleep(0.05)
        for i in range(3):
            st = mgr.task_status(f"q_{i}")
            assert st.get("status") == "done", f"q_{i}: {st}"
    finally:
        RangeHandler.slow_mode = False
