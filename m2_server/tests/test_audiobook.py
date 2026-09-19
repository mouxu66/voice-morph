"""audiobook 文本切分 / SRT 解析单测（纯函数，无 GPU / 网络依赖）。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import audiobook  # noqa: E402

# ---------------- split_sentences ----------------


def test_empty_and_whitespace():
    assert audiobook.split_sentences("") == []
    assert audiobook.split_sentences("   \n  ") == []


def test_short_text_flushed_even_below_min():
    # 只有一个短句：即使长度 < _MIN_SENT 也要产出，不能丢
    assert audiobook.split_sentences("hi") == ["hi"]


def test_multiple_short_sentences_not_lost():
    """回归：旧实现 `buf = (buf + s) if not buf else buf` 会在 buf 未达
    _MIN_SENT 时静默丢弃后续句子。三个短句应全部保留、按序拼接。"""
    out = audiobook.split_sentences("a。b。c。")
    assert out == ["a。b。c。"]
    assert "".join(out) == "a。b。c。"


def test_short_fragment_merged_into_next():
    out = audiobook.split_sentences("你好。好。今天天气不错。")
    # 首句 3 字 < _MIN_SENT，与第二句合并后才达阈值
    assert out == ["你好。好。", "今天天气不错。"]
    assert "".join(out) == "你好。好。今天天气不错。"


def test_trailing_fragment_appended_to_last_part():
    out = audiobook.split_sentences("今天天气不错。好")
    assert out == ["今天天气不错。好"]
    assert "".join(out) == "今天天气不错。好"


def test_english_sentences():
    text = "Hello world. This is a test!"
    out = audiobook.split_sentences(text)
    # 每段内部词间空格完整保留（供 TTS 使用）；段边界空格会被 strip
    assert out == ["Hello world.", "This is a test!"]


def test_no_content_loss_property():
    """性质测试：任意输入，切分后拼接必须还原原文。

    中文样例严格断言（标点后无空格，strip 无影响）；
    英文样例放宽为「非空白字符 / 单词完全保留」（英文标点后的边界空格会被
    strip，属既有设计，不影响逐句 TTS 的句内空格）。
    """
    strict_samples = [
        "第一句。第二句！第三句？",
        "。",
        "！！",
        "很短的句子",
        "a。b。c。d。",
        "句" * 200,  # 无标点超长硬切
        "，".join(["长" * 50] * 3),  # 逗号连接超长句
        "长" * 90 + "，短。尾" + "。" + "尾尾",
    ]
    for text in strict_samples:
        assert "".join(audiobook.split_sentences(text)) == text.replace("\r\n", "\n")

    def _chars(s: str) -> str:
        return "".join(s.split())

    # 英文：非空白字符必须完整保留（碎片合并会把短词拼成一句、单词边界自然
    # 变化，属 _MIN_SENT 设计使然，故这里只断言字符不丢；单词完整性由
    # test_english_sentences 用足够长的例句单独验证）
    for text in ["Hello. Hi! Bye? OK.", "Hello world. This is a test!"]:
        assert _chars("".join(audiobook.split_sentences(text))) == _chars(text)


def test_oversized_sentence_split_at_comma():
    # 超 80 字且带逗号：应在逗号处二次切分，且不丢内容
    text = "，".join(["长" * 50] * 3)
    out = audiobook.split_sentences(text)
    assert len(out) > 1
    assert "".join(out) == text


def test_oversized_no_punctuation_hard_cut():
    # 无任何标点：按 _MAX_SENT 硬切
    text = "字" * 200
    out = audiobook.split_sentences(text)
    assert out == ["字" * audiobook._MAX_SENT] * 2 + ["字" * 40]
    assert "".join(out) == text


# ---------------- parse_srt ----------------

SRT_SAMPLE = """1
00:00:01,000 --> 00:00:03,000
第一句

2
00:00:03,500 --> 00:00:05,000
第二句
"""


def test_looks_like_srt():
    assert audiobook.looks_like_srt(SRT_SAMPLE) is True
    assert audiobook.looks_like_srt("普通长文本 --> 但格式不对") is False
    assert audiobook.looks_like_srt("今天天气不错") is False


def test_parse_srt_basic():
    cues = audiobook.parse_srt(SRT_SAMPLE)
    assert cues == [
        (1.0, 3.0, "第一句"),
        (3.5, 5.0, "第二句"),
    ]


def test_parse_srt_filters_index_lines_and_sorts():
    # 序号行(纯数字)不进入文本；乱序输入按开始时间排序
    srt = """2
00:00:05,000 --> 00:00:06,000
B句

1
00:00:01,000 --> 00:00:02,000
A句
"""
    cues = audiobook.parse_srt(srt)
    assert [c[2] for c in cues] == ["A句", "B句"]


def test_parse_srt_invalid():
    assert audiobook.parse_srt("没有时间轴的内容") == []
    assert audiobook.parse_srt("") == []


def test_srt_seconds_comma_and_dot():
    assert audiobook._srt_seconds("0", "00", "01", "500") == 1.5
    assert audiobook._srt_seconds("1", "02", "03", "250") == 3723.25
