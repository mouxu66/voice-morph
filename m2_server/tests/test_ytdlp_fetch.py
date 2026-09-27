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
    # 登录态环境变量必须清掉：它们会改变报错文案与命令行构造。
    # 留在环境里，测试跑的就不再是"匿名用户"的现场了 —— 这种"我这儿是绿的"
    # 最难查，因为 CI 和本机的环境变量不一样。
    monkeypatch.delenv("VM_YTDLP_COOKIES", raising=False)
    monkeypatch.delenv("VM_YTDLP_COOKIES_BROWSER", raising=False)
    import ytdlp_api
    import ytdlp_fetch

    # ★ 默认**禁网**：`normalize` 在本地抠不出 ID 时要去解短链（真发 HTTP）。
    # 不封住的话，一个忘了断言的用例就会往公网打请求 —— 测试必须能离线跑。
    # 想测解短链的用例自己再覆盖这个替身（见"分享链接规范化"一节）。
    def no_net(url):
        raise OSError("本测试不允许解短链")

    monkeypatch.setattr(ytdlp_fetch, "_resolve_redirect", no_net)

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
    """★ 主机名必须**精确**匹配，或按**点号锚定**的域名族匹配，不能用裸前缀/包含。

    这是最容易被写错的一条：`"music.163.com" in url` 或 `url.startswith("https://music.163.com")`
    都会把 `music.163.com.evil.example` 放进来（攻击者只需买一个子域）。

    2026-09-27 引入了域名族（`_SUPPORTED_FAMILIES`，为了放行 QQ 音乐的
    `c6.y.qq.com` 这类分享子域），所以这里补上三组针对性反例 ——
    族匹配一旦写成 `endswith(domain)` 而漏掉那个点，`noty.qq.com` 就会漏进来。
    """
    yf, _ = env
    for spoof in (
        "https://music.163.com.evil.example/song",
        "https://notmusic.163.com/song",
        "https://evil.example/?q=music.163.com",
        "https://y.qq.com.evil.example/x",
        # ↓ 域名族引入后的针对性反例
        "https://noty.qq.com/x",  # 以 `y.qq.com` 结尾，但**不是**它的子域
        "https://evilqq.com/x",  # 连主干名都不是
        "https://music.163.com.attacker.io/song",  # 拿真域名当前缀
        "https://xymusic.163.com/song",
    ):
        with pytest.raises(yf.YtdlpError, match="暂不支持"):
            yf._check_supported(spoof)


def test_platform_share_subdomains_are_allowed(env):
    """★ 域名族要放行的东西：这些平台的分享链接在自家子域之间跳。

    `c6.y.qq.com` 是 2026-09-27 用户真实踩到的那个 —— 从 QQ 音乐 App 点「分享」
    拿到的就是它，而当时的精确白名单把它当成了陌生站点，报"暂不支持 c6.y.qq.com"。
    逐个列举没有出路（QQ 的短链域是 c1~c9），所以改成点号锚定的族。
    """
    yf, _ = env
    cases = {
        "https://c6.y.qq.com/base/fcgi-bin/u?__=x": "QQ音乐",
        "https://i2.y.qq.com/n3/other/pages/playsong/index.html": "QQ音乐",
        "https://y.music.163.com/m/song?id=1": "网易云音乐",
        "https://m.music.163.com/m/song?id=1": "网易云音乐",
        "https://m.bilibili.com/video/BV1xx": "哔哩哔哩",
        "https://www.ximalaya.com/sound/1": "喜马拉雅",
    }
    for url, site in cases.items():
        assert yf._check_supported(url) == site, url


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
    with pytest.raises(yf.YtdlpError, match="没拿到音频"):
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
    with pytest.raises(yf.YtdlpError, match="没拿到音频"):
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


# ---------------------------------------------------------------- 分享链接规范化
#
# 2026-09-27 用户给的真实链接引出的问题：从 QQ 音乐 App 分享出来的是
# `c6.y.qq.com/base/fcgi-bin/u?__=xxx`，它 302 三级跳到
# `y.qq.com/n/ryqq_v2/songDetail/<mid>`，而 yt-dlp 的 qqmusic extractor
# **只认** `/n/ryqq/songDetail/<mid>`，对另外两种都报 `Unsupported URL`。
# 所以"粘进去没反应"里有一大类不是版权问题，是地址形式问题。

REAL_QQ_SHORT = "https://c6.y.qq.com/base/fcgi-bin/u?__=yY3vbmLH9kYO"
#: 实测抓到的落地页（不是编的）：短链 302 之后到的地方
REAL_QQ_LANDING = (
    "https://i2.y.qq.com/n3/other/pages/playsong/index.html?ADTAG=cbshare"
    "&appshare=android_qq&songmid=0023jgxa0Ym5yo&type=0"
)
QQ_CANON = "https://y.qq.com/n/ryqq/songDetail/0023jgxa0Ym5yo"


def test_qq_share_link_is_rewritten_to_canonical(env, monkeypatch, tmp_path):
    """★ 用户那条真实链接：短链 → 落地页 → 规范地址。

    `_resolve_redirect` 的替身返回的是**实测抓到的那一跳**，不是编的地址。
    """
    yf, _ = env
    monkeypatch.setattr(yf, "_resolve_redirect", lambda url: REAL_QQ_LANDING)
    calls = _fake_tool(monkeypatch, yf, tmp_path)

    got = yf.fetch(REAL_QQ_SHORT)

    assert calls[0][-1] == QQ_CANON, "交给 yt-dlp 的必须是它认的规范地址"
    assert got["site"] == "QQ音乐"
    assert got["source_url"] == QQ_CANON


@pytest.mark.parametrize(
    "url",
    [
        # yt-dlp 认 /n/ryqq/songDetail/，**不认** /n/ryqq_v2/songDetail/（本机实测）
        "https://y.qq.com/n/ryqq_v2/songDetail/0023jgxa0Ym5yo?ADTAG=h5_play_song",
        "https://i.y.qq.com/v8/playsong.html?songmid=0023jgxa0Ym5yo",
        "https://y.qq.com/n/yqq/song/0023jgxa0Ym5yo.html",
        "https://y.qq.com/#/songDetail/0023jgxa0Ym5yo",
    ],
)
def test_qq_url_forms_are_normalized_without_network(env, url):
    """★ 本地就能抠出 songmid 的写法一律改写，**且一步网络都不发**。

    为什么"无网络"要在测试里钉住：`env` 把 `_resolve_redirect` 换成了抛异常的替身，
    所以只要某个实现偷偷走了"解短链"那条分支，这里就会炸 —— 这比断言"请求次数为 0"
    更直接，也不依赖任何网络替身。
    """
    yf, _ = env
    assert yf.normalize(url) == QQ_CANON


def test_netease_hash_route_is_normalized(env):
    """网易云的分享链接是 hash 路由（`/#/song?id=`），id 在 fragment 上，不在 query。"""
    yf, _ = env
    expect = "https://music.163.com/song?id=1978534"
    assert yf.normalize("https://music.163.com/#/song?id=1978534") == expect
    assert yf.normalize("https://y.music.163.com/m/song?id=1978534") == expect


def test_sites_without_an_id_rule_are_left_alone(env):
    """没有 ID 提取规则的站点原样放过 —— 规范化是"尽力而为"，不是关卡。

    b23.tv 这种 yt-dlp 自己会跳，硬改写反而可能把本来能用的链接改坏。
    """
    yf, _ = env
    for url in (
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://b23.tv/abcdef",
        "https://www.jamendo.com/track/1978534/eternal-echoes",
    ):
        assert yf.normalize(url) == url, url


def test_untrusted_redirect_target_is_not_trusted(env, monkeypatch):
    """★★ 安全：短链跳到白名单**之外**时，不能采信跳转结果。

    这是这一步唯一可能开出的口子 —— `normalize` 会真发一次 HTTP（跟着 302 走）。
    如果它把"最终地址"直接交给 yt-dlp，那白名单就等于让给了任意服务端重定向
    （在任意被放行的站点上找一个开放重定向即可绕过）。
    正确做法：**只用跳转结果去抠 ID，再用 ID 拼一条自己构造的地址**；
    跳转目标本身一律不采信。
    """
    yf, _ = env
    monkeypatch.setattr(yf, "_resolve_redirect", lambda url: "https://evil.example/steal")
    assert yf.normalize(REAL_QQ_SHORT) == REAL_QQ_SHORT


def test_evil_redirect_never_reaches_the_command_line(env, monkeypatch, tmp_path):
    """★★ 上一条的"落地"验证：真正传给 yt-dlp 的是原链接，绝不是跳转目标。

    只看 `normalize` 的返回值还不够 —— 这里验的是最终落到 `subprocess` 参数里的东西。
    """
    yf, _ = env
    monkeypatch.setattr(yf, "_resolve_redirect", lambda url: "https://evil.example/steal")
    calls = _fake_tool(monkeypatch, yf, tmp_path)

    yf.fetch(REAL_QQ_SHORT)

    assert calls[0][-1] == REAL_QQ_SHORT
    assert "evil.example" not in " ".join(calls[0])


def test_redirect_failure_falls_back_to_the_original(env):
    """解短链失败（超时/跳数超限/断网）不能让整次拉取失败。

    `env` 里的替身就是"抛异常"，直接拿它当现场 —— 规范化失败时原样交给 yt-dlp，
    由它自己去跳（它本来就能处理一部分短链）。
    """
    yf, _ = env
    assert yf.normalize(REAL_QQ_SHORT) == REAL_QQ_SHORT


def test_malformed_ids_are_rejected_not_concatenated(env):
    """★ ID 的字符集**本身就是安全性质**：抠出来的值会被拼进 URL。

    这是本模块唯一手工拼字符串的地方。`..`、`/`、`%` 一律不认，否则正则就成了
    路径穿越/改查询串的入口。长度下限只是防呆（真实 songmid 是 14 位 base62）。
    """
    yf, _ = env
    for bad in (
        "https://y.qq.com/n/ryqq/songDetail/../../etc/passwd",
        "https://y.qq.com/n/ryqq/songDetail/a%2Fb",
        "https://y.qq.com/n/ryqq/songDetail/x y",
        "https://y.qq.com/n/ryqq/songDetail/",
    ):
        assert yf._canonical_for(yf._host_of(bad), bad) is None, bad


# ---------------------------------------------------------------- 登录态（显式开关）


def test_cookies_are_off_by_default(env, monkeypatch, tmp_path):
    """★ 默认必须发匿名请求。

    `--cookies-from-browser` 会去解密用户浏览器的登录态数据库，是侵入性操作。
    这个插件是"外部工具桥"，不该顺手摸用户的浏览器凭据 —— 要用必须用户自己显式打开。
    """
    yf, _ = env
    calls = _fake_tool(monkeypatch, yf, tmp_path)
    yf.fetch("https://music.163.com/song?id=1")
    cmd = calls[0]
    assert "--cookies" not in cmd
    assert "--cookies-from-browser" not in cmd


def test_cookie_file_is_passed_through(env, monkeypatch, tmp_path):
    yf, _ = env
    jar = tmp_path / "cookies.txt"
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv(yf.ENV_COOKIES, str(jar))

    calls = _fake_tool(monkeypatch, yf, tmp_path)
    yf.fetch("https://music.163.com/song?id=1")

    cmd = calls[0]
    assert cmd[cmd.index("--cookies") + 1] == str(jar)


def test_cookie_file_wins_over_browser(env, monkeypatch, tmp_path):
    """两个都配时用文件 —— 文件是确定性的，浏览器那条在 Windows 上不可靠。

    本机实测（2026-09-27）：Chrome ≥ v127 报 `Failed to decrypt with DPAPI`；
    Edge/Chrome 只要还开着就报 `Could not copy Chrome cookie database`（库被锁）；
    而且本机 Edge 的 `Local State` 里确认有 `app_bound_encrypted_key`
    （即已启用 App-Bound Encryption）。所以默认推文件那条路。
    """
    yf, _ = env
    jar = tmp_path / "cookies.txt"
    jar.write_text("x", encoding="utf-8")
    monkeypatch.setenv(yf.ENV_COOKIES, str(jar))
    monkeypatch.setenv(yf.ENV_COOKIES_BROWSER, "edge")

    calls = _fake_tool(monkeypatch, yf, tmp_path)
    yf.fetch("https://music.163.com/song?id=1")

    cmd = calls[0]
    assert "--cookies" in cmd
    assert "--cookies-from-browser" not in cmd


def test_missing_cookie_file_is_a_loud_error(env, monkeypatch, tmp_path):
    """★ 配了却不存在 → 直接报错，不能静默退回匿名。

    静默忽略的后果：用户以为配好了，然后拿着"要登录"的报错反复试同一件事。
    """
    yf, _ = env
    monkeypatch.setenv(yf.ENV_COOKIES, str(tmp_path / "nope.txt"))
    _fake_tool(monkeypatch, yf, tmp_path)

    with pytest.raises(yf.YtdlpError) as ei:
        yf.fetch("https://music.163.com/song?id=1")
    assert yf.ENV_COOKIES in str(ei.value)


def test_cookie_browser_is_passed_through(env, monkeypatch, tmp_path):
    yf, _ = env
    monkeypatch.setenv(yf.ENV_COOKIES_BROWSER, "edge")
    calls = _fake_tool(monkeypatch, yf, tmp_path)
    yf.fetch("https://music.163.com/song?id=1")
    cmd = calls[0]
    assert cmd[cmd.index("--cookies-from-browser") + 1] == "edge"


def test_probe_reports_cookie_mode(env, monkeypatch):
    """探活要报登录态配了没 —— 前端据此提示，用户不用去猜环境变量。"""
    yf, _ = env
    monkeypatch.setattr(yf, "locate", lambda: None)
    assert yf.probe()["cookie"]["mode"] == "none"
    monkeypatch.setenv(yf.ENV_COOKIES_BROWSER, "edge")
    assert yf.probe()["cookie"]["mode"] == "browser"


def test_cookie_hint_distinguishes_configured_from_not(env, monkeypatch, tmp_path):
    """★ "没配"和"配了但过期"要给不同的话。

    两种情况用户下一步的动作完全不同：没配 → 去导 cookies；配了 → 重新导一份。
    混成一句"要登录"用户就会原地打转。
    """
    yf, _ = env
    assert yf.ENV_COOKIES in yf._cookie_hint("QQ音乐")

    jar = tmp_path / "cookies.txt"
    jar.write_text("x", encoding="utf-8")
    monkeypatch.setenv(yf.ENV_COOKIES, str(jar))
    assert "过期" in yf._cookie_hint("QQ音乐")

    monkeypatch.delenv(yf.ENV_COOKIES)
    monkeypatch.setenv(yf.ENV_COOKIES_BROWSER, "edge")
    assert "还开着" in yf._cookie_hint("QQ音乐")


# ---------------------------------------------------------------- 会话目录里的"上一首"


def test_previous_song_leftover_is_not_mistaken_for_this_run(env, monkeypatch, tmp_path):
    """★★ 上一首的残留不能被当成这一首的结果。

    会话目录里躺着上一次刚扒好的歌时，`_probe_audio_ext` 如果扫**全目录**，
    "这次什么都没产出"就会被那个残留伪装成成功 —— 用户拿到的其实是**上一首**，
    而且他多半发现不了（文件名、时长都是真的音频）。连着扒两首时必现。
    所以必须按本轮 `stem` 前缀过滤。
    """
    yf, _ = env
    old = tmp_path / ".session" / "ytdlp_1111111111.mp3"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"OLD-SONG" * 100)

    _fake_tool(monkeypatch, yf, tmp_path, produces=None)

    with pytest.raises(yf.YtdlpError, match="没拿到音频"):
        yf.fetch("https://music.163.com/song?id=1")
    assert old.exists(), "不属于本次的残留不能被删"


def test_leftover_from_a_failed_run_is_cleaned(env, monkeypatch, tmp_path):
    """"跑完但没产出"也要收掉本轮残渣，否则它会骗过下一次的探针。"""
    yf, _ = env
    _fake_tool(monkeypatch, yf, tmp_path, produces=("ytdl", 10))
    with pytest.raises(yf.YtdlpError, match="没拿到音频"):
        yf.fetch("https://music.163.com/song?id=1")
    assert list(tmp_path.glob("ytdlp_*")) == []


def test_no_audio_message_lists_the_real_causes(env, monkeypatch, tmp_path):
    """报错要给出"下一步三种可能"，不是一句"拉取失败"。"""
    yf, _ = env
    _fake_tool(monkeypatch, yf, tmp_path, produces=None)
    with pytest.raises(yf.YtdlpError) as ei:
        yf.fetch(QQ_CANON)
    msg = str(ei.value)
    assert "登录态" in msg and "yt-dlp -U" in msg
