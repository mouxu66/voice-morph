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


def parse(text: str) -> list[Hit]:
    """一句话可能同时有"点歌"和"指定音色"两个诉求，所以返回列表。

    顺序固定：先 song 后 voice —— 前端按顺序渲染，用户从上往下看就是
    "要发什么" → "怎么发"。
    """
    if not isinstance(text, str) or not text.strip():
        return []
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
