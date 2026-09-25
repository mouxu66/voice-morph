"""文字里的音效标记：`[爆炸]` → 在合成时把那一声拌进语音里。

用户原话（2026-09-25）：

    我就是想用这个程序自动按的时候如何在我说话的步骤能插一个爆炸声，
    然后我继续说话呢

为什么**不是一个窗口**（这是本模块存在的原因）
----------------------------------------------
一开始设想的方案是"做个悬浮窗，说话中途点一下插一声"。但把链路读完之后就知道
那样做不到，而且是结构性的：

   文字 → 逐句合成 → 拼成 wav → **一次性**播进 CABLE → 微信录音 → 发送

音频在**开始播放之前就已经全部合成完了**（`_split_for_budget` → `_record_and_send`），
播放那一刻是 `_play_to_cable` 把整段 wav 灌进虚拟声卡、微信从头录到尾。所以：

· 不存在"程序正在说话"这个中间态 —— 没有可以插入的时间窗口；
· 真要"现场插"，得把播放器改成可中断的流式播放，而**微信已经在录了**，
  时序、静音头、UIA 校验全都要重做，风险极高、收益只是省一次打字。

既然合成早于播放，**把音效在合成阶段拌进去**是同一个效果的更简单做法：
发出去的语音里本来就带着那一声，用户不需要点任何东西、不需要瞄准、
也就没有"窗口该放哪"这个问题。

标记语法
--------
在文字里写 `[名字]`：

    我今天去那个 [爆炸] 那个地方

名字按**素材的显示名**匹配（`sfx_lib.list_samples()` 的 `name`，如"爆炸"/"掌声"），
匹配不到再按 id（如 `boom`）兜底 —— 用户记中文名比记英文 id 自然。
含 `/` 的包内 id（`<pack>/<stem>`）不在这里特殊处理：它的 `name` 同样是显示名，
按 name 就能匹配到，而 `[pack/stem]` 这种写法不该鼓励（用户不该知道包结构）。

★ 三条必须守住的口径
--------------------
1. **未知名字要报错，不能静默丢掉**。用户写了 `[爆炸声]`（多一个字）而程序当
   普通文字念出来，是最糟的结果：他以为插进去了，发出去才发现没有。所以
   `parse()` 收集所有无法解析的标记名，由调用方决定怎么报。
2. **标记本身不进 TTS**。`[爆炸]` 三个字不能被念出来，解析后必须从文本里剔除。
3. **不许跨段**。音效的时长要算进那条语音的预算（微信单条 60s 硬上限），
   否则加了音效的那段会被**静默截断** —— 而"静默"正是这套预算要消灭的东西。
   插入位置在拼接阶段决定，所以本模块只负责给出"第几段之后要插什么"。
"""

from __future__ import annotations

import re

#: 标记语法：`[名字]`。名字不含 `[`/`]`，长度 1~24（超过多半是写错了）。
#: 用**非贪婪**且不允许嵌套 —— `[[爆炸]]` 不是合法标记，会被当成普通文字。
_MARK = re.compile(r"\[([^\[\]]{1,24})\]")


class UnknownSfx(Exception):
    """标记里的名字在素材库里找不到。带上候选名，方便调用方提示"你是想写……吗"。"""

    def __init__(self, name: str, candidates: list[str] | None = None) -> None:
        self.name = name
        self.candidates = candidates or []
        msg = f"没有这个音效：{name}"
        if self.candidates:
            msg += f"（可用：{'、'.join(self.candidates[:8])}）"
        super().__init__(msg)


def _name_index() -> tuple[dict[str, str], list[str], list[str]]:
    """建"名字 → 素材 id"索引。返回 (索引, 显示名列表, 问题列表)。

    惰性导入 `sfx_lib`：它 import 时会读素材目录，而本模块要在**测试里**被导入
    （测试可能没有素材目录）—— 把副作用推迟到真正要解析的时候。

    歧义处理：两个素材同名时**保留先出现的那个**（出厂优先于导入、导入优先于包），
    并在问题列表里记一笔。不报错 —— 用户可能真的不关心是哪一声，而同名本身就是
    素材库的问题，不该让发送失败。
    """
    import sfx_lib

    idx: dict[str, str] = {}
    names: list[str] = []
    problems: list[str] = []
    try:
        samples = sfx_lib.list_samples()
    except Exception as e:  # 素材目录不可读：当成"一个都没有"，由调用方按未知处理
        return {}, [], [f"读不到素材库：{e}"]

    for s in samples:
        sid = str(s.get("id") or "")
        if not sid:
            continue
        name = str(s.get("name") or sid)
        # 两套键都进索引：显示名优先，但 id 也要能匹配（用户可能照着声板写 id）。
        # 先到先得 —— 所以出厂素材要排在前面，`list_samples` 的顺序已经是那样。
        for key in (name, sid):
            k = key.strip()
            if not k:
                continue
            if k in idx:
                if idx[k] != sid:
                    problems.append(f"「{k}」对应多个素材（用了 {idx[k]}，忽略了 {sid}）")
                continue
            idx[k] = sid
        names.append(name)
    return idx, names, problems


def has_mark(text: str) -> bool:
    """这段文字里有没有音效标记？（调用方用来决定要不要走解析路径，省一次索引构建）"""
    return bool(_MARK.search(text or ""))


def parse(text: str) -> tuple[list[tuple[str, str | None]], list[str], list[str]]:
    """把文字切成 `[(文字片段, 后面的音效 id 或 None), …]`。

    返回 `(片段序列, 无法解析的名字, 提示)`。

    语义：**音效跟在它前面那段文字之后**。所以

        我今天去那个 [爆炸] 那个地方

    → `[("我今天去那个", "boom"), ("那个地方", None)]`

    开头就是标记（`[爆炸] 你好`）时第一个片段是空串 —— 保留它而不是丢掉，
    这样"音效在最前面"这个信息不会丢（空片段拼起来无副作用）。
    """
    text = text or ""
    idx, _names, problems = _name_index()
    segments: list[tuple[str, str | None]] = []
    unknown: list[str] = []
    buf: list[str] = []
    pos = 0

    for m in _MARK.finditer(text):
        buf.append(text[pos:m.start()])
        pos = m.end()
        raw = m.group(1).strip()
        sid = idx.get(raw)
        if sid is None:
            unknown.append(raw)
            # 解析不了就把标记**原样留在文字里** —— 调用方要么报错、要么当普通文字念，
            # 但不能让它凭空消失（用户会以为插进去了）。这里选择保留原文，
            # 由上层决定；下面的 segments 里它就成了普通文字。
            buf.append(m.group(0))
            continue
        segments.append(("".join(buf), sid))
        buf = []
    buf.append(text[pos:])
    segments.append(("".join(buf), None))
    return segments, unknown, problems


def available() -> list[str]:
    """可用音效的显示名列表（给界面提示 / 报错文案用）。"""
    _idx, names, _p = _name_index()
    return names
