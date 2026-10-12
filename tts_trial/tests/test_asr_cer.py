# -*- coding: utf-8 -*-
"""asr_cer 度量测试。

这些用例钉死的是 2026-10-12 实测发现的两个度量缺陷：
标点污染与 set() 吞重复字 —— 它们曾让 index_rate 扫描得出错误结论。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from asr_cer import accuracy, cer, normalize_zh  # noqa: E402


class TestNormalize:
    def test_strips_chinese_and_ascii_punctuation(self):
        assert normalize_zh("把步骤拆开，一步一步做就不会乱。") == "把步骤拆开一步一步做就不会乱"
        assert normalize_zh("把步骤拆开,一步一步做就不会乱") == "把步骤拆开一步一步做就不会乱"

    def test_punctuation_style_is_irrelevant(self):
        """两种标点风格必须归一化到同一个串，否则 CER 会被标点差异污染。"""
        assert normalize_zh("注意这里的条件，符号写反了。") == \
               normalize_zh("注意这里的条件,符号写反了")

    def test_traditional_to_simplified(self):
        """.venv 的 whisper 常回繁体（实测 out_tts02 回「步驟拆開」）。"""
        if normalize_zh("步驟") == "步驟":  # zhconv 缺失时跳过（venv312 无 zhconv 的旧路径）
            pytest.skip("zhconv 不可用")
        assert normalize_zh("把步驟拆開") == "把步骤拆开"

    def test_whitespace_and_fullwidth(self):
        assert normalize_zh(" 一 二 三 ") == "一二三"
        assert normalize_zh("ＡＢＣ１２３") == "ABC123"

    def test_empty(self):
        assert normalize_zh("") == ""
        assert normalize_zh(None) == ""


class TestCER:
    def test_perfect_match_is_zero(self):
        assert cer("这一节我们讲三个概念，先看第一个。",
                   "这一节我们讲三个概念,先看第一个") == 0.0

    def test_one_wrong_char(self):
        ref = "注意这里的条件，符号写反了结论就完全不同。"
        hyp = "注意这里的条件，符号写反了截认就完全不同"
        # 「结论」→「截认」：2 字替换；分母是去标点后的参考长度（19 字）
        assert normalize_zh(ref) == "注意这里的条件符号写反了结论就完全不同"
        assert cer(ref, hyp) == pytest.approx(2 / len(normalize_zh(ref)), abs=1e-6)

    def test_deletion_of_repeated_char_is_counted(self):
        """set() 度量对这里会给 0 分差异；CER 必须扣分。"""
        assert cer("一步一步做", "一步做") > 0

    def test_insertion_penalized(self):
        assert cer("把步骤拆开", "把步骤拆开拆开") > 0

    def test_empty_hypothesis_is_total_loss(self):
        assert cer("一二三四", "") == 1.0

    def test_empty_reference_is_nan(self):
        assert cer("", "任意") != cer("", "任意")  # NaN 自比不相等

    def test_can_exceed_one_on_insertions(self):
        assert cer("一", "一二三四五") > 1.0


class TestAccuracy:
    def test_accuracy_is_one_minus_cer(self):
        ref, hyp = "把步骤拆开，一步一步做就不会乱。", "把步就拆开一步一步做就不会乱"
        assert accuracy(ref, hyp) == pytest.approx(1.0 - cer(ref, hyp))

    def test_accuracy_floors_at_zero(self):
        assert accuracy("一", "一二三四五") == 0.0

    def test_index_rate_regression(self):
        """回归：index_rate 0 → 1 时 whisper 的三条转写，CER 口径下的真实差距。

        这组数据来自 2026-10-12 实测，用来防止度量退化后又把
        「查表更准但可懂度略降」误读成「查表显著更差」。
        """
        pairs = [
            ("这一节我们讲三个概念，先看第一个。", "这一节我们讲三个概念,先看第一个"),
            ("把步骤拆开，一步一步做就不会乱。", "把步骤拆开一步一步做就不会乱"),
            ("注意这里的条件，符号写反了结论就完全不同。", "注意这里的条件符号写反了结论就完全不同"),
        ]
        for ref, hyp in pairs:
            assert accuracy(ref, hyp) == pytest.approx(1.0, abs=1e-6)
