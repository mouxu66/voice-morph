"""「粘直链」的守卫测试（`url_fetch` 的护栏 + `cover_api` 的两个入口）。

这个功能的失败模式要么是**安全事故**（后端被当成内网探测/下载代理），
要么是**静默的错**（下回来一个 HTML 页面，交给 ffmpeg 报一句看不懂的错）。
所以这里逐条钉住：

    · 私网/回环/链路本地地址一律拒 —— 含"域名解析到内网"和"重定向逃到内网"
    · 只认 http/https（file:// ftp:// 之类不碰）
    · 大文件写盘过程中拦，且不留残渣
    · 网页页面要给人话，不是解码失败
    · 源文件住会话目录、跑完即删；源不对时 `COVER_STATE` 不许卡在 running

不碰网络、不跑 ffmpeg/RVC：所有外部依赖都用替身。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def env(monkeypatch, tmp_path):
    """把会话目录指到 tmp_path，返回 (url_fetch, cover_api)。

    顺带把 `COVER_STATE` 复位：它是 `cover_api` 的**模块级全局**，
    一条用例把 `running` 置真之后，下一条用例走 `/cover/run` 会在锁里
    撞上 409「已有翻唱任务在跑」—— 于是 `test_run_without_source_does_not_
    stick_in_running` 变成"单跑绿、整文件跑红"（2026-09-26 实测）。
    复位放在这里，而不是靠用例自己收尾：失败路径下收尾代码不保证执行。
    """
    import config as cfg

    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(cfg, "MEDIA_DIR", tmp_path / "media")
    import cover_api
    import url_fetch

    cover_api.COVER_STATE.update(
        running=False, status="idle", step="", message="", error="", percent=0.0
    )

    return url_fetch, cover_api


class FakeResp:
    """假响应。`iter_content` 返回**同一个迭代器** —— 与 requests 的生成器语义一致
    （第二次调用从上次的位置继续），否则"先读一个头再接着读完"会把首块读两遍。"""

    def __init__(self, status=200, headers=None, chunks=(), url="http://pub.example/song.mp3"):
        self.status_code = status
        self.headers = dict(headers or {})
        self.url = url
        self._it = iter(chunks)
        self.closed = False

    def iter_content(self, size):  # noqa: ARG002 —— 替身不看块大小
        return self._it

    def close(self):
        self.closed = True

    # requests 的 `Response` 就是上下文管理器（分片路径用的是 `with requests.get(...)`），
    # 替身不接这两个协议方法就会以 `TypeError: 'FakeResp' object does not support the
    # context manager protocol` 收场 —— 那是**替身缺件**，不是产品代码的错。
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _patch_get(monkeypatch, uf, responses):
    """按顺序返回 n 个响应（第一个 302、第二个 200 这种重定向场景用得上）。"""
    calls: list[str] = []
    seq = list(responses)

    def fake_get(url, **kw):
        calls.append(url)
        assert kw.get("allow_redirects") is False, "必须手动跟重定向（每一跳都要重判）"
        return seq.pop(0) if seq else FakeResp(status=200, chunks=[b""])

    monkeypatch.setattr(uf.requests, "get", fake_get)
    return calls


# ---------------------------------------------------------------- 地址护栏


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000/x.mp3",
        "http://localhost/x.mp3",
        "http://10.0.0.5/x.mp3",
        "http://192.168.1.9/a.mp3",
        "http://169.254.169.254/latest/meta-data",  # 云元数据
        "http://[::1]/x.mp3",
        "http://0.0.0.0/x.mp3",
    ],
)
def test_rejects_private_and_loopback(env, url):
    """★ 本机/内网地址一律不许 —— 后端监听 0.0.0.0，会被当成内网探测工具。"""
    uf, _ = env
    with pytest.raises(uf.FetchError):
        uf.validate_url(url)


def test_rejects_non_http_scheme(env):
    uf, _ = env
    for url in ("file:///C:/Windows/win.ini", "ftp://pub.example/a.mp3", "javascript:alert(1)"):
        with pytest.raises(uf.FetchError, match="http"):
            uf.validate_url(url)


def test_rejects_domain_resolving_to_private(env, monkeypatch):
    """域名解析到内网也要拒（`http://evil.example` → 10.0.0.7 这类）。"""
    uf, _ = env
    monkeypatch.setattr(
        uf.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.7", 0))],
    )
    with pytest.raises(uf.FetchError, match="内网"):
        uf.validate_url("http://evil.example/song.mp3")


def test_rejects_redirect_to_private(env, monkeypatch, tmp_path):
    """★ 302 逃到内网必须被拦下（SSRF 的经典绕法），且不许落下任何文件。"""
    uf, _ = env
    monkeypatch.setattr(
        uf.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    _patch_get(monkeypatch, uf, [FakeResp(status=302, headers={"Location": "http://127.0.0.1/secret"})])

    with pytest.raises(uf.FetchError):
        uf.fetch_to_session("http://pub.example/song.mp3")
    assert list((tmp_path / ".session").glob("cover_src_*")) == []


# ---------------------------------------------------------------- 内容护栏


def test_rejects_html_page(env, monkeypatch):
    """粘的是网页链接 → 给人话，而不是把 200KB HTML 交给 ffmpeg。"""
    uf, _ = env
    monkeypatch.setattr(
        uf.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    _patch_get(
        monkeypatch, uf,
        [FakeResp(headers={"Content-Type": "text/html; charset=utf-8"},
                  chunks=[b"<!doctype html><html><body>nope</body></html>"])],
    )
    with pytest.raises(uf.FetchError, match="网页"):
        uf.fetch_to_session("http://pub.example/song")


def test_rejects_oversize_and_leaves_no_residue(env, monkeypatch, tmp_path):
    """★ 超限要在写盘过程中拦（不能只信 Content-Length），且 .part 残渣要清掉。"""
    uf, _ = env
    monkeypatch.setattr(
        uf.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    big = b"ID3" + b"\x00" * (64 * 1024)
    _patch_get(
        monkeypatch, uf,
        [FakeResp(headers={"Content-Type": "audio/mpeg"}, chunks=[big] * 8)],
    )
    with pytest.raises(uf.FetchError, match="上限"):
        uf.fetch_to_session("http://pub.example/song.mp3", max_bytes=256 * 1024)
    leftovers = list((tmp_path / ".session").glob("*"))
    assert leftovers == [], f"不许留下半截文件：{leftovers}"


def test_happy_path_lands_in_session_with_mp3_suffix(env, monkeypatch, tmp_path):
    """正常一路：落会话目录、可试听地址带 `.session/`、字节数对得上。"""
    uf, _ = env
    monkeypatch.setattr(
        uf.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    payload = [b"ID3\x04\x00\x00", b"\x00" * 2048]
    _patch_get(
        monkeypatch, uf,
        [FakeResp(headers={"Content-Type": "audio/mpeg"}, chunks=payload)],
    )
    info = uf.fetch_to_session("http://pub.example/song.mp3")

    assert info["suffix"] == ".mp3", "魔数 ID3 → mp3"
    assert info["bytes"] == sum(len(c) for c in payload)
    assert Path(info["path"]).is_file()
    assert Path(info["path"]).parent == (tmp_path / ".session"), "必须住会话目录（退出即删）"
    assert info["url"] == f".session/{info['name']}", "给前端的试听地址要能直接播"


def test_suffix_from_content_type_when_url_has_none(env, monkeypatch):
    """Jamendo 那种 `?trackid=…&format=mp32` 没有扩展名 —— 靠 Content-Type 定后缀。"""
    uf, _ = env
    monkeypatch.setattr(
        uf.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    _patch_get(
        monkeypatch, uf,
        [FakeResp(headers={"Content-Type": "audio/flac"}, chunks=[b"\x00" * 512])],
    )
    info = uf.fetch_to_session("http://pub.example/download?trackid=1&format=mp32")
    assert info["suffix"] == ".flac"


# ---------------------------------------------------------------- 两个入口


def test_fetch_endpoint_surfaces_human_error(env):
    """`/cover/fetch` 把护栏的错翻成 400 + 那句人话。"""
    from fastapi.testclient import TestClient

    _, cover = env
    import server

    client = TestClient(server.app)
    res = client.post("/api/cover/fetch", json={"url": "http://127.0.0.1/x.mp3"})
    assert res.status_code == 400
    assert "内网" in res.json()["detail"]


def _run_env(monkeypatch, cover, tmp_path):
    """让 `/cover/run` 能过前置检查：替身模型 + 同步线程 + 不真跑。"""
    monkeypatch.setattr(cover, "ensure_infer_pth", lambda voice: tmp_path / f"{voice}.pth")
    monkeypatch.setattr(cover, "_gpu_guard", lambda: "")
    # ⚠️ 必须先替换 RVC_ROOT、再据它拼路径 —— 反过来写就会在**真机**上
    # 取到 `D:\RVC\.venv\Scripts\python.exe` 并把真解释器截成 0 字节
    # （2026-09-26 真事故：RVC venv 被清空，退出码 -1073741515）。
    # 两行顺序是安全前提，不是风格问题。
    monkeypatch.setattr(cover.cfg, "RVC_ROOT", tmp_path)
    venv = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv.parent.mkdir(parents=True, exist_ok=True)
    venv.write_bytes(b"")
    captured: dict = {}

    class SyncThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._t, self._a = target, args

        def start(self):
            self._t(*self._a)

    monkeypatch.setattr(cover.threading, "Thread", SyncThread)
    monkeypatch.setattr(cover, "_cover_worker", lambda raw_path, *a, **k: captured.setdefault("raw", raw_path))
    return captured


def test_run_accepts_session_source_and_passes_it_to_worker(env, monkeypatch, tmp_path):
    """★ 直链那条路：`src_name` 指向会话文件 → 交给 worker 的就是它（跑完会被删）。"""
    from fastapi.testclient import TestClient

    _, cover = env
    import server

    session = tmp_path / ".session"
    session.mkdir(parents=True, exist_ok=True)
    src = session / "cover_src_1.mp3"
    src.write_bytes(b"ID3")

    captured = _run_env(monkeypatch, cover, tmp_path)
    client = TestClient(server.app)
    res = client.post(
        "/api/cover/run",
        data={"src_name": "cover_src_1.mp3", "voice_id": "kangaroo"},
    )
    assert res.status_code == 200, res.text
    assert captured["raw"] == src


def test_run_rejects_source_outside_session(env, monkeypatch, tmp_path):
    """★ 只认会话目录里的文件：别处（尤其 outputs 根）一律不当源。"""
    from fastapi.testclient import TestClient

    _, cover = env
    import server

    outside = tmp_path / "not_session.mp3"
    outside.write_bytes(b"ID3")

    captured = _run_env(monkeypatch, cover, tmp_path)
    client = TestClient(server.app)
    res = client.post("/api/cover/run", data={"src_name": "not_session.mp3", "voice_id": "kangaroo"})
    assert res.status_code == 400
    assert "不在会话里" in res.json()["detail"]
    assert "raw" not in captured


def test_run_without_source_does_not_stick_in_running(env, monkeypatch, tmp_path):
    """★ 源不对时 `COVER_STATE` 不许变成 running —— 否则前端一直转圈等一个不存在的任务。"""
    from fastapi.testclient import TestClient

    _, cover = env
    import server

    _run_env(monkeypatch, cover, tmp_path)
    client = TestClient(server.app)
    res = client.post("/api/cover/run", data={"voice_id": "kangaroo"})
    assert res.status_code == 400
    assert cover.COVER_STATE["running"] is False
    assert cover.COVER_STATE["status"] == "idle"


def test_pitch_suggest_keeps_downloaded_source(env, monkeypatch, tmp_path):
    """★ 分析音域**不能**把下好的源删掉 —— 用户接着还要用它开跑。"""
    from fastapi.testclient import TestClient

    _, cover = env
    import server

    session = tmp_path / ".session"
    session.mkdir(parents=True, exist_ok=True)
    src = session / "cover_src_2.mp3"
    src.write_bytes(b"ID3")
    ref = cover.cfg.MEDIA_DIR / "voicebank" / "kangaroo" / "reference.wav"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_bytes(b"RIFF")

    monkeypatch.setattr(cover.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(cover, "separate_song", lambda step, stamp: (step, step))
    monkeypatch.setattr(cover, "_pitch_suggest", lambda vocals, voice: 7)

    client = TestClient(server.app)
    res = client.post(
        "/api/cover/pitch_suggest",
        data={"src_name": "cover_src_2.mp3", "voice_id": "kangaroo"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["pitch"] == 7
    assert src.is_file(), "直链下好的源必须留下（上传的那份才用完即删）"


# --------------------------------------------------- 多线程分片下载（提速）
#
# 2026-09-26 实测背景：**分片提速在真实网络上收益极不稳定** ——
# 同一个 Jamendo 源先测出 1.83x、后测出 0.52x；同一分钟内清华镜像从
# 0.01 MB/s 跳到 17.27 MB/s。所以这些用例**只钉正确性，不钉速度**
# （测速会被抖动放大成假红/假绿）。速度结论见 docs 里那条记录：
# 瓶颈在服务端限流与本机出口，不在片数。


class RangeResp(FakeResp):
    """分片请求的替身：按 Range 头切出自家的那一段。"""

    def __init__(self, body: bytes, want: str, status=206):
        rng = want.replace("bytes=", "").split("-")
        lo, hi = int(rng[0]), int(rng[1])
        super().__init__(
            status=status,
            headers={"Content-Type": "audio/mpeg", "Content-Range": f"bytes {lo}-{hi}/{len(body)}"},
            chunks=[body[lo : hi + 1]],
        )
        self._body = body
        self._want = want


def _patch_range_get(monkeypatch, uf, body: bytes, *, probe_ok=True, segment_ok=True):
    """替身 `requests.get`：区分 Range 探测、普通下载、分片下载三种请求。"""
    seen: dict = {"probe": 0, "plain": 0, "seg": []}

    class ProbeResp(FakeResp):
        def __init__(self):
            super().__init__(
                status=206 if probe_ok else 200,
                headers={"Content-Range": f"bytes 0-0/{len(body)}"} if probe_ok else {},
                chunks=[body[:1]],
            )

    def fake_get(url, **kw):
        rng = (kw.get("headers") or {}).get("Range")
        if rng == "bytes=0-0" and kw.get("allow_redirects") is False:
            seen["probe"] += 1
            return ProbeResp()
        if rng and rng.startswith("bytes=") and rng != "bytes=0-0":
            seen["seg"].append(rng)
            if not segment_ok:
                return FakeResp(status=200, headers={"Content-Type": "audio/mpeg"}, chunks=[body])
            return RangeResp(body, rng)
        seen["plain"] += 1
        return FakeResp(
            headers={"Content-Type": "audio/mpeg", "Content-Length": str(len(body))},
            chunks=[body],
        )

    monkeypatch.setattr(uf.requests, "get", fake_get)
    return seen


@pytest.fixture
def _pub_dns(monkeypatch, env):
    uf, _ = env
    monkeypatch.setattr(
        uf.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    return uf


def test_parallel_lands_identical_bytes(_pub_dns, monkeypatch, tmp_path):
    """★ 分片拼出来的文件必须与整份下载**逐字节相同**（拼错=静默坏档）。"""
    uf = _pub_dns
    # 必须大于 PARALLEL_MIN_BYTES，否则走的是单连接、测不到分片拼接
    body = b"ID3" + bytes(range(256)) * ((uf.PARALLEL_MIN_BYTES // 256) + 64)

    _patch_range_get(monkeypatch, uf, body)
    got = uf.fetch_to_session("http://pub.example/song.mp3", parallel=True)
    assert Path(got["path"]).read_bytes() == body, "分片拼接内容与原文不一致"
    assert got["bytes"] == len(body)


def test_parallel_segments_match_declared_range(_pub_dns, monkeypatch):
    """每一片都要带正确的 `Range: bytes=lo-hi`，且合起来覆盖全文、无重叠。"""
    uf = _pub_dns
    # 必须超过 PARALLEL_MIN_BYTES，否则退回单连接 —— 那时一片都不会发（见上条同类坑）
    body = b"ID3" + b"\x00" * (2 * uf.PARALLEL_MIN_BYTES)
    seen = _patch_range_get(monkeypatch, uf, body)

    uf.fetch_to_session("http://pub.example/song.mp3", parallel=True)

    assert seen["seg"], "应当发出分片请求"
    spans = []
    for rng in seen["seg"]:
        lo, hi = (int(x) for x in rng.replace("bytes=", "").split("-"))
        spans.append((lo, hi))
    spans.sort()
    assert spans[0][0] == 0, "第一片必须从头开始"
    assert spans[-1][1] == len(body) - 1, "最后一片必须到文件尾"
    for (_, prev_hi), (next_lo, _) in zip(spans, spans[1:]):
        assert next_lo == prev_hi + 1, f"分片区间必须无缝衔接，实际 {prev_hi} → {next_lo}"


def test_falls_back_to_single_when_range_unsupported(_pub_dns, monkeypatch):
    """★ 服务端不支持 Range → **静默退回单连接**，不能报错。

    提速是优化、不是前提：不支持分片的源（很多网盘直链就是）必须照样能下。
    """
    uf = _pub_dns
    body = b"ID3" + b"x" * 3000
    seen = _patch_range_get(monkeypatch, uf, body, probe_ok=False)

    got = uf.fetch_to_session("http://pub.example/song.mp3", parallel=True)

    assert seen["plain"] >= 1, "应当退回普通请求"
    assert seen["seg"] == [], "探测失败就不该再发分片请求"
    assert Path(got["path"]).read_bytes() == body


def test_server_ignoring_range_aborts_instead_of_corrupting(_pub_dns, monkeypatch, tmp_path):
    """★ 服务端忽略 Range 回 200 全量时，**必须整体失败** —— 拼进去就是坏档。

    这一条防的是最阴的失败模式：文件下下来了、大小看着也对，但内容是错的。
    """
    uf = _pub_dns
    body = b"ID3" + b"y" * (uf.PARALLEL_MIN_BYTES + 4096)
    _patch_range_get(monkeypatch, uf, body, segment_ok=False)

    with pytest.raises(uf.FetchError, match="不支持分片"):
        uf.fetch_to_session("http://pub.example/song.mp3", parallel=True)

    leftovers = list((tmp_path / ".session").glob("*"))
    assert leftovers == [], f"失败后不许留下半截文件：{leftovers}"


def test_parallel_respects_max_bytes(_pub_dns, monkeypatch, tmp_path):
    """分片模式下**大小上限照样拦**（不能因为换了写法就漏掉护栏）。"""
    uf = _pub_dns
    body = b"ID3" + b"z" * (4 * uf.PARALLEL_MIN_BYTES)
    _patch_range_get(monkeypatch, uf, body)

    with pytest.raises(uf.FetchError, match="上限"):
        uf.fetch_to_session("http://pub.example/song.mp3", max_bytes=64 * 1024, parallel=True)

    assert list((tmp_path / ".session").glob("*")) == []


def test_still_rejects_html_in_parallel_mode(_pub_dns, monkeypatch):
    """★ 分片路径也要先嗅探文件头 —— 别拿 16 条分片去下一个 HTML 页面。"""
    uf = _pub_dns
    html = b"<!doctype html><html><body>nope</body></html>"

    def fake_get(url, **kw):
        return FakeResp(
            headers={"Content-Type": "text/html", "Content-Length": str(len(html))},
            chunks=[html],
        )

    monkeypatch.setattr(uf.requests, "get", fake_get)
    with pytest.raises(uf.FetchError, match="网页"):
        uf.fetch_to_session("http://pub.example/song.mp3", parallel=True)


def test_small_file_skips_parallel_probe(_pub_dns, monkeypatch):
    """小于阈值的小文件不值得分片 —— 省掉一轮探测往返。"""
    uf = _pub_dns
    body = b"ID3" + b"a" * 100  # 远小于 PARALLEL_MIN_BYTES
    assert len(body) < uf.PARALLEL_MIN_BYTES
    seen = _patch_range_get(monkeypatch, uf, body)

    uf.fetch_to_session("http://pub.example/song.mp3", parallel=True)

    assert seen["probe"] == 0, "小文件不该做 Range 探测"
    assert seen["seg"] == [], "小文件不该分片"
