"""文字里的音效标记 `[爆炸]` → 解析器（`sfx_mark.py`）。

用户原话（2026-09-25）：

    我就是想用这个程序自动按的时候如何在我说话的步骤能插一个爆炸声，
    然后我继续说话呢

方案定的是「自动发送时：文字里标记，程序合成时就拌进去」+「按名字写：
「[爆炸]」「[笑声]」」。为什么不是悬浮窗那套，见 `sfx_mark.py` 模块注释
（音频在播放前就整段合成完了，没有可插入的时间窗口 —— 这是结构性的）。

★ 这个文件守护的是**三条口径**（`sfx_mark.py` 文档里点名的那三条）：
  1. 未知名字要**报错**，不能静默丢掉（用户以为插进去了、发出去才发现没有）；
  2. 标记本身**不进 TTS**（`[爆炸]` 三个字不能被念出来）；
  3. 音效时长要**算进预算**（在 `wechat_voice` 那侧，见 test_wechat_chunking.py）。

⚠️ 素材目录是**用户真实数据**（出厂素材 + `<media>/soundboard` 导入的）。
   本文件一律不依赖真实素材库：需要名字索引时用 `_fake_lib` 把 `sfx_lib`
   的两个函数打掉。**不许**让单测去读用户桌面上的素材（同类事故见
   `docs/犯错档案-工程.md` §8.36 / §8.37）。

⚠️ 变异测试（本仓传统，见 `docs/犯错档案-工程.md` §8.19）：把 `parse()` 里
   `unknown.append(raw)` 那行删掉、或让它不再把标记留在文字里，
   本文件必须**变红**。做变异时**整行删除**，不能只注释 ——
   断言可能匹配到注释里的同名文本（速查表第 46 条）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import pytest  # noqa: E402

import sfx_lib  # noqa: E402
import sfx_mark  # noqa: E402

# ---------------- 夹具 ----------------


#: 假素材库：id → 显示名。和出厂那 6 条**同形**（名字是中文、id 是英文），
#: 这样用例读起来就是用户会写的那个样子。
_FAKE = {
    "boom": "爆炸",
    "applause": "掌声",
    "ding": "叮",
}


@pytest.fixture
def fake_lib(monkeypatch):
    """把 `sfx_lib` 打成一个只有 3 条素材的假库（含一条同名歧义，见下）。"""

    def _list_samples():
        return [
            {"id": "boom", "name": "爆炸", "tags": [], "icon": "💥", "duration_s": 1.8},
            {"id": "applause", "name": "掌声", "tags": [], "icon": "👏", "duration_s": 2.4},
            {"id": "ding", "name": "叮", "tags": [], "icon": "🔔", "duration_s": 0.6},
        ]

    monkeypatch.setattr(sfx_lib, "list_samples", _list_samples)
    return _list_samples


# ---------------- has_mark：便宜的先行判据 ----------------


def test_has_mark_detects():
    assert sfx_mark.has_mark("我今天去那个 [爆炸] 那个地方") is True
    assert sfx_mark.has_mark("[爆炸]") is True


def test_has_mark_false_on_plain_text():
    """没有标记的普通文字必须判 False —— 调用方靠它跳过整条解析路径。"""
    assert sfx_mark.has_mark("我今天去那个地方") is False
    assert sfx_mark.has_mark("") is False
    assert sfx_mark.has_mark(None) is False


def test_has_mark_on_empty_brackets_is_false():
    """`[]` 不是标记（名字长度至少 1）—— 它是普通文字，别当空名字去查库。"""
    assert sfx_mark.has_mark("你好 [] 啊") is False


# ---------------- parse：位置语义（★ 核心口径） ----------------


def test_mark_attaches_to_text_before_it(fake_lib):
    """★ 语义：音效跟在**它前面那段文字之后**。

    `我今天去那个 [爆炸] 那个地方` 的直觉是"说到'那个'的时候炸一下"，
    所以第一段挂音效、第二段是剩下的文字。
    """
    segs, unk, _prob = sfx_mark.parse("我今天去那个 [爆炸] 那个地方")
    assert unk == []
    assert segs == [("我今天去那个 ", "boom"), (" 那个地方", None)]


def test_mark_at_tail_keeps_trailing_empty_segment(fake_lib):
    """句尾的标记 → 最后一段是空串（占位保留，语义没丢）。"""
    segs, unk, _p = sfx_mark.parse("我说完了 [掌声]")
    assert unk == []
    assert segs == [("我说完了 ", "applause"), ("", None)]


def test_mark_at_head_keeps_leading_empty_segment(fake_lib):
    """句首的标记 → 第一个片段是空串。

    这是有意的：`[爆炸] 你好` 是"先炸再说话"，丢掉空片段就把这个信息丢了。
    空片段拼起来无副作用，但**位置语义**得以保留。
    """
    segs, unk, _p = sfx_mark.parse("[爆炸] 你好")
    assert unk == []
    assert segs == [("", "boom"), (" 你好", None)]


def test_only_mark_no_text(fake_lib):
    """整句就是一个标记（`[掌声]` 单独成句）→ 文字是空的，但音效还在。"""
    segs, unk, _p = sfx_mark.parse("[掌声]")
    assert unk == []
    assert segs == [("", "applause"), ("", None)]


def test_multiple_marks_in_one_sentence(fake_lib):
    """一句话里多个标记 → 每段各挂一个（调用方只取最后一个，见下）。"""
    segs, unk, _p = sfx_mark.parse("啊 [爆炸] 呀 [掌声] 哈")
    assert unk == []
    assert [sid for _t, sid in segs] == ["boom", "applause", None]
    # 文字片段拼起来必须**等于原文去掉标记**（标记一个字都不许留在里面）
    assert "".join(t for t, _ in segs) == "啊  呀  哈"


def test_text_without_mark_is_single_segment(fake_lib):
    segs, unk, _p = sfx_mark.parse("没有标记的一句话。")
    assert segs == [("没有标记的一句话。", None)]
    assert unk == []


def test_whitespace_inside_mark_is_tolerated(fake_lib):
    """`[ 爆炸 ]` 视同 `[爆炸]` —— 用户在中文输入法下多打空格太常见了。"""
    segs, unk, _p = sfx_mark.parse("好啊 [ 爆炸 ] 行")
    assert unk == []
    assert [sid for _t, sid in segs] == ["boom", None]


def test_text_is_preserved_exactly_besides_marks(fake_lib):
    """★ 非标记文字**一字不易** —— 解析不是"重写文本"，只是把标记挖出来。"""
    src = "第一句，有逗号。还有「引号」和（括号）以及 emoji 😀"
    segs, unk, _p = sfx_mark.parse(src)
    assert "".join(t for t, _ in segs) == src
    assert unk == []


def test_empty_string(fake_lib):
    segs, unk, _p = sfx_mark.parse("")
    assert segs == [("", None)]
    assert unk == []


def test_none_is_handled(fake_lib):
    segs, unk, _p = sfx_mark.parse(None)
    assert segs == [("", None)]
    assert unk == []


# ---------------- parse：名字匹配（显示名优先，id 兜底） ----------------


def test_match_by_chinese_display_name(fake_lib):
    """按**显示名**匹配是最主打的用法：用户记中文名比记英文 id 自然。"""
    segs, _u, _p = sfx_mark.parse("停 [爆炸]")
    assert segs[0][1] == "boom"


def test_match_by_id_also_works(fake_lib):
    """id 也能匹配 —— 用户可能照着声板的英文名/日志写。"""
    segs, _u, _p = sfx_mark.parse("停 [boom]")
    assert segs[0][1] == "boom"


def test_name_and_id_are_both_indexed(fake_lib):
    """同一个素材的两套键都要能命中（且都指向同一个 id）。"""
    for key in ("爆炸", "boom"):
        segs, unk, _p = sfx_mark.parse(f"a [{key}] b")
        assert unk == []
        assert segs[0][1] == "boom", key


def test_unknown_name_is_reported_and_kept_in_text(fake_lib):
    """★ 口径 1：未知名字要**报出来**，而且标记**不许凭空消失**。

    为什么两件事都要做：
      · 只报不保留 → 上层若选择"照常发"，标记就没了，用户不知道发生了什么；
      · 只保留不报 → 上层无从知道，`[爆炸声]` 会被**逐字念出来**。
    所以 `parse()` 同时给出 `unknown` 和"原样留在文字里"的标记。
    """
    segs, unk, _p = sfx_mark.parse("啊 [爆炸声] 呀")
    assert unk == ["爆炸声"]
    assert "".join(t for t, _ in segs) == "啊 [爆炸声] 呀"
    assert all(sid is None for _t, sid in segs), "未知标记不该被当成音效"


def test_unknown_name_has_no_candidate_hint_from_parser(fake_lib):
    """解析器只如实报名字，不做"猜你想写哪个"—— 候选提示属于上层文案。"""
    _segs, unk, _p = sfx_mark.parse("[不存在的音效]")
    assert unk == ["不存在的音效"]


def test_mixed_known_and_unknown(fake_lib):
    """已知的照常生效、未知的照常报错 —— 不能"有一个错就全放弃"。"""
    segs, unk, _p = sfx_mark.parse("a [爆炸] b [没有这个] c [掌声]")
    assert unk == ["没有这个"]
    # 已知标记照常生效：`[爆炸]` 挂给 "a "，`[掌声]` 挂给中间那一大段。
    # 未知标记（`[没有这个]`）被原样留在**文字**里（口径 1），且它**不打断**分段 ——
    # 所以中间那段文字长这样，音效挂在段末。
    assert segs == [
        ("a ", "boom"),
        (" b [没有这个] c ", "applause"),
        ("", None),
    ]
    # 拼回去 == 原文去掉两个**已解析**的标记（`[没有这个]` 原样留着，它要被念出来）
    assert "".join(t for t, _ in segs) == "a  b [没有这个] c "  # noqa: E501


def test_unknown_is_reported_in_order(fake_lib):
    """多个未知名的顺序要稳定（提示文案里按原顺序列出才好读）。"""
    _segs, unk, _p = sfx_mark.parse("[甲] x [乙] y [丙]")
    assert unk == ["甲", "乙", "丙"]


def test_same_unknown_twice_is_reported_twice(fake_lib):
    """同一个错写两次报两条 —— 上层去重是上层的事，解析器不替它做决定。"""
    _segs, unk, _p = sfx_mark.parse("[错] a [错]")
    assert unk == ["错", "错"]


# ---------------- 语法边界：什么算标记、什么不算 ----------------


def test_nested_brackets_match_the_inner_span():
    """`[[爆炸]]` 的**内层** `[爆炸]` 是一个合法标记（外层 `[`/`]` 是普通文字）。

    这条是**记录现状**而不是"规定正确"：正则 `\\[([^\\[\\]]{1,24})\\]` 不允许嵌套，
    所以在 `[[爆炸]]` 上它会从第二个 `[` 开始匹配，命中 `[爆炸]` —— 外层括号
    被留成普通文字。    用户几乎不会这么写（`[爆炸]` 就够了），但**行为必须是确定的**：
    将来谁把它"修成"不匹配内层，这条会红，提醒他一并想想是不是要改文案。
    """
    segs, unk, _p = sfx_mark.parse("[[爆炸]]")
    assert unk == []
    # 正则 `\[([^\[\]]{1,24})\]` 在 `[[爆炸]]` 上的匹配窗口：先看见外层 `[`，
    # 但 `[` 不在字符类里 → 起点后移到第二个 `[`，命中 `[爆炸]`，
    # 剩下 `[`（第一个）+ `]`（最后那个）是普通文字 → 合起来是 `"[]"`。
    assert "".join(t for t, _ in segs) == "[]"
    assert segs[0][1] == "boom"
    assert sfx_mark.has_mark("[[爆炸]]") is True, "has_mark 与 parse 的判断必须一致"


def test_unclosed_bracket_is_plain_text(fake_lib):
    """没闭合的 `[爆炸` 是普通文字，不该被吞掉。"""
    segs, unk, _p = sfx_mark.parse("我说 [爆炸 声音")
    assert unk == []
    assert "".join(t for t, _ in segs) == "我说 [爆炸 声音"


def test_too_long_name_is_not_a_mark(fake_lib):
    """名字长度上限 24：超了多半是写错了，当普通文字（比如把一整段话套进括号）。"""
    long_name = "x" * 25
    assert sfx_mark.has_mark(f"[{long_name}]") is False
    segs, unk, _p = sfx_mark.parse(f"啊 [{long_name}] 呀")
    assert unk == []
    assert "".join(t for t, _ in segs) == f"啊 [{long_name}] 呀"


def test_name_at_max_length_boundary():
    """恰好 24 个字符 → 算标记（边界不能取严）。"""
    assert sfx_mark.has_mark("[" + "x" * 24 + "]") is True


def test_code_brackets_are_reported_unknown_not_silently_ignored(fake_lib):
    """代码里的 `arr[0]` 会被当成标记 —— 这是**已知且可接受**的代价。

    为什么接受它：标记语法就是"方括号里一个短名字"，而区分 `arr[0]`（代码）与
    `[爆炸]`（音效）需要读懂上下文，正则做不到。这里选的是**错得响亮**：
    `arr[0]` 变成"没有这个音效：0"报给用户，他一眼就知道要换个写法
    （或者在设置里关掉标记）。反过来"尽量不误报"会让正则越来越绕，
    而漏报（该插的没插）比误报（多问一句）代价大得多。

    ⚠️ 硬边界只有一条：名字超 24 字符就不算标记（`x[1234…]` 这类长索引不会误报）。
    `arr[0]`、`a[i]` 这种短索引**会**误报 —— 这是接受的代价。
    """
    segs, unk, _p = sfx_mark.parse("arr[0] = 1")
    assert unk == ["0"], "已知代价：应报「没有这个音效：0」，而不是静默通过"
    assert "".join(t for t, _ in segs) == "arr[0] = 1", "报错的同时文字必须原样保留"

    # 超长名字（>24）不算标记 —— 这是防误报的硬边界
    src = "x[" + "9" * 25 + "] 长索引"
    _segs, unk2, _p2 = sfx_mark.parse(src)
    assert unk2 == [], f"{src} 不该被当成标记"
    assert _segs == [(src, None)], "超长名要连括号一起原样保留"

    # ⚠️ 边界提醒：23 字符的短索引仍然**会**被当成标记（上限是 24）。
    # 把它写出来是为了让"24 到底够不够宽"这个决定有据可查 —— 若将来要收窄，
    # 改的是 `_MARK` 里的数字，而这条会红、提醒改文案。
    assert sfx_mark.has_mark("x[" + "9" * 23 + "]") is True


def test_emoji_name_is_a_mark(fake_lib):
    """emoji 也是合法名字（用户可能直接写声板格子上的图标）。"""
    assert sfx_mark.has_mark("[💥]") is True


def test_adjacent_marks(fake_lib):
    """两个标记紧挨着（`[爆炸][掌声]`）—— 各成一段，中间文字是空串。"""
    segs, unk, _p = sfx_mark.parse("[爆炸][掌声]")
    assert unk == []
    assert [sid for _t, sid in segs] == ["boom", "applause", None]


# ---------------- available()：给界面提示 / 报错文案 ----------------


def test_available_returns_display_names(fake_lib):
    """可用列表给的是**显示名**（用户就是按显示名写的），且顺序稳定。"""
    assert sfx_mark.available() == ["爆炸", "掌声", "叮"]


def test_available_is_empty_when_lib_unreadable(monkeypatch):
    """素材库读不到时返回空列表（不崩）—— 发送路径不能让"列素材失败"变成 500。"""

    def _boom():
        raise OSError("目录不可读")

    monkeypatch.setattr(sfx_lib, "list_samples", _boom)
    assert sfx_mark.available() == []


def test_unreadable_lib_reports_problem_not_crash(monkeypatch):
    """库读不到 → `parse()` 把**所有**标记都当未知（并给一条提示），但绝不抛异常。"""

    def _boom():
        raise OSError("目录不可读")

    monkeypatch.setattr(sfx_lib, "list_samples", _boom)
    segs, unk, prob = sfx_mark.parse("啊 [爆炸] 呀")
    assert unk == ["爆炸"]
    assert prob and "读不到素材库" in prob[0]
    assert "".join(t for t, _ in segs) == "啊 [爆炸] 呀"


# ---------------- 同名歧义：不报错，但记一笔 ----------------


def test_duplicate_name_keeps_first_and_records_problem(monkeypatch):
    """★ 两个素材同名 → 保留**先出现的**（出厂优先于导入/包），并记一条提示。

    为什么不报错：同名本身就是素材库的问题，不该让**发送**失败。用户写
    `[掌声]` 心里想的是"来点掌声"，系统挑哪一条都比"发送失败"好。
    """

    def _list_samples():
        return [
            {"id": "boom", "name": "爆炸"},
            {"id": "boom2", "name": "爆炸"},  # 同名（比如导入了一条也叫"爆炸"）
        ]

    monkeypatch.setattr(sfx_lib, "list_samples", _list_samples)
    segs, unk, prob = sfx_mark.parse("啊 [爆炸]")
    assert unk == [], "同名不该被当成未知"
    assert segs[0][1] == "boom", "应该保留先出现的那个（出厂优先）"
    assert any("对应多个素材" in p for p in prob), f"没记录歧义：{prob}"


# ---------------- 真实出厂素材（只读，不做写入） ----------------


def test_builtin_samples_exist_and_are_addressable():
    """出厂 6 条素材仍能被名字索引到 —— 这是"用户写得出来"的前提。

    这条**故意**读真实 `sfx_lib`（只读，不写）：`[爆炸]` 在真机上必须能用，
    而上面全部用例都用假库 —— 假库绿而真库没有 `boom` 的话，
    用户看到的仍然是"没有这个音效：爆炸"。所以这条是防止"假库自嗨"的护栏。
    """
    names = sfx_mark.available()
    if not names:  # pragma: no cover —— 素材缺席（比如裁剪过的分发）
        pytest.skip("本机没有出厂素材，跳过真库可用性检查")
    idx, _names, _prob = sfx_mark._name_index()
    assert "爆炸" in idx, f"出厂素材里没有「爆炸」：{names}"
    assert idx["爆炸"] == "boom"
