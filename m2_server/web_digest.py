"""群聊里「只有外部知识才答得上」的那部分：搜一下，摆给用户看。

## 为什么要单独一层

2026-10-25 在 3864 群 40 个真实回话时机上实测：模型答不好的根子不是「不会说话」，
是**记录里根本没有那条信息**。同一份提示词，问「码发给我」它读反方向、问游戏数值它
编数字 —— 这两件事都不是提示词能修的。凡是「用户自己没说、模型也没见过」的东西，
9B 只会编。

所以这里不做「让模型搜」，只做「替用户搜，然后把查到的东西摊在面板上」：
用户看一眼标题摘要就知道该说什么，再决定要不要让模型按这个写一句。
**模型不参与检索、不参与筛选** —— 它一旦参与，就会把搜到的标题改写成一句听起来
很确定的话，那比不搜更糟。

## 为什么是搜狗，不是必应

同一天实测（都是裸 HTTP、无 Cookie、真实浏览器 UA）：

- `cn.bing.com/search?q=...&format=rss`：**静默返回完全无关的结果**。搜
  「无尽冬日 英雄 推荐」拿到 10 条里的前 4 条是 DeepSeek 官网、汽水音乐、知乎音乐
  问题 —— 0 条相关，而 HTTP 200、XML 结构完整、`<title>` 里还写着正确的查询词。
  这比搜不到危险得多：它会让人以为查到了。
- `www.bing.com/search`（HTML）：返回抖店广告页，同样 0 条相关。
- `html.duckduckgo.com`：本机直连超时（代理下也可能通，但不能作为默认路径）。
- `www.so.com`：超时。
- `www.sogou.com/web?query=`：**380KB HTML、9 条结果全部命中「无尽冬日」**。

所以默认后端是搜狗。别因为「必应有 API」就换回去 —— 那不是 API，是降级页。

## 相关性必须自己校验

上面那件事的教训：HTTP 200 不等于查到了。`search()` 会算一个 `hits`（query 的每一段
在标题+摘要里出现了几个），`weak=True` 表示「结果跟问题基本没关系」。
前端要据此弱化显示，绝不能把 weak 的结果当成答案摆出来。
"""

from __future__ import annotations

import html as html_mod
import re
import time
import urllib.parse

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
SOGOU = "https://www.sogou.com/web?query="
#: 搜索只值一次「顺手查」的等待：面板是浮层，超过 8 秒用户早就自己打了。
TIMEOUT = 8.0
#: 结果少于 2 条基本等于没查到（搜狗对长句会直接返回「没有找到」）。
MIN_ITEMS = 1

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_H3_RE = re.compile(r'<h3[^>]*class="[^"]*\bvr-title\b[^"]*"[^>]*>(.*?)</h3>', re.S)
_SUM_RE = re.compile(
    r'<div[^>]*id="cacheresult_summary_[^"]*"[^>]*>(.*?)</div>', re.S
)
#: 只有一部分结果带 `cacheresult_summary_*` 这个 id（2026-10-25 实测 9 条里 2 条），
#: 其余摘要挂在 `class="fz-mid space-txt ..."` 上。只认 id 的话 9 条会被砍成 2 条，
#: 而砍掉的那 7 条恰好是「12 代英雄讲解」「强度排行」这种更具体的。
_SUM_ALT_RE = re.compile(
    r'<div[^>]*class="[^"]*\bspace-txt\b[^"]*"[^>]*>(.*?)</div>', re.S
)
_URL_RE = re.compile(r'data-url="(https?://[^"]+)"')
_SPAN_RE = re.compile(r"<span>([^<]{1,40})</span>")

#: 搜狗自己的壳、导航、广告位标题 —— 出现在 vr-title 里也不能当结果。
_NOISE = ("搜狗", "相关搜索", "大家还在搜", "查看更多", "广告")


def _text(s: str) -> str:
    """去标签 + 反转义 + 压空白。标题里 `<em>` 是高亮，必须留下文字。"""
    return _WS_RE.sub(" ", html_mod.unescape(_TAG_RE.sub("", s or ""))).strip()


def _tokens(query: str) -> list[str]:
    """把查询切成「段」，用来做相关性判定。

    只按空格/标点切，不做中文分词 —— 这里要的是一眼可解释的判断
    （「查的每个词有没有出现在标题里」），不是真的语义匹配。
    单字段（长度 1）丢掉：一个字在哪儿都能撞上，命中率会被它灌水。
    """
    out = []
    for seg in re.split(r"[\s,，、。\.;；:：!！?？]+", (query or "").strip()):
        seg = seg.strip()
        if len(seg) >= 2:
            out.append(seg)
    return out


def parse_sogou(body: str, query: str = "") -> list[dict]:
    """解析搜狗结果页 → `[{title, snippet, source, site, url, date, hits}]`。

    按 `h3.vr-title` 的位置切片，而不是按 `<div class="vrwrap">` 配对 ——
    结果块里有嵌套 div，正则配对一定会在某一条上错位（少一条或多半条正文）。
    """
    marks = [m for m in _H3_RE.finditer(body or "")]
    if not marks:
        return []
    toks = _tokens(query)
    items: list[dict] = []
    for i, m in enumerate(marks):
        start = m.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else min(len(body), start + 4000)
        chunk = body[start:end]
        title = _text(m.group(1))
        if not title or any(n in title for n in _NOISE):
            continue
        sm = _SUM_RE.search(chunk) or _SUM_ALT_RE.search(chunk)
        snippet = _text(sm.group(1)) if sm else ""
        # 没摘要的块多半是聚合卡片/视频位，正文在别处 —— 对上标题也没用。
        if not snippet:
            continue
        um = _URL_RE.search(chunk)
        spans = [s for s in _SPAN_RE.findall(chunk) if s.strip()]
        # 末三个 span 固定是「来源 / 站点地址 / 日期」（2026-10-25 实测结构）；
        # 少了就都留空，不猜。
        source = spans[-3] if len(spans) >= 3 else ""
        site = spans[-2] if len(spans) >= 3 else ""
        date = spans[-1] if len(spans) >= 3 else ""
        blob = title + " " + snippet
        hits = sum(1 for t in toks if t in blob)
        items.append(
            {
                "title": title,
                "snippet": snippet[:240],
                "source": _text(source),
                "site": _text(site),
                "date": _text(date),
                "url": um.group(1) if um else "",
                "hits": hits,
            }
        )
    return items


_CLEAN_AT_RE = re.compile(r"@[^\s，。,.!?！？]{1,20}")
_CLEAN_EMOJI_RE = re.compile(r"\[[^\]]{1,6}\]")
_CLEAN_KEEP_RE = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]+")


def clean_frag(text: str, limit: int = 10) -> str:
    """把一句群聊剪成能进检索词的短片段。

    @某人、表情名、标点全去掉 —— 它们对搜索没贡献，还会把 query 拉长。
    搜狗对长句的处理是**直接放弃**（返回 0 条），所以宁可短。
    """
    t = _CLEAN_AT_RE.sub(" ", text or "")
    t = _CLEAN_EMOJI_RE.sub(" ", t)
    t = _CLEAN_KEEP_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()[:limit]


def useful_frag(frag: str) -> bool:
    """这段能不能单独当检索词。

    纯数字不行：搜狗会老老实实返回一堆带这个数字的无关页面，而相关性校验只看
    「有没有出现这个数」，于是把垃圾当成了查到 —— 2026-10-25 在真实 117 群上，
    对方报「2562」，搜回来的 4 条分属 4 个不同游戏（率土之滨、QQ阅读、霓虹深渊）。

    这条也顺手挡住了「好」「嗯」这类短应声。
    """
    t = (frag or "").strip()
    if len(t) < 2:
        return False
    # 「嗯嗯」「好的」「哈哈」这类应声：字数是够的，但拿它搜只会搜出通用页面。
    if re.fullmatch(r"[嗯哦啊哈好行对是的了嘛呀呃额呢吧噢嘿\s]{2,}", t):
        return False
    return bool(re.search(r"[\u4e00-\u9fa5]{2,}|[A-Za-z]{2,}", t))


def build_query(
    chat: str = "",
    messages: list[dict] | None = None,
    extra: str = "",
    intent: str = "攻略",
) -> str:
    """拼检索词：群名里的实体 + 那句问题的短片段 + 意图词。

    刻意**短**：搜狗对长句直接放弃（返回 0 条），而 3~4 个词在 2026-10-25 的对照里
    效果最好（「无尽冬日 英雄 搭配」命中 8/8 相关）。

    群名清洗只做最保守的一步：去掉尾部的「数字+区」「N 群」「(N)」这类组织信息，
    不做分词 —— 「无尽冬日3864区ALL天地联盟」能不能压成「无尽冬日」靠的是
    **只取第一段**这个笨办法，它在实测的 3 个群名上都对。
    """
    base = (chat or "").strip()
    base = re.sub(r"[（(\[【].*?[）)\]】]", " ", base)      # 括号里的补充说明
    base = re.sub(r"\d+\s*[区服群]", " ", base)            # 3864区 / 12服 / 3群
    parts = [
        p for p in re.split(r"[\s\-_·|]+", base)
        if len(p) >= 2 and not p.isdigit()
    ]
    # 「117」「3864」这类纯数字群名不当实体：拿它去搜会命中一堆带这个数字的
    # 无关页面，而相关性校验只看「有没有出现 117」，于是把垃圾当成了查到
    # （2026-10-25 用真实 117 群跑出来的）。
    ent = parts[0] if parts else ""
    frag = clean_frag(extra)
    # 只有「攻略」两个字搜不出东西，还会命中一堆通用页面。宁可明确说「不知道搜什么」，
    # 让用户自己给一个词 —— 猜错一次，用户就再也不会点这个按钮了。
    if not ent and not useful_frag(frag):
        return ""
    out = [ent, frag, intent]
    return " ".join(w for w in out if w).strip()


def search(query: str, limit: int = 5, timeout: float = TIMEOUT) -> dict:
    """搜一次。任何失败都返回 `ok=False` + 一句人话，绝不抛异常。

    返回 `{ok, q, items, weak, note, elapsed_ms}`。
    `weak=True` 表示查到了页面但**内容跟问题没关系** —— 调用方必须把它当「没查到」。
    """
    q = (query or "").strip()
    if not q:
        return {
            "ok": False, "q": "", "items": [], "weak": False,
            "note": "不知道搜什么 —— 在右边输入框说个词",
        }
    t0 = time.perf_counter()
    try:
        import requests  # 项目里 url_fetch 已依赖 requests
    except Exception:  # pragma: no cover - 环境缺库时不影响主流程
        return {"ok": False, "q": q, "items": [], "weak": False, "note": "缺 requests 库"}
    url = SOGOU + urllib.parse.quote(q)
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        resp.encoding = resp.encoding if resp.encoding and resp.encoding.lower() != "iso-8859-1" else "utf-8"
        body = resp.text
        status = resp.status_code
    except Exception as exc:  # noqa: BLE001 —— 网络问题一律降级成「没查到」
        return {
            "ok": False, "q": q, "items": [], "weak": False,
            "note": f"搜索没连上：{type(exc).__name__}",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    if status != 200:
        return {
            "ok": False, "q": q, "items": [], "weak": False,
            "note": f"搜索返回 HTTP {status}",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    # 搜狗反爬时会返回验证码页（HTTP 200、没有 vr-title）。
    if "antispider" in body or "请输入验证码" in body or "验证码" in body[:4000]:
        return {
            "ok": False, "q": q, "items": [], "weak": False,
            "note": "搜索被要求验证码，先按你自己的话回",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    items = parse_sogou(body, q)
    toks = _tokens(q)
    # 命中过半算「对上了」；一条都没命中的话，宁可说没查到，也不把无关内容摆出去。
    strong = [it for it in items if toks and it["hits"] >= max(1, (len(toks) + 1) // 2)]
    use = strong or items[:0]
    weak = not strong and bool(items)
    out = {
        "ok": len(use) >= MIN_ITEMS,
        "q": q,
        "items": use[:limit],
        "weak": weak,
        "note": "" if use else ("查到页面但内容对不上" if weak else "没搜到"),
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    return out


def digest_prompt(digest: dict, messages: list[dict]) -> str:
    """把检索结果压成给模型的几行参考（只在用户主动「照攻略回一句」时才用）。

    只给标题和摘要，**不给 URL**：模型会把 URL 抄进回复里发出去。
    """
    lines = []
    for i, it in enumerate((digest or {}).get("items") or [], 1):
        head = it.get("title") or ""
        snip = it.get("snippet") or ""
        if head or snip:
            lines.append(f"{i}. {head}：{snip}")
    return "\n".join(lines[:4])
