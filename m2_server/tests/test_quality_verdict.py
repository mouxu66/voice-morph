"""P2-1 失败可诊断：把「跑完了但结果不对」翻译成「为什么 + 下一步」的判定测试。

为什么值得单独测（2026-09-25）：
    `quality_verdict.build_quality_verdict()` 是**纯函数**——不读盘、不起线程、
    不 import torch。纯函数的价值就在于可以用造出来的 summary 把每条分支都钉死，
    而不必真的跑一次 demucs。这个模块的三条纪律也正好逐条可测：

        · 绝不抛：传 None / 传空 dict / 传字符串都不能炸；
        · 按「能否挽救」排序：同时命中多个问题时，先报必须换素材的那个；
        · 去数字聚合：`信噪比 8dB` 与 `信噪比 9dB` 必须归到同一科目。

★ 最后一条测试是**跨端对账**：后端每个 VERDICT_* 常量，前端
  `web/src/lib/qualityVerdict.ts` 的 VERDICT_META 都必须有条目。
  漏掉一个不会报错，只会显示成兜底灰色（"说好的红叉呢"）—— 所以由测试钉死。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import quality_verdict as qv

WEB_LIB = Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "qualityVerdict.ts"


def _summary(total: int, ok: int, reasons: dict | None = None, grades: dict | None = None) -> dict:
    """造一份 `clip_qc.score_prefixes()` 形状的 summary（只填判定用得着的字段）。"""
    if grades is None:
        d = total - ok
        grades = {"A": ok // 2, "B": ok - ok // 2, "C": 0, "D": d}
    return {"materials": 1, "total": total, "ok": ok, "grades": grades, "top_reasons": reasons or {}}


# ---------------------------------------------------------------- 分支行为


def test_status_error_passes_backend_message_verbatim():
    """跑失败时**不加工**错误原文 —— `_pipeline_error_hint` 已经翻过一轮了。

    再翻译一次只会把它给的具体建议（"检查网络后重试"）磨成泛泛而谈。
    """
    hint = "demucs 没找到模型文件，检查网络后重试（首次运行要下载约 300MB）"
    v = qv.build_quality_verdict(None, status="error", error=hint)
    assert v["verdict"] == qv.VERDICT_EMPTY
    assert hint in v["detail"]
    assert v["actions"], "失败也要给下一步，否则用户只能盯着报错发呆"


def test_status_error_without_message_still_returns_a_verdict():
    """异常路径可能没有 error 文案 —— 不能因此返回空对象。"""
    v = qv.build_quality_verdict(None, status="error", error="")
    assert v["verdict"] == qv.VERDICT_EMPTY
    assert v["title"]
    assert v["detail"]


def test_zero_clips_reports_empty_material():
    """纯 BGM 切出 0 条：最该说清的是"这段素材没有人声"，而不是让用户看空列表。"""
    v = qv.build_quality_verdict(_summary(0, 0))
    assert v["verdict"] == qv.VERDICT_EMPTY
    assert "没有人声" in v["title"]
    assert v["ok_ratio"] == 0.0
    assert v["total"] == 0


def test_multi_speaker_dominating_reports_wrong_speaker():
    """说话人维度比质检分数更根本 —— 分数再高，唱的不是本人也没用。"""
    speaker = {
        "n_speakers": 3,
        "speakers": [
            {"id": 0, "label": "说话人 A", "ratio": 0.26, "is_main": True},
            {"id": 1, "label": "说话人 B", "ratio": 0.44, "is_main": False},
            {"id": 2, "label": "说话人 C", "ratio": 0.30, "is_main": False},
        ],
    }
    # 即便质检结果不错（ok_ratio 高于 MIN_OK_RATIO），也要先报说话人问题
    v = qv.build_quality_verdict(_summary(20, 16), speaker=speaker)
    assert v["verdict"] == qv.VERDICT_WRONG_SPEAKER
    assert "26%" in v["detail"], "要给出具体占比，用户才知道差多少"


def test_single_speaker_never_triggers_wrong_speaker():
    """只有一个人说话时，ratio 必然是 1.0 —— 不该走这条分支。

    边界：不能只看 ratio < 0.5，必须先确认 speakers 不止一位。
    """
    speaker = {"n_speakers": 1, "speakers": [{"id": 0, "label": "说话人 A", "ratio": 1.0, "is_main": True}]}
    v = qv.build_quality_verdict(_summary(10, 9), speaker=speaker)
    assert v["verdict"] == qv.VERDICT_OK


def test_speaker_block_is_ignored_when_absent():
    """没做说话人分离时（speaker=None）不能因此崩或误判。"""
    v = qv.build_quality_verdict(_summary(10, 9))
    assert v["verdict"] == qv.VERDICT_OK


@pytest.mark.parametrize(
    ("reason_text", "expected"),
    [
        ("信噪比 8dB 偏低（底噪或伴奏残留）", qv.VERDICT_NOISY),
        ("有效语音仅 12%，其余是静音或噪声", qv.VERDICT_NOISY),
        ("中段静音 2.1s，听感断裂", qv.VERDICT_NOISY),
        ("响度 -3.2dBFS 偏高，接近上限", qv.VERDICT_TOO_LOUD),
        ("存在削波（1.4% 采样点顶到满量程）", qv.VERDICT_TOO_LOUD),
        ("时长 1.2s 短于下限 2.5s", qv.VERDICT_TOO_SHORT),
    ],
)
def test_low_ok_ratio_routes_by_dominant_reason(reason_text, expected):
    """可用率低时，对策由**占多数的那类原因**决定，而不是一律"换素材"。"""
    v = qv.build_quality_verdict(_summary(40, 6, reasons={reason_text: 34}))
    assert v["verdict"] == expected
    assert v["detail"], "每条分支都要有解释，不能只给结论"


def test_unknown_reason_falls_back_honestly():
    """说不出具体科目时，诚实地说"原因比较分散"，不编一个假具体的原因。"""
    v = qv.build_quality_verdict(_summary(40, 6, reasons={"某种没见过的判废原因": 34}))
    assert v["verdict"] in (qv.VERDICT_NOISY, qv.VERDICT_UNKNOWN)
    assert "分散" in v["detail"] or "不可用" in v["title"]


def test_high_ok_ratio_is_ok():
    v = qv.build_quality_verdict(_summary(40, 36))
    assert v["verdict"] == qv.VERDICT_OK
    assert v["ok_ratio"] > qv.MIN_OK_RATIO
    assert v["actions"], "一切正常时也要给「下一步」，否则用户会在这步发呆"


def test_short_reference_is_reported_even_when_clips_are_fine():
    """切片可用但参考音只有 3 秒 —— 这是"怎么调都不像"的真因，必须说出来。"""
    v = qv.build_quality_verdict(_summary(40, 36), reference_s=3.0)
    assert v["verdict"] == qv.VERDICT_TOO_SHORT
    assert "3" in v["detail"]
    assert v["ok_ratio"] > qv.MIN_OK_RATIO, "切片本身没问题这一点要在数据上仍然成立"


def test_reference_length_only_checked_when_provided():
    """没给 reference_s 时不能瞎猜 —— 切片好就是好。"""
    assert qv.build_quality_verdict(_summary(40, 36))["verdict"] == qv.VERDICT_OK
    assert qv.build_quality_verdict(_summary(40, 36), reference_s=20.0)["verdict"] == qv.VERDICT_OK


# ---------------------------------------------------------------- 纪律：绝不抛


@pytest.mark.parametrize(
    "bad",
    [None, {}, "不是 dict", 123, {"total": "abc", "ok": None}, {"total": -5, "ok": 99,
     "grades": "不是 dict", "top_reasons": "也不是"}],
)
def test_never_raises_on_broken_input(bad):
    """诊断器自己崩掉比没有诊断器更糟 —— 用户会以为整个后端挂了。

    所以任何残缺输入都必须退化成一份「信息不足」的结论，而不是抛异常。
    （`pipeline_api._attach_verdict` 外还有一层 try 兜底，但那是最后一道保险，
    不该被这条测试豁免。）
    """
    v = qv.build_quality_verdict(bad)
    assert isinstance(v, dict)
    assert v["verdict"] in {getattr(qv, n) for n in dir(qv) if n.startswith("VERDICT_")}
    assert isinstance(v["actions"], list)
    assert isinstance(v["grades"], dict)


def test_negative_or_absurd_numbers_do_not_break_ratio():
    """ok > total 这种矛盾数据（轮询读到的中间态）不能让比例算出 NaN 或炸。

    这里不规定"应该夹到 1.0 还是保留 9.9" —— 只规定它是个能用的数：
    前端 `verdictSummary()` 会把它渲染成百分比，出 NaN 就是界面上写 "NaN%"。
    """
    v = qv.build_quality_verdict({"total": 10, "ok": 99})
    assert isinstance(v["ok_ratio"], float)
    assert v["ok_ratio"] == v["ok_ratio"], "不能是 NaN"
    assert v["total"] == 10
    assert v["ok"] == 99
    assert v["actions"], "数据再乱也要给出一份可读的结论，而不是空对象"


# ---------------------------------------------------------------- 聚合口径


def test_dominant_reason_strips_numbers_before_counting():
    """`信噪比 8dB` 与 `信噪比 9dB` 必须归到同一科目。

    否则两条各占 8 次，谁都不到多数，明明 16 条都是信噪比问题却报成"原因分散"
    —— `ft_corpus_qc._reason_key` 已经踩过一次这个坑。
    """
    reasons = {"信噪比 8dB 偏低": 8, "信噪比 9dB 偏低": 8, "响度 -3dBFS 偏高": 2}
    assert qv.dominant_reason(reasons) == qv.VERDICT_NOISY


def test_dominant_reason_accepts_list_of_single_reasons():
    """逐条分析时手上有的是 list（每条切片一个原因），不是聚合 dict。"""
    clips = ["信噪比 8dB 偏低", "信噪比 12dB 偏低", "时长 1.2s 短于下限"]
    assert qv.dominant_reason(clips) == qv.VERDICT_NOISY


def test_dominant_reason_returns_empty_for_no_known_reason():
    assert qv.dominant_reason({}) == ""
    assert qv.dominant_reason(None) == ""
    assert qv.dominant_reason(["完全看不懂的一句话"]) == ""


def test_dominant_reason_picks_the_most_severe_on_tie():
    """同票时取规则表里更靠前（更严重）的那个。

    「主说话人不一致」能一票否决，「时长不符」只是不够长 —— 数量打平时
    先说更严重的，早说早止损。
    """
    assert qv.dominant_reason({"声纹与主说话人不一致（0.31）": 3, "时长 1.2s 短于下限": 3}) == qv.VERDICT_WRONG_SPEAKER


def test_zero_division_is_impossible():
    """total=0 时 ok/total 会 ZeroDivisionError —— 这条专门钉住它。"""
    assert qv._ratio(5, 0) == 0.0
    v = qv.build_quality_verdict(_summary(0, 0))
    assert v["ok_ratio"] == 0.0


# ---------------------------------------------------------------- 跨端对账（★）


def _backend_codes() -> set[str]:
    """从**真实后端源码**里抠出所有 VERDICT_* 常量的值。

    不 import 取属性（那样会把"常量被删了"变成 AttributeError 而不是失败信息），
    直接读源码文本 —— 这样报错能直接告诉人"缺哪个码"。
    """
    text = (Path(qv.__file__) if qv.__file__ else WEB_LIB).read_text(encoding="utf-8")
    return set(re.findall(r'^VERDICT_[A-Z_]+ = "([a-z_]+)"$', text, re.MULTILINE))


def _frontend_codes() -> set[str]:
    text = WEB_LIB.read_text(encoding="utf-8")
    # VERDICT_META 的键：`  ok: { tone: "ok", ... }`
    block = text[text.index("VERDICT_META") : text.index("KNOWN_VERDICT_CODES")]
    return set(re.findall(r"^\s{2}(\w+):\s*\{", block, re.MULTILINE))


def test_frontend_lib_exists():
    assert WEB_LIB.is_file(), f"前端判定码表不见了：{WEB_LIB}"


def test_every_backend_code_has_frontend_meta():
    """★ 后端的每个判定码，前端配色表都必须有条目。

    这是本模块最重要的一条测试：漏掉一个**不会报错**，只会落到兜底分支
    显示成灰色 —— 用户看到"说好的红叉变灰了"，却没有任何地方提示这里缺了映射。
    """
    backend, frontend = _backend_codes(), _frontend_codes()
    assert backend, "没从后端源码里抽出任何判定码 —— 正则或常量命名变了"
    missing = backend - frontend
    assert not missing, (
        "后端有但前端 VERDICT_META 缺条目（会显示成兜底灰色）："
        f"{sorted(missing)}\n  web/src/lib/qualityVerdict.ts 的 VERDICT_META 要补齐"
    )

def test_frontend_has_no_ghost_codes():
    """反向：前端也不该有后端不存在的码（幽灵码不会被任何分支产出）。"""
    ghost = _frontend_codes() - _backend_codes()
    assert not ghost, f"前端有多余的判定码（后端永远不会产出）：{sorted(ghost)}"


def test_frontend_and_backend_code_sets_match_exactly():
    assert _backend_codes() == _frontend_codes()
