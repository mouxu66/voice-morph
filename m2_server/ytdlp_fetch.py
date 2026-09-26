"""把一条**平台链接**交给外部的 yt-dlp 取回音频（「在线扒歌」插件用）。

这个模块做什么 / 不做什么
------------------------
**做**：找到用户自备的 yt-dlp，用它把网易云 / QQ音乐 / B站 等站点的音频拉进会话目录，
返回可试听的相对地址与时长，让上游走和「粘直链」完全一样的翻唱链路。

**不做**：不内置 yt-dlp、不代管它的更新、不解析任何平台接口。
它是**外部工具桥** —— 用户自己装、自己用、自己 `yt-dlp -U` 升级。

为什么是"桥"而不是"内置解析"
----------------------------
曾评估过三类方案（2026-09-26 调研结论）：

    · 第三方 PHP 解析接口（各种 `music.xxx.php?msg=`）—— 无维护者、平均几个月失效，
      且它绕的就是平台 DRM，写进源码里是开源/比赛的硬伤；
    · 社区 NeteaseCloudMusicApi 及其 fork —— 原版作者已**主动删库停更**
      （README 只剩"保护版权，此仓库不再维护"），活着的 fork 也随时可能步后尘；
    · **yt-dlp** —— 官方维护、更新极勤（2026-08 仍在发版），`netease:*` 与 `qqmusic:*`
      是一等公民 extractor，且**平台改版由整个社区一起修**。

选第三条，但仍然**不把它的逻辑抄进来**：它就是立可执行文件，调用即可。
这样既拿到了"活人维护"的好处，又让本仓库里**没有一行绕 DRM 的代码** ——
要为比赛/开源脱敏时，这块天然是干净的（用户自己装工具、自己承担使用边界）。

护栏为什么不能省
----------------
与 `url_fetch` 不同，这里**不自己发 HTTP**，所以拦不住 yt-dlp 内部的网络行为。
能守的两条边界必须守住：

    ① **站点白名单**（`_SUPPORTED_HOSTS`）。没有它，这个端点就是一个"拿 url 参数
       让服务器跑任意 yt-dlp"的万能入口 —— 最差情况变成内网探测/任意下载的工具。
       yt-dlp 自己支持上千个站点，这里只放行「歌词场景真的会用到」的那些。
    ② **产物必须落会话目录**，且只收 `.opus` 之外能喂给 demucs 的音频格式。
       "随用随删"是这个功能的核心诉求（用户的语音场景不可能存一堆歌），
       所以路径由 yt-dlp 的 `-P`/`-o` 钉死，不接受它自己挑地方。

失败一律抛 `YtdlpError`，消息是给用户看的人话（尤其"没装 yt-dlp"这一条 ——
静默失败会让人以为是歌的问题）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import session_out

#: 找 yt-dlp 的顺序：环境变量 → PATH → 与项目同目录的常见位置。
#: 不写死盘符（换机器就废），不假设 Python 环境里有 `yt_dlp` 包 ——
#: 两种形态（独立 exe / pip 装的模块）都接受。
ENV_VAR = "VM_YTDLP"

#: 只放行这些站点。**故意收得很紧**：yt-dlp 支持上千站，全放行等于开了个任意下载口。
#: 加站点前先问一句"歌词场景真的会用它取歌吗"。
_SUPPORTED_HOSTS: dict[str, str] = {
    # 华语音乐主战场
    "music.163.com": "网易云音乐",
    "y.qq.com": "QQ音乐",
    "i.y.qq.com": "QQ音乐",
    "c.y.qq.com": "QQ音乐",
    "music.migu.cn": "咪咕音乐",
    # B站：官方另有 BilibiliAudio extractor，且大量歌曲只有 B 站有官方音源
    "www.bilibili.com": "哔哩哔哩",
    "b23.tv": "哔哩哔哩（短链）",
    "m.bilibili.com": "哔哩哔哩",
    # 播客/长音频（同样是"想换音色"的常见素材）
    "www.ximalaya.com": "喜马拉雅",
    # 合法自由音源（无版权风险，但中文歌少 —— 放着以备方案 A 不可用时兜底）
    "freemusicarchive.org": "Free Music Archive",
    "www.jamendo.com": "Jamendo",
}

#: 拉取超时。整首歌（含音视频分离后的音频流）通常几秒到几十秒；
#: 600s 是给"网络很慢的大文件"留的余量，不是给"挂着不动"的。
_TIMEOUT_S = 600

#: `--audio-format` 转出来的容器。mp3 最通用，demucs / ffmpeg 都直接吃。
_AUDIO_FORMAT = "mp3"

#: yt-dlp 自己的网络行为没法从 Python 侧逐跳校验，但可以让它别把探测结果写到会话外。
_DENY_EXTRA = ("--no-playlist", "--no-warnings", "--no-part", "--newline")


class YtdlpError(RuntimeError):
    """给用户看的错误（消息即文案）。"""


def _candidates() -> list[Path]:
    """列出可能放 yt-dlp 的路径，按可信度排序。"""
    out: list[Path] = []
    env = os.environ.get(ENV_VAR)
    if env:
        out.append(Path(env))
    # PATH（含 .exe / 无扩展名两种写法，Windows 上都有）
    for name in ("yt-dlp.exe", "yt-dlp"):
        found = shutil.which(name)
        if found:
            out.append(Path(found))
    # 与项目同级的常见落点（用户"丢进任意文件夹"的典型位置）
    root = Path(__file__).resolve().parents[2]
    out.extend([root / "tools" / "yt-dlp.exe", root / "yt-dlp.exe"])
    return out


def locate() -> Path | None:
    """找 yt-dlp。找不到返回 `None`（调用方负责给"怎么装"的话）。"""
    for path in _candidates():
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def _host_of(url: str) -> str:
    parsed = urllib.parse.urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower()


def _check_supported(url: str) -> str:
    """站点必须属于白名单，且必须是 http(s)。返回站点显示名。"""
    if not isinstance(url, str) or not url.strip():
        raise YtdlpError("请先粘一条歌曲链接")
    raw = url.strip()
    scheme = urllib.parse.urlparse(raw if "://" in raw else f"https://{raw}").scheme
    if scheme not in ("http", "https"):
        raise YtdlpError("只支持 http/https 链接")
    host = _host_of(raw)
    if not host:
        raise YtdlpError("这条链接看不出站点，请粘完整的分享链接")
    if host not in _SUPPORTED_HOSTS:
        names = "、".join(sorted(set(_SUPPORTED_HOSTS.values())))
        raise YtdlpError(f"暂不支持 {host}。目前只放了：{names}。其它来源请用「翻唱」页的上传或粘直链。")
    return _SUPPORTED_HOSTS[host]


def _site_list() -> list[dict]:
    """给前端展示的"支持哪些站点"（按站点名去重，取第一个示例域名）。

    注意元组顺序：`_SUPPORTED_HOSTS` 是 `{host: name}`，去重后要输出
    `{name: host}` —— 搞反了前端会显示"music.163.com：网易云音乐"这种倒装。
    """
    seen: dict[str, str] = {}
    for host, name in _SUPPORTED_HOSTS.items():
        seen.setdefault(name, host)
    return [{"name": name, "example": host} for name, host in sorted(seen.items())]


def probe() -> dict:
    """只读状态：yt-dlp 在不在、什么版本、支持哪些站点。**不跑网络。**"""
    path = locate()
    if path is None:
        return {
            "available": False,
            "path": "",
            "version": "",
            "sites": _site_list(),
            "hint": (
                f"没找到 yt-dlp。装好之后用环境变量 {ENV_VAR} 指定它的完整路径"
                "（或直接放进 PATH），然后重启后端。"
            ),
        }
    version = ""
    try:
        # `--version` 是纯本地输出，不发网络请求 —— 拿它做探活是安全的
        r = subprocess.run(  # noqa: S603 —— 路径来自白名单式的本地探测，非用户输入
            [str(path), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        version = (r.stdout or "").strip().splitlines()[0] if r.stdout else ""
    except (OSError, subprocess.SubprocessError):
        version = ""
    return {
        "available": True,
        "path": str(path),
        "version": version,
        "sites": _site_list(),
        "hint": "",
    }


def _probe_audio_ext(path: Path) -> str | None:
    """在会话目录里找出 yt-dlp 刚产出的音频文件。多个时取最大的（合并产物）。"""
    if not path.exists():
        return None
    cands = [p for p in path.iterdir() if p.is_file() and p.suffix.lower() in (".mp3", ".m4a", ".wav", ".flac", ".opus", ".webm")]
    if not cands:
        return None
    return str(max(cands, key=lambda p: p.stat().st_size))


def fetch(url: str, timeout_s: int = _TIMEOUT_S) -> dict:
    """把 `url` 对应的音频拉进会话目录，返回与 `url_fetch.fetch_to_session` **同形**的结果。

    同形很重要：上游（`cover_api`）拿它当"这首歌从哪来"的一种答案，
    字段一致就不用改链路。

    为什么不走 `url_fetch`：那个是"自己发 HTTP 的直链下载器"，面对的是已经能直接
    下载的 URL；这里面对的是**平台页面链接**，取回流地址要跑平台的页面逻辑 ——
    那正是 yt-dlp 干的活。两者的护栏口径不同（那边是逐跳校验 IP），不能混。
    """
    site = _check_supported(url)

    exe = locate()
    if exe is None:
        raise YtdlpError(
            f"没找到 yt-dlp，拿不到这条链接的音频。装好后用环境变量 {ENV_VAR} 指向它，"
            "或直接放进 PATH，然后重启后端。"
        )

    # 产物路径由 `-P`/`-o` 钉死，不接受 yt-dlp 自己挑地方（"随用随删"的前提）。
    out_dir = session_out.session_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    stem = f"ytdlp_{stamp}"

    cmd = [
        str(exe),
        # 只取音频，转成 mp3；不下载视频流（省带宽，也避免留下用不上的文件）
        "-x",
        "--audio-format", _AUDIO_FORMAT,
        "--audio-quality", "0",
        # ★ 输出落会话目录，文件名固定前缀 —— 复现「会话目录 + 退出即删」口径
        "-P", str(out_dir),
        "-o", f"{stem}.%(ext)s",
        # 单曲语义：粘贴的如果是歌单/合集链接，只取第一条，别把整个歌单拉下来
        *_DENY_EXTRA,
        "--",
        url,
    ]

    try:
        r = subprocess.run(  # noqa: S603 —— exe 来自本地探测；url 已过站点白名单
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        _cleanup(out_dir, stem)
        raise YtdlpError(f"yt-dlp 超过 {timeout_s} 秒没拉完，已中止。网络慢或这首歌太大。") from exc
    except OSError as exc:
        raise YtdlpError(f"启动 yt-dlp 失败：{exc}") from exc

    if r.returncode != 0:
        _cleanup(out_dir, stem)
        raise YtdlpError(_explain(r, site))

    got = _probe_audio_ext(out_dir)
    if got is None:
        raise YtdlpError(
            "yt-dlp 跑完了但会话目录里没有音频文件。可能是这首歌要登录才能听"
            "（试试带 cookie 跑，见文档），或者平台改了取流规则（升级 yt-dlp）。"
        )

    src = Path(got)
    # 产物必须真的在会话目录里 —— 别信"它说写在那儿"，实测过 md5sum 那类假绿。
    try:
        src.resolve().relative_to(out_dir.resolve())
    except ValueError as exc:
        raise YtdlpError(f"yt-dlp 把文件写到了会话目录之外（{src}），已拒绝使用") from exc

    size = src.stat().st_size
    if size <= 0:
        _cleanup(out_dir, stem)
        raise YtdlpError("拉回来的文件是空的，可能是这首歌有版权限制（付费曲常见）。")

    return {
        "name": src.name,
        "path": str(src),
        "url": f"/media/session/{src.name}",
        "suffix": src.suffix.lower(),
        "bytes": size,
        "content_type": "audio/mpeg" if src.suffix.lower() == ".mp3" else "audio/*",
        "site": site,
    }


def _cleanup(out_dir: Path, stem: str) -> None:
    """失败时收尾：删掉这次留下的半截文件（含 yt-dlp 的中间产物）。

    必须按 `stem` 前缀删，**不能清空整个会话目录** —— 那里面有用户刚下好、
    还没跑翻唱的那首歌。这个函数被调用时是"这次失败了"，不是"清理场地"。
    """
    try:
        for p in out_dir.glob(f"{stem}*"):
            if p.is_file():
                p.unlink(missing_ok=True)
    except OSError:
        pass  # 收尾失败不该盖掉真正的错误信息


def _explain(r: subprocess.CompletedProcess, site: str) -> str:
    """把 yt-dlp 的 stderr 翻译成人话。

    直接甩原始 stderr 给用户是没用的（那是几百行下载日志）。按最常见的几类
    归因，剩下的才回落到最后一行。
    """
    err = (r.stderr or "").strip()
    low = err.lower()
    if "sign in" in low or "login" in low or "cookie" in low:
        return f"{site} 这首歌要登录才能拿。用 `--cookies-from-browser` 带浏览器的登录态再试。"
    if "unsupported url" in low or "no suitable extractor" in low:
        return (
            f"yt-dlp 认不出这条 {site} 链接。多半是它的版本旧了 —— "
            "跑一次 `yt-dlp -U` 升级再试。"
        )
    if "copyright" in low or "not available" in low or "vip" in low:
        return f"{site} 这首有版权/付费限制，拿不到音频流。换一首或换来源。"
    if "403" in err or "forbidden" in low:
        return f"{site} 拒绝了请求（403）。升级 yt-dlp 或用浏览器 cookie 再试。"
    tail = err.splitlines()[-1] if err else f"退出码 {r.returncode}"
    return f"yt-dlp 失败：{tail[:300]}"


def _main(argv: list[str]) -> int:
    """命令行入口（排查用）：`python m2_server/ytdlp_fetch.py probe|fetch <url>`。"""
    if not argv or argv[0] == "probe":
        print(json.dumps(probe(), ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "fetch" and len(argv) > 1:
        try:
            print(json.dumps(fetch(argv[1]), ensure_ascii=False, indent=2))
        except YtdlpError as e:
            print(f"错误：{e}")
            return 1
        return 0
    print("用法：ytdlp_fetch.py [probe | fetch <url>]")
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
