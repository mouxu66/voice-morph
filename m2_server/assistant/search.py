"""按关键词搜歌 —— 只用 B 站，因为只有它的链接一定能被下载链路接住。

为什么不用 YouTube 搜索
-----------------------
`ytdlp_fetch._check_supported` 的白名单里**没有** youtube.com（这是有意的：
那条链路的定位是"国内的平台页面链接"）。如果这里用 `ytsearch:`，就会出现
"搜到了、点了确认、下载时被拒"这种最难受的失败 —— 用户在确认框里点的那一下
等于白点。搜索源必须是下载白名单的子集。

`bilibili.com` 在 `_SUPPORTED_FAMILIES` 里，所以 `bilisearch` 的结果一定能进
`ytdlp_fetch.fetch`。这条约束写在测试里（test_assistant_search.py）。

为什么不用 flat-playlist（快 5 倍）
-----------------------------------
实测 B 站的 flat 结果里 `title` / `duration` 全是 None，只剩一个 av 号。
没有标题就没法让用户挑，也没法打分排序 —— 那就退化成"随便给你一首"。
所以宁可多花几秒逐个解析（2 条约 5s，5 条约 12s）。
"""

from __future__ import annotations

import re
import subprocess
import time

try:
    import ytdlp_fetch
except ImportError:  # pragma: no cover
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import ytdlp_fetch

#: 搜索结果条数。5 条够挑，再多解析时间翻倍且用户也不会往下看。
DEFAULT_LIMIT = 5
#: 整体超时。B 站反爬时会卡住，必须有上限。
TIMEOUT_S = 90

#: 打分用的词表。权重是"人挑歌时会怎么想"的直译，不是调参调出来的。
_GOOD = {
    "无损": 8,
    "Hi-Res": 8,
    "hires": 6,
    "高音质": 6,
    "官方": 6,
    "原版": 5,
    "完整版": 4,
    "MV": 3,
}
_BAD = {
    "翻唱": -9,
    "cover": -8,
    "伴奏": -12,
    "纯音乐": -10,
    "鬼畜": -14,
    "DJ": -8,
    "remix": -6,
    "教学": -14,
    "吉他": -8,
    "钢琴": -6,
    "教程": -14,
    "reaction": -12,
    "直播": -10,
    "切片": -8,
}


class SearchError(RuntimeError):
    """搜不了（yt-dlp 缺失 / 超时 / 全网失败）。消息直接给人看。"""


def _exe() -> str:
    exe = ytdlp_fetch.locate()
    if exe is None:
        raise SearchError(
            "没找到 yt-dlp，搜不了歌。装好后放进 PATH，"
            f"或用环境变量 {ytdlp_fetch.ENV_VAR} 指向它。"
        )
    return str(exe)


def _parse_dur(raw: str) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def score(title: str, duration: float | None, query: str) -> float:
    """给一条候选打分。分数只用于**排序**，不用于"自动选一条"。

    自动选（`pick_best`）也只在用户明确说"就这个"时才用，见 executor 的注释。
    """
    s = 0.0
    low = title.lower()
    # 查询里的每个词出现在标题里都算命中（"周杰伦 晴天" 命中两个比一个强）
    for tok in re.split(r"[\s,，、]+", query.strip()):
        if tok and tok.lower() in low:
            s += 10
    for word, w in _GOOD.items():
        if word.lower() in low:
            s += w
    for word, w in _BAD.items():
        if word.lower() in low:
            s += w
    # 歌曲的合理长度：1 分钟到 8 分钟
    if duration is not None:
        if 60 <= duration <= 480:
            s += 6
        elif duration < 30:
            s -= 12
        elif duration > 900:
            s -= 10
    return s


#: 搜索结果缓存。"换一首"来回点的时候不该每次都去撞 B 站的风控。
_CACHE: dict[str, tuple[float, list[dict]]] = {}
CACHE_TTL_S = 600


def search_song(query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """搜 `query` 返回候选列表（已按分数降序）。

    每条：`{title, duration, url, uploader, score}`。`duration` 未知时为 None ——
    不要填 0，那会让"时长未知"和"零秒音频"混在一起。
    """
    q = (query or "").strip()
    if not q:
        raise SearchError("请先说清要搜什么")
    ck = f"{q}|{limit}"
    hit = _CACHE.get(ck)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return list(hit[1])
    n = max(1, min(int(limit), 10))
    cmd = [
        _exe(),
        "--skip-download",
        "--no-warnings",
        "--ignore-errors",
        "--print",
        "%(title)s\t%(duration)s\t%(webpage_url)s\t%(uploader)s",
        f"bilisearch{n}:{q}",
    ]
    # B 站对连续搜索有风控（HTTP 412）。实测"刚搜完一条马上再搜"最容易触发，
    # 等几秒就好 —— 所以这里退一次避再重试，并把 412 翻译成人话。
    # 只重试一次：再多就是拿风控当运气赌，反而把 IP 关得更久。
    last_err = ""
    for attempt in range(2):
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                timeout=TIMEOUT_S,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SearchError(
                f"搜「{q}」超时（{TIMEOUT_S}s）。B 站这会儿可能限流，过一会儿再试。"
            ) from exc
        except OSError as exc:
            raise SearchError(f"yt-dlp 起不来：{exc}") from exc
        last_err = (out.stderr or "").strip()
        if "412" in last_err and attempt == 0:
            time.sleep(8.0)
            continue
        break

    rows: list[dict] = []
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        title, dur, url = parts[0].strip(), parts[1].strip(), parts[2].strip()
        uploader = parts[3].strip() if len(parts) > 3 else ""
        if not title or not url.startswith("http"):
            continue
        # 只收白名单内的链接：宁可少一条，也不要给出点下去会失败的候选
        try:
            ytdlp_fetch._check_supported(url)
        except Exception:  # noqa: BLE001 —— 白名单拒绝也算"这条不能用"
            continue
        d = _parse_dur(dur)
        rows.append(
            {
                "title": title,
                "duration": d,
                "url": url,
                "uploader": uploader,
                "score": score(title, d, q),
            }
        )

    if not rows:
        err = (out.stderr or "").strip().splitlines()
        tail = err[-1] if err else "没有任何结果"
        if "412" in last_err:
            raise SearchError("B 站限流了（412），等半分钟再搜，或者直接把歌的链接粘进来。")
        raise SearchError(f"搜「{q}」没搜到能用的结果（{tail}）")

    rows.sort(key=lambda r: r["score"], reverse=True)
    _CACHE[ck] = (time.time(), rows)
    return rows


def pick_best(rows: list[dict]) -> dict:
    """取分数最高的一条。调用方必须已经把这条显示给用户看过。"""
    if not rows:
        raise SearchError("没有候选可选")
    return rows[0]
