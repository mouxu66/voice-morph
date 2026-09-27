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
    """假 `subprocess.run` 的返回值（现在只剩 `probe` 与 `taskkill` 用它）。"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Calls(list):
    """收集到的命令行，外加一个观测位（`killed` = 调了几次 taskkill）。

    做成 list 子类是为了不改既有用例的 `calls[0]` —— 而 `killed` 必须是**可变计数**
    （绑个 int 出去只会在替换时断开），所以放在实例上。
    """

    def __init__(self, *args):
        super().__init__(*args)
        self.killed = 0


class _FakeStream:
    """`proc.stdout` 的替身：可迭代、可 `close()`。

    `close()` 不是装饰 —— 产品代码在收尾时会关管道，少这一个方法就会以
    `AttributeError: 'tuple' object has no attribute 'close'` 收场，
    而那是**替身缺件**，不是产品代码的错（踩过一次）。
    """

    def __init__(self, lines=()):
        self._it = iter(lines)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def close(self):
        pass


def _fake_tool(
    monkeypatch, yf, tmp_path, *, rc=0, stderr="", produces=("mp3", 2048),
    lines=(), alive_polls=0,
):
    """装一个"存在的 yt-dlp"替身，并拦掉全部进程启动。

    `produces` 控制"它跑完留下什么"：`(后缀, 字节数)` 或 `None`（什么都不留）。
    后者用来测"跑成功了但没拿到音频"这条真实会发生的失败（歌曲有版权限制时）。

    `lines` 是它往**合并后的输出流**里逐行吐的东西 —— 进度行、报错行都走这条
    （产品代码把 stderr 并进了 stdout，见 `_run_tool`）。

    `alive_polls` 是"它还活着几轮"，0 = 第一次 `poll()` 就报已退出。
    给看门狗/取消那几条用例制造"进程一直不退出"的现场。

    ★ 替身做在 `Popen` 上而不是 `run` 上：`run` 的 timeout 只杀直接子进程，
    而这次的改动正是**不用 `run`**（要按进程树杀），做在 `run` 上就测不到那条路。
    """
    exe = tmp_path / "yt-dlp.exe"
    exe.write_bytes(b"stub")
    monkeypatch.setattr(yf, "locate", lambda: exe)

    calls = Calls()

    class _Popen:
        def __init__(self, cmd, **kw):
            calls.append(list(cmd))
            self.pid = 4242
            self.returncode = None
            self._left = alive_polls
            self.kw = kw
            if produces is not None:
                suffix, size = produces
                # yt-dlp 的真实行为：按 `-o` 模板落到 `-P` 指定的目录
                out_dir = Path(cmd[cmd.index("-P") + 1])
                stem = cmd[cmd.index("-o") + 1].split(".%(")[0]
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{stem}.{suffix}").write_bytes(b"A" * size)
            self.stdout = _FakeStream([*lines] + ([stderr] if stderr else []))

        def poll(self):
            if self._left > 0:
                self._left -= 1
                return None
            if self.returncode is None:
                self.returncode = rc
            return self.returncode

        def wait(self, timeout=None):  # noqa: ARG002 —— 替身不等
            self.returncode = rc
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(yf.subprocess, "Popen", _Popen)

    def fake_run(cmd, **kw):  # noqa: ARG001
        # `_kill_tree` 会用 `subprocess.run(["taskkill", ...])` —— 替掉，
        # 否则测试会拿假 pid（4242）去杀真机上的进程。只记数，不真杀。
        if isinstance(cmd, (list, tuple)) and cmd and "taskkill" in str(cmd[0]).lower():
            calls.killed += 1
        return FakeProc()

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

    class _Popen:
        pid = 4242
        returncode = 0
        stdout = _FakeStream()

        def __init__(self, cmd, **kw):  # noqa: ARG002
            # 故意无视 -P，写到外面去
            (outside / "ytdlp_x.mp3").write_bytes(b"B" * 100)

        def poll(self):
            return 0

    monkeypatch.setattr(yf.subprocess, "Popen", _Popen)
    monkeypatch.setattr(yf.subprocess, "run", lambda cmd, **kw: FakeProc())
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
    """墙钟总上限。注意 idle 判定要关掉（`idle_s` 拉高），否则先被"卡住"那条拦住。"""
    yf, _ = env
    monkeypatch.setattr(yf, "_POLL_S", 0.02)
    _fake_tool(monkeypatch, yf, tmp_path, produces=None, alive_polls=10_000)
    monkeypatch.setattr(yf, "_IDLE_TIMEOUT_S", 999)
    with pytest.raises(yf.YtdlpError, match="秒没拉完"):
        yf.fetch("https://music.163.com/song?id=1", timeout_s=0.05)


# ------------------------------------------------- 取回必须能停（取消 / 卡住 / 进程树）
#
# 2026-09-27 加。改动前的样子：`subprocess.run(timeout=600)`。粘一条坏链接，前端
# 只能干等十分钟，用户唯一能做的是关页面 —— 而后台 yt-dlp 还在跑。而且 `run` 的
# 超时只 kill **直接子进程**，yt-dlp 拉起的 ffmpeg 会变成孤儿继续往会话目录里写。


def test_cancel_stops_the_run_and_kills_the_tree(env, monkeypatch, tmp_path):
    """★ 取消要真的停：用户改主意时正在跑的 yt-dlp 必须**当场**被中止。

    替身让进程一直"活着"（`alive_polls` 很大），作业在另一条线程上被取消 ——
    这正是前端点「取消」或关页面时的现场。
    """
    import threading

    yf, _ = env
    monkeypatch.setattr(yf, "_POLL_S", 0.02)
    calls = _fake_tool(monkeypatch, yf, tmp_path, produces=None, alive_polls=10_000)

    job = yf.new_job("job-cancel-0001")
    timer = threading.Timer(0.15, job.cancel)
    timer.start()
    try:
        with pytest.raises(yf.YtdlpError, match="已取消"):
            yf.fetch("https://music.163.com/song?id=1", timeout_s=3, job=job)
    finally:
        timer.cancel()

    assert calls.killed >= 1, "必须按**进程树**杀（taskkill /T），否则 ffmpeg 变孤儿继续写"

    # 取消后不能留下半截文件 —— 本轮的 stem 前缀会被下一次的探针当成产物
    assert list((tmp_path / ".session").glob("ytdlp_*")) == []


def test_cancel_before_start_does_not_run_the_tool(env, monkeypatch, tmp_path):
    """已经取消的票不该再起进程（点了取消又马上重发同一条链接的竞态）。"""
    yf, _ = env
    monkeypatch.setattr(yf, "_POLL_S", 0.02)
    calls = _fake_tool(monkeypatch, yf, tmp_path, produces=None, alive_polls=10_000)

    job = yf.new_job("job-cancel-0002")
    job.cancel()
    with pytest.raises(yf.YtdlpError, match="已取消"):
        yf.fetch("https://music.163.com/song?id=1", timeout_s=3, job=job)
    assert calls.killed >= 1
    assert list((tmp_path / ".session").glob("ytdlp_*")) == []


def test_silent_run_is_aborted_by_the_idle_watchdog(env, monkeypatch, tmp_path):
    """★ 干等十分钟的真正成因是"挂着不动"，而它**不报错、不退出**。

    墙钟上限拦不住它（600s 一到用户早放弃了），所以另有一条"连续无输出"判定。
    报错必须说清是「卡住」而不是「超时」—— 两者用户要做的事不一样。
    """
    yf, _ = env
    monkeypatch.setattr(yf, "_POLL_S", 0.02)
    # 阈值按测试节奏调小：真实值是 90s，量的是同一套逻辑，不必真等 90 秒
    monkeypatch.setattr(yf, "_IDLE_TIMEOUT_S", 0.1)
    calls = _fake_tool(monkeypatch, yf, tmp_path, produces=None, alive_polls=10_000)

    with pytest.raises(yf.YtdlpError, match="没有任何输出"):
        yf.fetch("https://music.163.com/song?id=1", timeout_s=3)

    assert calls.killed >= 1
    assert list((tmp_path / ".session").glob("ytdlp_*")) == []


def test_idle_threshold_is_read_at_call_time(env, monkeypatch, tmp_path):
    """★ 阈值必须在**调用时**读模块常量，不能写成函数的默认参数值。

    写成 `idle_s: float = _IDLE_TIMEOUT_S` 会在**定义时**就把 90 求值进去，
    之后调参（或像上面那样打桩）一律无效 —— 而现象是"打桩了但没用"，
    查起来要先怀疑自己的测试写错了。这条用例就是钉住那一行。
    """
    yf, _ = env
    monkeypatch.setattr(yf, "_POLL_S", 0.02)
    monkeypatch.setattr(yf, "_IDLE_TIMEOUT_S", 0.1)
    _fake_tool(monkeypatch, yf, tmp_path, produces=None, alive_polls=10_000)
    with pytest.raises(yf.YtdlpError, match="没有任何输出"):
        yf.fetch("https://music.163.com/song?id=1", timeout_s=3)


def test_wall_clock_threshold_is_read_at_call_time(env, monkeypatch, tmp_path):
    """同上，但盯的是**墙钟上限**：`fetch` 的 `timeout_s=None` 必须落到调用时的 `_TIMEOUT_S`。

    同一个坑踩了两次：`fetch(timeout_s: int = _TIMEOUT_S)` 也是"定义时求值"，
    打桩 `_TIMEOUT_S` 之后它照样按 600 秒等。所以这里**故意不传** `timeout_s`，
    逼它走 `None → 函数体里取模块常量` 那条路。
    """
    yf, _ = env
    monkeypatch.setattr(yf, "_POLL_S", 0.02)
    monkeypatch.setattr(yf, "_IDLE_TIMEOUT_S", 999)  # 先排掉空闲看门狗，只留墙钟这一条
    monkeypatch.setattr(yf, "_TIMEOUT_S", 0.1)
    _fake_tool(monkeypatch, yf, tmp_path, produces=None, alive_polls=10_000)
    with pytest.raises(yf.YtdlpError, match="没拉完"):
        yf.fetch("https://music.163.com/song?id=1")


def test_output_keeps_the_watchdog_alive(env, monkeypatch, tmp_path):
    """反面：只要还在吐行就不算卡住 —— 否则慢网下真在下载的大文件会被误杀。"""
    yf, _ = env
    monkeypatch.setattr(yf, "_POLL_S", 0.02)
    monkeypatch.setattr(yf, "_IDLE_TIMEOUT_S", 999)
    _fake_tool(monkeypatch, yf, tmp_path, alive_polls=3, lines=["[download]  10.0% of 3MiB"])

    got = yf.fetch("https://music.163.com/song?id=1", timeout_s=999)
    assert got["bytes"] == 2048, "有输出就不该被看门狗打断"


def test_progress_is_surfaced_to_the_job(env, monkeypatch, tmp_path):
    """进度只做展示，但**必须有** —— 否则前端只能显示一个转不完的圈。"""
    yf, _ = env
    monkeypatch.setattr(yf, "_POLL_S", 0.02)
    monkeypatch.setattr(yf, "_IDLE_TIMEOUT_S", 999)
    _fake_tool(
        monkeypatch, yf, tmp_path, alive_polls=3,
        lines=["[y.qq.com] Extracting URL", "[download]  42.5% of 3MiB", "[ExtractAudio] x"],
    )

    job = yf.new_job("job-progress-01")
    yf.fetch("https://music.163.com/song?id=1", job=job)
    # 最后一行是转码 → stage 停在"转码中"；percent 保留最后一次读到的下载进度
    assert job.snapshot()["percent"] == 42.5
    assert job.snapshot()["stage"] == "转码中"


def test_forget_removes_only_its_own_ticket(env):
    """★ 摘票只摘自己那张 —— **同 id** 的后来者不能被前一个的收尾顺手抹掉。

    现场是"同一个 `job_id` 的两轮取回"：第一轮的收尾（端点的 `finally: forget`）
    与前端拿同一 id 重试的第二轮**可能交叠**。少了 `is` 判定就会把第二轮刚登记的
    票摘走，于是用户点取消时后端"查无此票" —— 又回到那个偶发的"取消按钮不灵"。

    ⚠️ 这条第一版用了**两个不同 id**，等于什么都没测：不同 id 下加不加 `is` 判定
    行为一模一样，突变跑起来是绿的（突变验证当场抓出来了）。
    """
    yf, _ = env
    old = yf.new_job("job-forget-001")
    yf.forget(old)
    assert yf.job_of("job-forget-001") is None

    # 同一个 id 的下一轮：旧票已摘，这里会**新建**一张（与 old 不是同一对象）
    newer = yf.new_job("job-forget-001")
    assert newer is not old, "场景前提：同 id 的下一轮是一张新票"

    yf.forget(old)  # 旧票的收尾迟到一步
    assert yf.job_of("job-forget-001") is newer, "不能抹掉同 id 的后来者"
    assert yf.cancel_job("job-forget-001") is True, "后来者还得能被取消"


def test_same_job_id_reuses_the_same_ticket(env):
    """★ 同一个 id 必须拿到**同一张**票。

    否则"取消请求先到、fetch 后到"时，取消会落在已经被丢弃的旧票上 ——
    表现成"取消按钮偶尔不灵"，而且只在慢请求上偶现，最难查。
    """
    yf, _ = env
    a = yf.new_job("job-same-0001")
    b = yf.new_job("job-same-0001")
    assert a is b
    assert yf.cancel_job("job-same-0001") is True
    assert a.cancelled is True, "取消必须落在 fetch 手上那张票上"
    assert yf.cancel_job("job-never-made") is False


def test_cleanup_retries_a_locked_file(env, monkeypatch, tmp_path):
    """★ 收尾必须**重试**：刚被 taskkill 的进程持有的句柄不是立刻释放的。

    原来那句 `except OSError: pass` 会把它变成静默的假成功 —— 用户看到"已取消"，
    会话目录里却躺着一个还在长的半截文件，而它带着本轮 stem 前缀，下一次
    `_probe_audio_ext` 真有可能把它当产物。
    """
    yf, _ = env
    _fake_tool(monkeypatch, yf, tmp_path, rc=1, stderr="ERROR: boom", produces=("mp3.part", 10))
    out_dir = tmp_path / ".session"
    out_dir.mkdir(parents=True, exist_ok=True)

    real_unlink = Path.unlink
    state = {"tries": 0}

    def flaky_unlink(self, missing_ok=False):  # noqa: ARG001
        if self.name.endswith(".part") and state["tries"] < 2:
            state["tries"] += 1
            raise OSError(32, "The process cannot access the file")  # WinError 32
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    monkeypatch.setattr(yf.time, "sleep", lambda _s: None)  # 别让退避拖慢测试

    with pytest.raises(yf.YtdlpError):
        yf.fetch("https://music.163.com/song?id=1")

    assert list(out_dir.glob("ytdlp_*")) == [], "占用解不开就重试，别静默留下半截文件"


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


class FakeRequest:
    """够用的 `Request` 替身：只提供端点要用的 `is_disconnected()`。"""

    def __init__(self, disconnected=False, boom=False):
        self._disconnected = disconnected
        self._boom = boom

    async def is_disconnected(self) -> bool:
        if self._boom:
            raise RuntimeError("这个 ASGI 实现不支持断连探测")
        return self._disconnected


def _call(ya, url, *, job_id="", request=None):
    """直接 await 端点（不启真实服务），返回 awaitable 的结果。"""
    import asyncio

    async def go():
        return await ya.ytdlp_fetch_endpoint(
            ya.YtdlpFetchReq(url=url, job_id=job_id),
            request if request is not None else FakeRequest(),
        )

    return asyncio.run(go())


def test_endpoint_turns_error_into_400(env, monkeypatch):
    """HTTP 层把 `YtdlpError` 转成 400 + 人话 detail（输入/环境问题不是 500）。"""
    yf, ya = env
    monkeypatch.setattr(yf, "locate", lambda: None)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _call(ya, "https://music.163.com/song?id=1")
    assert ei.value.status_code == 400
    assert yf.ENV_VAR in str(ei.value.detail)


def test_endpoint_returns_preview_url(env, monkeypatch, tmp_path):
    """成功时返回可直接试听的地址 + 站点名 + 时长。"""
    yf, ya = env
    _fake_tool(monkeypatch, yf, tmp_path)

    got = _call(ya, "https://music.163.com/song?id=1")
    assert got["ok"] is True
    assert got["site"] == "网易云音乐"
    assert got["url"].startswith("/media/session/")
    assert got["bytes"] == 2048


def test_endpoint_rejects_malformed_job_id(env):
    """`job_id` 是前端给的，会被当字典键也是取消凭据 —— 必须校验，别照单全收。"""
    yf, ya = env

    from fastapi import HTTPException

    for bad in ("short", "has space", "a/b", "a" * 80, "semi;colon"):
        with pytest.raises(HTTPException) as ei:
            _call(ya, "https://music.163.com/song?id=1", job_id=bad)
        assert ei.value.status_code == 400
        assert "job_id" in str(ei.value.detail)


def test_endpoint_forgets_the_job_when_done(env, monkeypatch, tmp_path):
    """跑完必须把票摘掉 —— 否则长会话里每扒一首就留一张，是慢性泄漏。"""
    yf, ya = env
    _fake_tool(monkeypatch, yf, tmp_path)

    _call(ya, "https://music.163.com/song?id=1", job_id="job-http-0001")
    assert yf.job_of("job-http-0001") is None


def test_progress_endpoint_reports_unknown_job_as_not_found(env):
    """没这个作业不是错误 —— 跑完/取消完再查就是这个结果，前端据此收掉进度条。"""
    yf, ya = env

    import asyncio

    got = asyncio.run(ya.ytdlp_fetch_progress("job-nonexistent"))
    assert got["found"] is False
    assert got["percent"] is None

    live = yf.new_job("job-live-0001")
    live.note(stage="下载中", percent=12.5)
    got = asyncio.run(ya.ytdlp_fetch_progress("job-live-0001"))
    assert got["found"] is True
    assert got["stage"] == "下载中"
    assert got["percent"] == 12.5


def test_cancel_endpoint_flags_the_job(env):
    """取消端点把票打上标记；找不到就如实说 `found: false`（不是错误）。"""
    yf, ya = env

    import asyncio

    job = yf.new_job("job-http-0002")
    got = asyncio.run(ya.ytdlp_fetch_cancel("job-http-0002"))
    assert got == {"ok": True, "found": True}
    assert job.cancelled is True

    got = asyncio.run(ya.ytdlp_fetch_cancel("job-gone-0002"))
    assert got == {"ok": True, "found": False}


def test_client_disconnect_cancels_the_job(env, monkeypatch, tmp_path):
    """★ 前端关页面 = 连接断开 → 后台的 yt-dlp 也得停。

    改之前：用户以为关掉页面就停了，其实 yt-dlp 还在把整首歌拉完 —— 而这一页
    的承诺是"只留本次会话、随用随删"，留一个没人要的下载结果在那里是**背刺**。

    ⚠️ 断言必须落在**报错原文**上，不能只看"杀过进程"：看门狗与墙钟上限也都会
    杀进程树，所以 `killed >= 1` 单独一条抓不住"忘了盯断连"这个突变。
    这里同时把兜底超时压到 3 秒 —— 突变时它是 3 秒内红，而不是干等 600 秒。
    """
    yf, ya = env
    monkeypatch.setattr(yf, "_POLL_S", 0.02)
    monkeypatch.setattr(yf, "_TIMEOUT_S", 3)
    calls = _fake_tool(monkeypatch, yf, tmp_path, produces=None, alive_polls=10_000)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _call(ya, "https://music.163.com/song?id=1", job_id="job-gone-0001",
              request=FakeRequest(disconnected=True))

    assert "已取消" in str(ei.value.detail), f"该是「连接断了所以取消」，实际：{ei.value.detail}"
    assert calls.killed >= 1, "连接断了就要按进程树杀，别让 yt-dlp 继续跑"


def test_disconnect_probe_failure_does_not_break_fetch(env, monkeypatch, tmp_path):
    """断连探测是**尽力而为**：某些 ASGI 实现不支持时，取回不该因此失败。"""
    yf, ya = env
    _fake_tool(monkeypatch, yf, tmp_path)

    got = _call(ya, "https://music.163.com/song?id=1", job_id="job-boom-0001",
                request=FakeRequest(boom=True))
    assert got["ok"] is True
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


def test_stem_is_unique_even_within_the_same_second(env, monkeypatch):
    """★★ `stem` 不只在文件名里 —— 它同时是**探针的过滤键**与**收尾的删除键**。

    用秒级时间戳（`int(time.time())`）时，同一秒内的两次取回共用前缀：
      · 各自的 `_probe_audio_ext` 会看到对方的产物（多个时取最大的）→
        用户拿到的是**另一条链接**的音频，而它是个合法音频，不报错；
      · 先跑完的那次 `_cleanup` 会按前缀删掉后跑那次正在写的文件。
    连点两下就在同一秒内，所以这不是理论风险。

    这里把 `time.time` 钉死成同一个值，模拟"同一秒"的极端现场。

    ⚠️ 这条用例的**主护栏是单调顺延**（`ms <= _LAST_STEM_MS` 就 +1），
    毫秒精度只是让顺延几乎不触发。所以单把"毫秒"退回秒级、但保留顺延，
    它**不会红** —— 这是对的（顺延独立成立）。要验这段就整体退回原始实现，
    见下方 `test_stem_survives_a_clock_going_backwards` 与 commit 说明里的突变记录。
    """
    yf, _ = env
    monkeypatch.setattr(yf.time, "time", lambda: 1_700_000_000.0)  # 秒级完全相同
    got = {yf._new_stem() for _ in range(50)}
    assert len(got) == 50, f"同秒内前缀必须唯一，实际只得到 {len(got)} 个不同的"


def test_stem_survives_a_clock_going_backwards(env, monkeypatch):
    """时钟被校回（或 NTP 回拨）时前缀仍要单调，不能撞上刚用过的那个。"""
    yf, _ = env
    ticks = iter([2_000_000_000.5, 2_000_000_000.5, 1_999_999_999.0, 1_999_999_999.0])
    monkeypatch.setattr(yf.time, "time", lambda: next(ticks))
    got = [yf._new_stem() for _ in range(4)]
    assert len(set(got)) == 4, f"回拨后撞名：{got}"


def test_no_audio_message_lists_the_real_causes(env, monkeypatch, tmp_path):
    """报错要给出"下一步三种可能"，不是一句"拉取失败"。"""
    yf, _ = env
    _fake_tool(monkeypatch, yf, tmp_path, produces=None)
    with pytest.raises(yf.YtdlpError) as ei:
        yf.fetch(QQ_CANON)
    msg = str(ei.value)
    assert "登录态" in msg and "yt-dlp -U" in msg


# ------------------------------------------------- 会员曲兜底：MV 抽音轨
#
# 背景与实测证据见 `mv_audio_fallback` 的模块 docstring。这里只钉住三件事：
#   ① 默认必须是关的（它是"绕会员授权"的路径，不该被顺手打开）
#   ② 只对 QQ 音乐生效（别家没有等价通路）
#   ③ 兜底只在 yt-dlp 失败之后跑，且**失败不能盖掉 yt-dlp 的原话**
#      （用户要的归因是"为什么没成"，不是"兜底也失败了"）


@pytest.fixture
def mvenv(monkeypatch, tmp_path):
    """MV 兜底模块，依赖全部隔离：不启浏览器、不跑 ffmpeg、不发网络。

    ★ **不要**替换 `enabled()` 本身 —— 它是被测对象之一（默认必须是关的），
    打桩会把这个性质一起测没。只隔离它依赖的外部东西。
    """
    import mv_audio_fallback as m

    monkeypatch.delenv(m.ENV_ENABLE, raising=False)
    monkeypatch.delenv(m.ENV_PW_CORE, raising=False)
    monkeypatch.delenv(m.ENV_NODE, raising=False)
    return m


def test_mv_fallback_is_off_by_default(mvenv, monkeypatch):
    """★ 默认关。这条是**性质**不是实现细节 —— 它决定"用户装完插件后
    会不会在不知情的情况下走绕授权的路径"。"""
    monkeypatch.delenv(mvenv.ENV_ENABLE, raising=False)
    assert mvenv.enabled() is False
    with pytest.raises(mvenv.MvFallbackError, match="没开启"):
        mvenv.fetch_from_mv("0023jgxa0Ym5yo")


@pytest.mark.parametrize("val", ["1", "true", "YES", "On"])
def test_mv_fallback_accepts_common_truthy_values(mvenv, monkeypatch, val):
    monkeypatch.setenv(mvenv.ENV_ENABLE, val)
    assert mvenv.enabled() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "maybe"])
def test_mv_fallback_rejects_other_values(mvenv, monkeypatch, val):
    """含糊的值一律当**关** —— 这个开关不该有"以为开了其实没开"的中间态。"""
    monkeypatch.setenv(mvenv.ENV_ENABLE, val)
    assert mvenv.enabled() is False


def test_mv_status_reports_the_env_var_name(mvenv, monkeypatch):
    """`status` 要能被前端直接展示 —— 关闭时得告诉用户"怎么开"。"""
    monkeypatch.delenv(mvenv.ENV_ENABLE, raising=False)
    st = mvenv.status()
    assert st["enabled"] is False
    assert mvenv.ENV_ENABLE in st["detail"]


def test_only_qq_urls_trigger_the_fallback(env, monkeypatch):
    """★ 只认 `y.qq.com` 域名族。别家没有等价的免费 MV 通路，
    误触发等于对一个不支持的站点发一堆网络请求。"""
    _, ya = env

    assert ya._qq_songmid_of("https://y.qq.com/n/ryqq/songDetail/0023jgxa0Ym5yo") == "0023jgxa0Ym5yo"
    assert ya._qq_songmid_of("https://c6.y.qq.com/base/fcgi-bin/u?__=x") is None  # 短链要解跳（本测试禁网）
    for other in (
        "https://music.163.com/song?id=1",
        "https://www.bilibili.com/video/av1",
        "https://music.migu.cn/v3/music/song/1",
        "https://y.qq.com.evil.example/n/ryqq/songDetail/0023jgxa0Ym5yo",
    ):
        assert ya._qq_songmid_of(other) is None, other


def test_fallback_does_not_mask_the_original_error(env, monkeypatch, tmp_path):
    """★ 兜底没开时，用户必须看到 yt-dlp 的原话 —— 不是"兜底失败"。

    yt-dlp 的归因（要登录 / 版权 / 升级）比我们的兜底信息有用得多。
    """
    yf, ya = env
    _fake_tool(monkeypatch, yf, tmp_path, rc=1, produces=None,
               stderr="ERROR: This song is only available for registered users")

    monkeypatch.delenv("VM_MV_FALLBACK", raising=False)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _call(ya, QQ_CANON)
    assert ei.value.status_code == 400
    assert "登录态" in str(ei.value.detail)


def test_fallback_success_is_marked_with_quality_note(env, monkeypatch, tmp_path):
    """★ 兜底成功时必须带 `quality_note` 和 `via=mv_fallback`。

    不能让 MV 抽轨的歌**看起来**和正版音源一样 —— 用户有权知道他拿到的是
    192kbps 的混音轨，而不是母带。
    """
    yf, ya = env
    import mv_audio_fallback as m

    _fake_tool(monkeypatch, yf, tmp_path, rc=1, produces=None,
               stderr="ERROR: This song is only available for registered users")
    monkeypatch.setenv("VM_MV_FALLBACK", "1")
    monkeypatch.setattr(
        m, "fetch_from_mv",
        lambda songmid, timeout_s=None: {
            "name": "ytdlp_mv_1.m4a", "path": "/tmp/x.m4a",
            "url": "/media/session/ytdlp_mv_1.m4a", "bytes": 6561304,
            "site": "QQ音乐（MV 抽轨）",
            "source_url": "https://y.qq.com/n/ryqq/mv/r0035thc5pb",
            "quality_note": "音质：MV 抽轨",
        },
    )
    got = _call(ya, QQ_CANON)
    assert got["ok"] is True
    assert got["via"] == "mv_fallback"
    assert "MV 抽轨" in got["quality_note"]


def test_fallback_failure_still_reports_the_original_error(env, monkeypatch, tmp_path):
    """兜底自己失败（没关联 MV / 抓不全）时，仍然抛 yt-dlp 的原话。

    这是刻意的：兜底是"多做一次尝试"，它的失败原因对用户没有指导价值 ——
    用户能行动的是"配登录态 / 换来源 / 升 yt-dlp"，那些都在 yt-dlp 的话里。
    """
    yf, ya = env
    import mv_audio_fallback as m

    _fake_tool(monkeypatch, yf, tmp_path, rc=1, produces=None,
               stderr="ERROR: This song is only available for registered users")
    monkeypatch.setenv("VM_MV_FALLBACK", "1")

    def boom(songmid, timeout_s=None):
        raise m.MvFallbackError("这首歌没有关联的官方 MV")

    monkeypatch.setattr(m, "fetch_from_mv", boom)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _call(ya, QQ_CANON)
    assert ei.value.status_code == 400
    assert "登录态" in str(ei.value.detail), "必须是 yt-dlp 的原话"
    assert "没有关联" not in str(ei.value.detail), "兜底的失败原因不该外泄给用户"


def test_fallback_is_not_tried_when_ytdlp_succeeds(env, monkeypatch, tmp_path):
    """★ 兜底是**兜底**。主路成功时不能去碰它 —— 否则每首歌都白抓一次 MV。"""
    yf, ya = env
    import mv_audio_fallback as m

    _fake_tool(monkeypatch, yf, tmp_path)
    monkeypatch.setenv("VM_MV_FALLBACK", "1")

    called = []
    monkeypatch.setattr(m, "fetch_from_mv", lambda s, timeout_s=None: called.append(s))

    got = _call(ya, "https://music.163.com/song?id=1")
    assert got["via"] == "ytdlp"
    assert called == [], "主路成功时不该触发兜底"
    assert "quality_note" not in got, "正版音源不该被标注成打折音质"


def test_fallback_unexpected_exception_does_not_escape(env, monkeypatch, tmp_path):
    """兜底里出任何意外都不该让请求变成 500 —— 它只是"多做一次尝试"。"""
    yf, ya = env
    import mv_audio_fallback as m

    _fake_tool(monkeypatch, yf, tmp_path, rc=1, produces=None,
               stderr="ERROR: nope")
    monkeypatch.setenv("VM_MV_FALLBACK", "1")

    def boom(songmid, timeout_s=None):
        raise RuntimeError("兜底代码自己有 bug")

    monkeypatch.setattr(m, "fetch_from_mv", boom)

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        _call(ya, QQ_CANON)
    assert ei.value.status_code == 400, "不能变成 500"


# ------------------------------------------------ MV 抓取的完整性把关


def test_incomplete_grab_is_rejected(monkeypatch, tmp_path):
    """★ 抓不全必须失败，不能把截断的歌给用户。

    分片是惰性加载的：只播开头就只拿到开头几片。给一份被截断的音频，
    比明确失败糟得多 —— 用户可能拿去跑完翻唱才发现少了尾部。
    """
    import mv_audio_fallback as m

    monkeypatch.setattr(m, "_node_exe", lambda: "node")
    monkeypatch.setattr(m, "_playwright_core", lambda: tmp_path)

    class P:
        returncode = 0
        stdout = '{"ok":true,"complete":false,"segments":5,"want":27,"bytes":1,"ts":"x"}'
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: P())
    with pytest.raises(m.MvFallbackError, match="没抓全"):
        m._grab_mv("r0035thc5pb", tmp_path)


def test_complete_grab_passes(monkeypatch, tmp_path):
    """抓全了要放行 —— 别把正常路径也拦了。"""
    import mv_audio_fallback as m

    monkeypatch.setattr(m, "_node_exe", lambda: "node")
    monkeypatch.setattr(m, "_playwright_core", lambda: tmp_path)

    class P:
        returncode = 0
        stdout = '{"ok":true,"complete":true,"segments":27,"want":27,"bytes":6561304,"ts":"x"}'
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: P())
    got = m._grab_mv("r0035thc5pb", tmp_path)
    assert got["segments"] == 27


def test_no_segments_is_a_clear_error(monkeypatch, tmp_path):
    """打开了页面但一片都没抓到（下架 / 地区受限）—— 要说清是哪种情况。"""
    import mv_audio_fallback as m

    monkeypatch.setattr(m, "_node_exe", lambda: "node")
    monkeypatch.setattr(m, "_playwright_core", lambda: tmp_path)

    class P:
        returncode = 0
        stdout = '{"ok":false,"err":"no-segments"}'
        stderr = ""

    monkeypatch.setattr(m.subprocess, "run", lambda *a, **k: P())
    with pytest.raises(m.MvFallbackError, match="下架|地区受限|改版"):
        m._grab_mv("r0035thc5pb", tmp_path)


def test_find_vid_extracts_from_nested_json(monkeypatch):
    """vid 在 `song_detail` 响应里的嵌套层级不固定，所以是扫 JSON 文本抠的 ——
    这条钉住"换一层嵌套也还认得"。"""
    import mv_audio_fallback as m

    monkeypatch.setattr(
        m, "_musicu",
        lambda *a, **k: {"data": {"track_info": {"mv": {"vid": "r0035thc5pb", "id": 1}}}},
    )
    assert m.find_vid("0023jgxa0Ym5yo") == "r0035thc5pb"


def test_find_vid_returns_none_when_no_mv(monkeypatch):
    """没有关联 MV 的曲子要能识别出来（不是报错，是"这条路不通"）。"""
    import mv_audio_fallback as m

    monkeypatch.setattr(m, "_musicu", lambda *a, **k: {"data": {"track_info": {}}})
    assert m.find_vid("0023jgxa0Ym5yo") is None


def test_find_vid_rejects_malformed_songmid(mvenv):
    """songmid 会被拼进请求 —— 形态不对就直接返回 None，不拼。"""
    import mv_audio_fallback as m

    for bad in ("", "../../etc", "a b", "x" * 100, "a/b"):
        assert m.find_vid(bad) is None, bad

