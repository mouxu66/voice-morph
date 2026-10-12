# -*- coding: utf-8 -*-
"""ASR 可懂度度量：字错率 CER（标点/空白无关）。

为什么不用 `ab_eval_multi.py` 里的字符集 Jaccard
------------------------------------------------
那个度量有两个致命缺陷，实测会把结论带偏（2026-10-12）：
1. **标点污染**：参考文本含 `，` `。`，而 whisper 输出常用 `,` 且句末不加标点。
   实测 `把步骤拆开,一步一步做,就不会乱`（多一个逗号）与
   `把步骤拆开,一步一步做就不会乱` **同为 0.786** —— 逗号增减完全淹没在噪声里，
   而这两个串的可懂度其实一样，度量却给出"加逗号扣分"的假信号。
2. **set() 吞重复**：`一步一步` 被算作一个字，于是"漏掉一个'一'"这种真实错误
   在集合里根本不可见；句子越长越失真。

CER 是 ASR 评测的通行口径，且对插入/删除/替换都敏感，故改用之。

用法
----
    from asr_cer import cer, normalize_zh
    cer("把步骤拆开，一步一步做就不会乱。", "把步就拆开一步一步做就不会乱")
"""
import re
import unicodedata

try:
    import zhconv
except ImportError:  # 本机 .venv 已装；venv312 也有
    zhconv = None

# 只保留中日韩统一表意文字 + 拉丁字母 + 数字；标点、空白、其他符号全部丢弃。
# 这样"标点风格差异"不再影响分数，而"字/词错误"仍然被计入。
_KEEP = re.compile(r"[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]")


def normalize_zh(text: str, to_simplified: bool = True) -> str:
    """去标点/空白/全角符号，可选繁转简；用于中文 ASR 误差度量。"""
    if not text:
        return ""
    if to_simplified and zhconv is not None:
        text = zhconv.convert(text, "zh-cn")
    # NFKC 把全角字母数字折半角，避免"ＡＢＣ vs ABC"被当成错
    text = unicodedata.normalize("NFKC", text)
    return _KEEP.sub("", text)


def _levenshtein(a: str, b: str) -> int:
    """O(len(a)*len(b)) 编辑距离；句长量级（<200 字）无需优化。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1,          # 删除
                           cur[j - 1] + 1,        # 插入
                           prev[j - 1] + (ca != cb)))  # 替换
        prev = cur
    return prev[-1]


def cer(reference: str, hypothesis: str, to_simplified: bool = True) -> float:
    """字错率 = 编辑距离 / 参考长度。0.0 = 全对；可 >1（大量插入）。"""
    r = normalize_zh(reference, to_simplified)
    h = normalize_zh(hypothesis, to_simplified)
    if not r:
        return float("nan")
    return _levenshtein(r, h) / len(r)


def accuracy(reference: str, hypothesis: str, to_simplified: bool = True) -> float:
    """可懂度 = 1 - CER（截断到 0）。与旧 Jaccard 同向、可直接对比大小。"""
    c = cer(reference, hypothesis, to_simplified)
    return float("nan") if c != c else max(0.0, 1.0 - c)
