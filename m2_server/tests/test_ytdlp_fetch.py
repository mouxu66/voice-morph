"""「在线扒歌」的守卫测试（`ytdlp_fetch` 的站点白名单 + 外部工具桥）。

这个功能的失败模式与「粘直链」**不同**，值得单独钉：

    粘直链的护栏是"别让我成为内网探测器"（自己发 HTTP，逐跳校验 IP）；
    这里的护栏是"别让我成为任意命令执行器/任意下载器" —— 因为真正发请求的是
    yt-dlp，Python 侧管不到它内部。所以能守的只有两条：

        · **站点白名单**：没有它，`/api/ytdlp/fetch?url=` 就是一个"拿任意 URL
          让服务器跑 yt-dlp"的万能口子（yt-dlp 支持上千站，等于放开整个互联网）；
        · **产物必须落会话目录**：否则"退出即删"失效，用户的歌会漏在别处。

另外两条**人话**也必须钉住，因为它们决定了"报错时用户能不能自己修"：

        · 没装 yt-dlp → 必须明说 + 给出装法，不能是"拉取失败"这种废话；
        · 版本旧认不出链接 → 必须指向"升级 yt-dlp"，而不是让用户以为是歌的问题。

不碰网络、不真跑 yt-dlp：所有 subprocess 都用替身。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def env(monkeypatch, tmp_path):
    """把会话目录指到 tmp_path，返回 (ytdlp_fetch, ytdlp_api)。

    `session_dir` 必须补丁掉：`fetch()` 会真的往那里写文件，不补就会在真机上
    的 outputs/.session 里造垃圾（"随用随删"是运行时的承诺，测试不该依赖它）。
    """
    import config as cfg

    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(cfg, "MEDIA_DIR", tmp_path / "media")
    import ytdlp_api
    import ytdlp_fetch

    return ytdlp_fetch, ytdlp_api


class FakeProc:
    """假 subprocess.run 的返回值。"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_tool(monkeypatch, yf, tmp_path, *, rc=0, stderr="", produces=("mp3", 2048)):
    """装一个"存在的 yt-dlp"替身，并拦掉全部 subprocess.run。

    `produces` 控制"它写完盘之后留下什么"：`(后缀, 字节数)` 或 `None`（什么都不留）。
    后者用来测"跑成功了但没拿到音频"这条真实会发生的失败（歌曲有版权限制时）。
    """
    exe = tmp_path / "yt-dlp.exe"
    exe.write_bytes(b"stub")
    monkeypatch.setattr(yf, "locate", lambda: exe)

    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if produces is not None:
            suffix, size = produces
            # yt-dlp 的真实行为：按 `-o` 模板落到 `-P` 指定的目录
            out_dir = Path(cmd[cmd.index("-P") + 1])
            stem = cmd[cmd.index("-o") + 1].split(".%(")[0]
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{stem}.{suffix}").write_bytes(b"A" * size)
        return FakeProc(returncode=rc, stdout="", stderr=stderr)

    monkeypatch.setattr(yf.subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------- 站点白名单


def test_rejects_ip_based_urls(env):
    """★ 回环/链路本地**字面 IP** 必须被拒（白名单天然覆盖，但要显式钉住）。

    这条单列出来，是因为「127.0.0.1 不是白名单成员」这件事**容易在重构里被破坏** ——
    比如有人把白名单改成"允许 IP 段内的地址"或者加个 `startswith("music.")` 的
    宽松匹配，回环就漏了。而它一旦漏了，这个端点就是内网探测器。
    """
    yf, _ = env
    for url in (
        "http://127.0.0.1:8000/x.mp3",
        "http://169.254.169.254/latest/meta-data",
        "http://192.168.1.9/song.mp3",
        "http://[::1]/x.mp3",
    ):
        with pytest.raises(yf.YtdlpError, match="暂不支持"):
            yf._check_supported(url)


def test_host_match_is_exact_not_prefix(env):
    """★ 必须**精确匹配**主机名，不能用前缀/后缀包含。

    这是最容易被写错的一条：`"music.163.com" in url` 或 `url.startswith("https://music.163.com")`
    都会把 `music.163.com.evil.example` 放进来（攻击者只需买一个子域）。
    `_check_supported` 走的是 `hostname` 精确查表，所以这里能过。
    """
    yf, _ = env
    for spoof in (
        "https://music.163.com.evil.example/song",
        "https://notmusic.163.com/song",
        "https://evil.example/?q=music.163.com",
        "https://y.qq.com.evil.example/x",
    ):
        with pytest.raises(yf.YtdlpError, match="暂不支持"):
            yf._check_supported(spoof)


@pytest.mark.parametrize(
    "url",
    [
        # 真正的 IP 直连：yt-dlp 的 generic extractor 会照单全收 → 必须拒
        "http://127.0.0.1:8000/x.mp3",
        "http://169.254.169.254/latest/meta-data",
        "http://192.168.1.9/song.mp3",
        # 公网但不是"取歌"场景的站：白名单的意义就在这里
        "https://www.youtube.com/watch?v=abc",
        "https://example.com/song.mp3",
        "https://music.163.com.evil.example/song",  # 后缀伪装，不能被 startswith 骗过
    ],
)
def test_rejects_hosts_outside_allowlist(env, url):
    """★ 白名单外的站点一律拒 —— 否则这个端点就是"服务器任意下载器"。"""
    yf, _ = env
    with pytest.raises(yf.YtdlpError, match="暂不支持|看不出站点|只支持"):
        yf.fetch(url)


def test_rejects_non_http_scheme(env):
    yf, _ = env
    for url in ("file:///C:/Windows/win.ini", "ftp://music.163.com/a.mp3"):
        with pytest.raises(yf.YtdlpError, match="http"):
            yf.fetch(url)


def test_rejects_empty_url(env):
    yf, _ = env
    with pytest.raises(yf.YtdlpError, match="粘一条"):
        yf.fetch("   ")


@pytest.mark.parametrize(
    "url,site",
    [
        ("https://music.163.com/song?id=123", "网易云音乐"),
        ("https://y.qq.com/n/ryqq/songDetail/abc", "QQ音乐"),
        ("https://www.bilibili.com/video/BV1xx", "哔哩哔哩"),
        ("https://b23.tv/abcdef", "哔哩哔哩（短链）"),
    ],
)
def test_accepts_allowlisted_sites(env, monkeypatch, tmp_path, url, site):
    """白名单内的站点能走到"调 yt-dlp"这一步，且返回命中站点名。"""
    yf, _ = env
    _fake_tool(monkeypatch, yf, tmp_path)
    got = yf.fetch(url)
    assert got["site"] == site


def test_scheme_less_url_is_tolerated(env, monkeypatch, tmp_path):
    """从 App 复制出来的分享链接常常没有 `https://` 前缀 —— 不该因此被拒。"""
    yf, _ = env
    _fake_tool(monkeypatch, yf, tmp_path)
    assert yf.fetch("music.163.com/song?id=1")["site"] == "网易云音乐"


# ---------------------------------------------------------------- 外部工具缺失


def test_missing_tool_gives_actionable_message(env, monkeypatch):
    """★ 没装 yt-dlp 时必须是"怎么装"的人话，不是"拉取失败"。"""
    yf, _ = env
    monkeypatch.setattr(yf, "locate", lambda: None)
    with pytest.raises(yf.YtdlpError) as ei:
        yf.fetch("https://music.163.com/song?id=1")
    msg = str(ei.value)
    assert yf.ENV_VAR in msg, "要指出用哪个环境变量指定路径"
    assert "PATH" in msg or "重启" in msg, "要给出下一步动作"


def test_probe_reports_unavailable_without_crashing(env, monkeypatch):
    """探活是页面挂载就调的，缺工具时必须正常返回而不是抛异常。"""
    yf, _ = env
    monkeypatch.setattr(yf, "locate", lambda: None)
    got = yf.probe()
    assert got["available"] is False
    assert got["sites"], "即使工具不在，也该显示支持哪些站点"
    assert got["hint"]


def test_probe_reads_version(env, monkeypatch, tmp_path):
    yf, _ = env
    _fake_tool(monkeypatch, yf, tmp_path)
    monkeypatch.setattr(
        yf.subprocess, "run", lambda cmd, **kw: FakeProc(stdout="2026.03.17\n")
    )
    got = yf.probe()
    assert got["available"] is True
    assert got["version"] == "2026.03.17"


# ---------------------------------------------------------------- 输出落点与清理


def test_output_lands_in_session_dir(env, monkeypatch, tmp_path):
    """★ 产物必须落会话目录，且返回值同形于 `url_fetch`（链路才能直接吃）。

    注意口径：`session_out.session_dir()` 是 `<OUTPUTS_DIR>/.session`，**不是**
    `OUTPUTS_DIR` 本身。断言要跟这个真实口径走，否则测试会自己骗自己
    （第一版就写成了 `tmp_path`，红了才发现是我读错了目录层级）。
    """
    yf, _ = env
    calls = _fake_tool(monkeypatch, yf, tmp_path)
    got = yf.fetch("https://music.163.com/song?id=1")

    session_dir = tmp_path / ".session"
    cmd = calls[0]
    assert "-P" in cmd and cmd[cmd.index("-P") + 1] == str(session_dir)
    assert Path(got["path"]).parent == session_dir
    assert got["bytes"] == 2048
    # 与 `url_fetch.fetch_to_session` 同形的字段（少一个上游就会 KeyError）
    for key in ("name", "path", "url", "suffix", "bytes"):
        assert key in got, f"缺字段 {key}"


def test_stale_file_outside_session_is_rejected(env, monkeypatch, tmp_path):
    """★ 防的是最阴的一种：yt-dlp 因配置把文件写到了会话目录**之外**。

    这时绝不能返回一个会话外的路径 —— 「退出即删」的保证会当场失效，
    而且上游会去删一个它不该碰的文件。
    """
    yf, _ = env
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)

    exe = tmp_path / "yt-dlp.exe"
    exe.write_bytes(b"stub")
    monkeypatch.setattr(yf, "locate", lambda: exe)

    def fake_run(cmd, **kw):
        # 故意无视 -P，写到外面去
        (outside / "ytdlp_x.mp3").write_bytes(b"B" * 100)
        return FakeProc()

    monkeypatch.setattr(yf.subprocess, "run", fake_run)
    # 会话目录里没有产物 → 报"没拿到音频"（而不是把外部文件当成果端出去）
    with pytest.raises(yf.YtdlpError, match="没有音频文件"):
        yf.fetch("https://music.163.com/song?id=1")


def test_cleanup_only_removes_this_run(env, monkeypatch, tmp_path):
    """★ 失败收尾只能删**这次**留下的，不能清空会话目录。

    会话目录里可能躺着用户刚下好、还没跑翻唱的那首歌 —— 清空它等于
    "扒歌失败把用户的歌也弄丢了"。
    """
    yf, _ = env
    keeper = tmp_path / "cover_src_1700000000.mp3"
    keeper.write_bytes(b"PRECIOUS")

    _fake_tool(monkeypatch, yf, tmp_path, rc=1, stderr="ERROR: boom", produces=None)
    with pytest.raises(yf.YtdlpError):
        yf.fetch("https://music.163.com/song?id=1")

    assert keeper.exists(), "不属于本次的文件绝不能被删"


def test_failed_run_cleans_its_own_leftovers(env, monkeypatch, tmp_path):
    """本次的半截产物要清掉，别让下次的探针把它当成结果。"""
    yf, _ = env
    _fake_tool(monkeypatch, yf, tmp_path, rc=1, stderr="ERROR: boom", produces=("part", 10))
    with pytest.raises(yf.YtdlpError):
        yf.fetch("https://music.163.com/song?id=1")
    assert list(tmp_path.glob("ytdlp_*")) == []


def test_no_audio_produced_is_an_error(env, monkeypatch, tmp_path):
    """跑成功了但没留下音频（版权限制的常见表现）→ 必须是错误，不能返回空结果。"""
    yf, _ = env
    _fake_tool(monkeypatch, yf, tmp_path, produces=None)
    with pytest.raises(yf.YtdlpError, match="没有音频文件"):
        yf.fetch("https://music.163.com/song?id=1")


def test_zero_byte_output_is_an_error(env, monkeypatch, tmp_path):
    yf, _ = env
    _fake_tool(monkeypatch, yf, tmp_path, produces=("mp3", 0))
    with pytest.raises(yf.YtdlpError, match="空的"):
        yf.fetch("https://music.163.com/song?id=1")


# ---------------------------------------------------------------- 命令构造


def test_command_pins_audio_only_and_output_path(env, monkeypatch, tmp_path):
    """命令必须：只取音频、钉死输出目录（`-P`）、单曲语义、URL 放在 `--` 之后。"""
    yf, _ = env
    calls = _fake_tool(monkeypatch, yf, tmp_path)
    yf.fetch("https://music.163.com/song?id=1")
    cmd = calls[0]

    assert "-x" in cmd, "必须只要音频，不下载视频流"
    assert "--no-playlist" in cmd, "粘歌单链接时只取第一首"
    assert "--" in cmd and cmd[-1] == "https://music.163.com/song?id=1", (
        "URL 必须在 `--` 之后，防它被当成选项解析"
    )
    assert cmd[cmd.index("-P") + 1] == str(tmp_path / ".session")


def test_timeout_is_reported_as_human_message(env, monkeypatch, tmp_path):
    yf, _ = env
    exe = tmp_path / "yt-dlp.exe"
    exe.write_bytes(b"stub")
    monkeypatch.setattr(yf, "locate", lambda: exe)

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

    monkeypatch.setattr(yf.subprocess, "run", boom)
    with pytest.raises(yf.YtdlpError, match="秒没拉完"):
        yf.fetch("https://music.163.com/song?id=1")


@pytest.mark.parametrize(
    "stderr,expect",
    [
        ("ERROR: Sign in to confirm", "登录"),
        ("ERROR: Unsupported URL: https://x", "升级"),
        ("ERROR: This video is not available in your country", "版权"),
        ("ERROR: HTTP Error 403: Forbidden", "403"),
    ],
)
def test_stderr_is_translated_to_human(env, monkeypatch, tmp_path, stderr, expect):
    """★ 原始 stderr 是几百行下载日志，直接甩给用户等于没报错。

    必须归因到"下一步做什么"（登录 / 升级 / 换来源）。
    """
    yf, _ = env
    _fake_tool(monkeypatch, yf, tmp_path, rc=1, stderr=stderr, produces=None)
    with pytest.raises(yf.YtdlpError) as ei:
        yf.fetch("https://music.163.com/song?id=1")
    assert expect in str(ei.value), f"期望提示含 {expect!r}，实际：{ei.value}"


# ---------------------------------------------------------------- HTTP 层


def test_endpoint_turns_error_into_400(env, monkeypatch):
    """HTTP 层把 `YtdlpError` 转成 400 + 人话 detail（输入/环境问题不是 500）。"""
    yf, ya = env
    monkeypatch.setattr(yf, "locate", lambda: None)

    import asyncio

    from fastapi import HTTPException

    async def go():
        return await ya.ytdlp_fetch_endpoint(ya.YtdlpFetchReq(url="https://music.163.com/song?id=1"))

    with pytest.raises(HTTPException) as ei:
        asyncio.run(go())
    assert ei.value.status_code == 400
    assert yf.ENV_VAR in str(ei.value.detail)


def test_endpoint_returns_preview_url(env, monkeypatch, tmp_path):
    """成功时返回可直接试听的地址 + 站点名 + 时长。"""
    yf, ya = env
    _fake_tool(monkeypatch, yf, tmp_path)

    import asyncio

    async def go():
        return await ya.ytdlp_fetch_endpoint(ya.YtdlpFetchReq(url="https://music.163.com/song?id=1"))

    got = asyncio.run(go())
    assert got["ok"] is True
    assert got["site"] == "网易云音乐"
    assert got["url"].startswith("/media/session/")
    assert got["bytes"] == 2048
