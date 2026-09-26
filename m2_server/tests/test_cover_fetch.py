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
