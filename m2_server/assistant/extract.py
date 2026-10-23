"""把聊天里的一句话解析成候选动作 —— **纯规则，不调模型**。

为什么先规则再模型
------------------
点歌这件事的句式收敛得很厉害：`发/来/放/唱/点` + `一(首|曲|个)` + 名字。
规则跑完只要几十微秒，且**每一条命中都能回指原文片段**（`Hit.matched`），
用户一眼能看出"AI 是看到哪句话才觉得要发歌的"。反过来先上模型，一是不稳定
（同一句话两次解析出不同歌名），二是给不出"凭什么是这首"的凭据。

模型的位置
----------
规则没命中、但句子看着像请求（`looks_like_request`）时，才交给 `llm.py` 深解析。
那里同样只产出候选，且必须带 `evidence`。

抽不准就不抽
------------
"你发的那首歌真好听" 里有 `发` 和 `歌`，但它不是请求。所以这里刻意**要求动词后
紧跟数量词或引号**（`发一(首|曲)` / `发《X》`）。宁可漏，不可错 —— 漏了用户自己
点一下就行，错了会拿错的歌名去搜、然后被发出去。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 点歌动词。`整`/`甩` 是口语（"整一首""甩首歌过来"）。
SONG_VERBS = "发来放唱点整甩求"
#: ★ 必须是**字符类**而不是 `(?:发来放唱点整甩求)` —— 后者在正则里匹配的是
#: "发来放唱点整甩求" 这串字面连续的文本，一个都命中不了。这个坑踩过一次。
_VERB = "[" + SONG_VERBS + "]"

#: 语气尾巴。抽出来的候选几乎总带着"吗/吧/谢谢"，不剥干净会搜不到歌。
_TAIL = re.compile(
    r"(?:好不好|行不行|可以不|可以吗|能不能|谢谢|拜托|麻烦了|呗|吧|吗|嘛|呢|啊|呀|哦|啦|了|~|～|。|！|!|？|\?)+$"
)

#: 常见方言/口音名。命中的会被当**诉求**记录下来，但执行层能不能做到是另一回事
#: （见 executor：TTS 目前只有 zh/en，方言要靠改写法或 RVC 音色，不能假装支持）。
DIALECTS = (
    "粤语",
    "广东话",
    "四川话",
    "重庆话",
    "东北话",
    "上海话",
    "闽南话",
    "台湾腔",
    "天津话",
    "河南话",
    "陕西话",
    "湖南话",
    "方言",
)

_SONG_PATTERNS = (
    # 发/来/放/唱 + 数量词 + 名字（名字允许被书名号/引号包住，或任意到标点为止）
    # ★ 量词必须写 {{1,40}}：f-string 会把 {1,40} 当成 set 字面量求值，
    #   正则里就变成 "(1, 40)"，一个都不匹配 —— 这个坑踩过一次，别再改回去。
    re.compile(
        rf"{_VERB}(?:一|1)?(?:首|曲|个)(?:歌)?\s*[《<「\"“']?"
        r"([^《》<>「」\"“”'\n，。！？；、]{1,40})"
    ),
    # 发《X》/ 来《X》：没有数量词但有书名号，指代很明确
    re.compile(rf"{_VERB}\s*[《<「\"“']([^》>」\"”'\n]{{1,40}})"),
    # 我想听 X / 想听 X / 听点 X
    re.compile(r"(?:我)?(?:想|要)(?:听|点)\s*[《<「\"“']?([^《》<>「」\"“”'\n，。！？；、]{1,40})"),
)

#: 「用 X 的声音说 Y」「用粤语说 Y」
_VOICE_SAY = re.compile(
    r"用\s*[《<「\"“']?([^《》<>「」\"“”'\n，。]{1,16}?)[》>」\"”']?\s*的?(?:声音|嗓子|语气|腔调)"
    r"\s*(?:来)?(?:说|讲|念|读|喊)\s*[：:]?\s*(.+)$"
)
_DIALECT_SAY = re.compile(
    r"(?:用|拿)\s*(" + "|".join(DIALECTS) + r")\s*(?:来)?(?:说|讲|念|读|喊)\s*[：:]?\s*(.+)$"
)
#: 单纯「说一句 Y」（没有指定音色时，用当前选中音色）
_SAY = re.compile(r"(?:说一句|说段|念一句|读一下)\s*[：:]?\s*(.+)$")

#: 看着像在跟我提请求（用于决定要不要送给模型深解析）。
_REQUEST_HINT = re.compile(
    r"(?:发|来|放|唱|点|整|甩|搜|找|说|念|读|回|分析|看看|帮我|能不能|可以|想要|想听)"
)


@dataclass
class Hit:
    """一次规则命中。`matched` 是原文里被抠出来的那一段，直接当 evidence 给用户看。"""

    kind: str
    params: dict
    matched: str
    confidence: float = 0.8
    notes: list[str] = field(default_factory=list)


def _clean_name(raw: str) -> str:
    """剥掉语气尾巴与包裹符号。空串表示"看着像点歌但没说出名字"。"""
    s = raw.strip().strip("《》<>「」\"“”'")
    # 反复剥：'晴天吗谢谢' → '晴天吗' → '晴天'
    prev = None
    while prev != s:
        prev = s
        s = _TAIL.sub("", s).strip().strip("，。、,.")
    return s.strip()


def parse_song(text: str) -> Hit | None:
    """从一句话里抽点歌请求。抽不出名字返回 None（**不**用整句兜底）。"""
    for pat in _SONG_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        name = _clean_name(m.group(1))
        if not name:
            continue
        # 名字太短（一个字）几乎都是误抽："发个了"、"来首啊"
        if len(name) < 2 and not re.search(r"[a-zA-Z]{2,}", name):
            continue
        # 名字里还有"歌"且非常短，多半是"发首歌"这种泛指 —— 指名道姓才有意义
        if name in ("歌", "歌曲", "音乐", "声音"):
            continue
        matched = text[m.start(): m.end()].strip()
        return Hit(
            kind="song.send",
            params={"query": name},
            matched=matched,
            confidence=0.85 if "《" in matched or "「" in matched else 0.75,
        )
    return None


def parse_voice(text: str) -> Hit | None:
    """抽「用 X 的声音说 Y」/「用粤语说 Y」/「说一句 Y」。"""
    m = _DIALECT_SAY.search(text)
    if m:
        dialect, body = m.group(1), m.group(2).strip()
        if body:
            return Hit(
                kind="voice.say",
                params={"text": body, "dialect": dialect},
                matched=text[m.start(): m.end()].strip(),
                confidence=0.8,
            )
    m = _VOICE_SAY.search(text)
    if m:
        who, body = m.group(1).strip(), m.group(2).strip()
        if body:
            return Hit(
                kind="voice.say",
                params={"text": body, "voice": who},
                matched=text[m.start(): m.end()].strip(),
                confidence=0.75,
            )
    m = _SAY.search(text)
    if m:
        body = m.group(1).strip()
        if body:
            return Hit(
                kind="voice.say",
                params={"text": body},
                matched=text[m.start(): m.end()].strip(),
                confidence=0.6,
            )
    return None


# ---------------------------------------------------------------- 绝对时间音效
# 用户原话（2026-10-23）："我说给我生成一个什么什么的话，然后在第几秒插入音效"。
# 内联 `[爆炸]` 只表达"这句之后"，做不到"第 3.5 秒"；这里解析绝对秒数，
# 由执行器在合成完之后按时间叠加（`sfx_lib.mix_into` 的 layer 模式）。

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

#: "第 3 秒 / 在3秒的时候 / 三秒处"。数字支持阿拉伯与中文（"第三秒"很常见）。
_TIME = (
    r"(?:在\s*)?(?:第\s*)?"
    r"(\d+(?:\.\d+)?|[零一二两三四五六七八九十]+)"
    r"\s*秒(?:钟)?(?:\s*(?:的时候|时候|时|处|那里|的位置))?"
)
_SFX_VERBS = "插|加入|加|放|来|响|播|配"
#: 时间在前、名字在后："第3秒插入爆炸声"
_SFX_FORWARD = re.compile(
    _TIME + rf"\s*[，,]?\s*(?:{_SFX_VERBS})(?:一声|一个|一段|个|下)?\s*([^，,。；;！？!?\n]{{1,20}})"
)
#: 名字在前、时间在后："爆炸声放在第3秒"
_SFX_BACKWARD = re.compile(
    r"([^，,。；;！？!?\n]{1,20}?)\s*(?:放|插|加|来|响|播)(?:在|到)?\s*" + _TIME
)
_SFX_LEAD = re.compile(r"^(?:个|一个|一声|一段|下|个音效|一个音效)+")
_SFX_TAIL = re.compile(r"(?:音效|一下|一声|的声音)$")
#: 用户描述需求时的"外壳"：「给我生成一句话，内容是…」「帮我生成一段话：…」
#: 这些不是要念的字。只在**没解析出「说一句 X」**时剥，避免把"说实话"误伤成"实话"。
_FRAME = re.compile(
    r"^(?:(?:请|帮我|麻烦|给我|你|先|再)\s*)?"
    r"(?:"
    r"(?:生成|合成|写|来|整|弄)\s*(?:一|1)?\s*(?:句|段|条)?\s*(?:话|语音|语音条|台词)?"
    r"|(?:跟|和|对)\s*(?:我|大家|群里|大家伙)\s*说"
    r")"
    r"\s*[，,：:、]?\s*"
)
#: 「内容是…」「台词：…」—— 换行说法的引子
_CONTENT = re.compile(r"^(?:内容|正文|台词)\s*(?:是|：|:)\s*")
#: 删掉音效片段后常留下「…，然后」
_TAIL_CONN = re.compile(r"(?:[，,]\s*)?(?:然后|接着|之后|最后)\s*$")


def _seconds(raw: str) -> float | None:
    """把 '3' / '3.5' / '三' / '十五' 变成秒。解析不了返回 None（宁可漏，不猜）。"""
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return max(0.0, float(s))
    except ValueError:
        pass
    if "十" in s:
        left, _, right = s.partition("十")
        if (left and left not in _CN_DIGITS) or (right and right not in _CN_DIGITS):
            return None
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return float(tens * 10 + ones)
    if len(s) == 1 and s in _CN_DIGITS:
        return float(_CN_DIGITS[s])
    return None


def _sfx_names() -> list[str]:
    """素材库的显示名。素材目录不可读时返回空表（此时任何名字都解析不了）。"""
    import sfx_mark

    return sfx_mark.available()


def _canon_sfx_name(raw: str, names: list[str]) -> str | None:
    """把口语里的音效名归一到素材库的显示名（或 id）。

    4B 与用户口语都不会严格照抄素材名：实测 4B 把"爆炸"写成"爆炸声"。
    所以按"去量词 → 去尾巴 → 去'声' → 包含匹配"逐级放宽，**命中即返回**；
    全不命中返回 None —— 由调用方明确告诉用户"没这个音效"，绝不静默当文字念。
    """
    import sfx_mark

    s = (raw or "").strip().strip("《》<>「」\"“”'，,。.!！?？;；:：")
    if not s:
        return None
    cands = [s]
    stripped = _SFX_LEAD.sub("", s).strip()
    if stripped and stripped != s:
        cands.append(stripped)
    for c in list(cands):
        t = _SFX_TAIL.sub("", c).strip()
        if t and t != c:
            cands.append(t)
    for c in list(cands):
        if c.endswith("声") and len(c) > 2:
            cands.append(c[:-1])
    for c in cands:
        if c in names:
            return c
        _segs, unknown, _p = sfx_mark.parse(f"[{c}]")
        if c and not unknown:
            return c  # 写的是素材 id（如 boom）也认
    hits = [n for n in names if n and n in s]
    return max(hits, key=len) if hits else None


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """删掉已识别的音效指令片段（合并重叠区间），其余原样保留。"""
    if not spans:
        return text
    merged: list[tuple[int, int]] = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    out: list[str] = []
    pos = 0
    for a, b in merged:
        out.append(text[pos:a])
        pos = b
    out.append(text[pos:])
    return "".join(out)


def _tidy(text: str) -> str:
    """收拾删片段留下的空标点：`说一句，` → `说一句`；`，，` → `，`。"""
    s = re.sub(r"[，,；;]{2,}", "，", text or "")
    return s.strip().strip("，,。；;：:、 \t\n")


def _clean_spoken(text: str) -> str:
    """剥掉"描述需求"的外壳，只留要念的字。

    `给我生成一句话，内容是我今晚不去了，然后` + 音效片段删掉之后，
    剩下的是**指令的外壳**而不是台词 —— 原样念出来就是"给我生成一句话…"，
    这是用户最不能接受的结果（他描述需求，AI 却把需求念给群里听）。
    """
    s = _tidy(text)
    prev = None
    while prev != s:
        prev = s
        s = _FRAME.sub("", s, count=1)
        s = _CONTENT.sub("", s, count=1)
        s = _TAIL_CONN.sub("", s, count=1)
        s = _tidy(s)
    return s


def parse_sfx_instruction(text: str) -> Hit | None:
    """解析"在第 N 秒插音效"的指令，返回带 `sfx` 参数的 voice.say 候选。

    `sfx` 是 `[{"name": 显示名, "at_s": 秒}, …]`，交给执行器在合成后按绝对时间叠加。
    音效名认不出来时**不生成可执行提案**（`text` 留空 → 校验判 missing），
    并用 `notes` 说明"没这个音效 + 可用列表" —— 静默把它当文字念出来是最坏结果。
    """
    raw = text or ""
    names = _sfx_names()
    items: list[dict] = []
    spans: list[tuple[int, int]] = []
    matched: list[str] = []
    unknown: list[str] = []

    for pat in (_SFX_FORWARD, _SFX_BACKWARD):
        for m in pat.finditer(raw):
            if any(m.start() < b and a < m.end() for a, b in spans):
                continue  # 前后两种句式命中了同一段，别算两次
            if pat is _SFX_FORWARD:
                raw_time, raw_name = m.group(1), m.group(2)
            else:
                raw_time, raw_name = m.group(2), m.group(1)
            at_s = _seconds(raw_time)
            if at_s is None:
                continue
            canon = _canon_sfx_name(raw_name, names)
            spans.append((m.start(), m.end()))
            if canon is None:
                unknown.append(_SFX_LEAD.sub("", raw_name).strip())
                continue
            items.append({"name": canon, "at_s": at_s})
            matched.append(m.group(0).strip())

    if not items and not unknown:
        return None

    cleaned = _tidy(_strip_spans(raw, spans))
    notes: list[str] = []
    if unknown:
        avail = "、".join(names[:12])
        notes.append(f"没有这个音效：{'、'.join(unknown)}" + (f"（可用：{avail}）" if avail else ""))
    if not items:
        # 形如"第3秒放个爆炸声"：句式对，但名字不认识 —— 不生成提案，只回提示。
        return Hit(kind="voice.say", params={"text": ""}, matched="；".join(matched) or raw.strip(), notes=notes)

    voice = parse_voice(cleaned)
    params = dict(voice.params) if voice else {}
    params["text"] = _clean_spoken(str(params.get("text") or cleaned))
    if not params["text"]:
        return Hit(
            kind="voice.say",
            params={"text": ""},
            matched="；".join(matched),
            notes=notes + ["只有音效、没有要念的话：先说清这句要念什么"],
        )
    params["sfx"] = items
    return Hit(
        kind="voice.say",
        params=params,
        matched="；".join(matched),
        confidence=0.85,
        notes=notes,
    )


def parse(text: str) -> list[Hit]:
    """一句话可能同时有"点歌"和"指定音色"两个诉求，所以返回列表。

    顺序固定：先 song 后 voice —— 前端按顺序渲染，用户从上往下看就是
    "要发什么" → "怎么发"。

    ★ 带绝对秒数音效的指令**独占**这句话：`说一句明天开会，第3秒插入爆炸声`
    里的"第3秒..."不是要念的内容，继续走通用 `parse_voice` 会把它一起念出来。
    所以这里先试指令解析，命中就只返回那一条。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    ins = parse_sfx_instruction(text)
    if ins is not None:
        return [ins]
    hits: list[Hit] = []
    song = parse_song(text)
    if song:
        hits.append(song)
    voice = parse_voice(text)
    if voice:
        hits.append(voice)
    return hits


def looks_like_request(text: str) -> bool:
    """要不要为了这一句去调模型。判据刻意宽松 —— 深解析的代价是几百毫秒，
    比"漏了一句该发的"便宜。真正的护栏在 confirm 那一步，不在解析这一步。"""
    if not isinstance(text, str):
        return False
    t = text.strip()
    if len(t) < 2 or len(t) > 400:
        return False
    return bool(_REQUEST_HINT.search(t))
