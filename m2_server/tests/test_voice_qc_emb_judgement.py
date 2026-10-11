# -*- coding: utf-8 -*-
"""变声验收声纹判据（`tools/voice_qc.py` 的 `emb_sim` 项）单测（2026-10-11）。

背景：2026-10-11 标定证明「自空间余弦 ≥0.95」在 RVC 这条链路上**误杀一半同人**，
而两把尺子的同人/异人簇都重叠 ⇒ 任何绝对阈值都有错判。验收改成**相对判据**
（输出离目标参考 vs 离输入者本人）；本文件钉住这套改动的三条不变式：

  1. **问不出来时是「未测」，不是「不合格」** —— 自转换（目标参考即输入本身、
     两侧同一段音）与 CAM++ 权重未缓存两种情形都必须回到 `None`，且总判定放行。
     方向写反的代价：用户被一个不存在的失败拦住，或未被测的样本被当成合格。
  2. **余量的符号就是判据**：目标更近 → 正（pass）；更像源说话人 → 负（fail）。
     两侧都要测 —— 只测一边会漏掉「把两个余弦相减写成相加」的实现。
  3. **短参考音只提示、不作废**（<5s 的提示文案 + 够长时为 None）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402
import voice_qc  # noqa: E402


def _item(passed, score: float = 25.0) -> dict:
    return {"value": 0.9, "pass": passed, "score": score, "detail": "x"}


# ---------------- 1. 未测 ≠ 不合格 ----------------

def test_self_convert_has_no_margin_and_says_why():
    """自转换：目标参考就是输入本身 —— 两侧同一段音，余量没有信息。"""
    margin, why = voice_qc._voice_margin(
        Path("out.wav"), Path("in.wav"), Path("in.wav"), self_convert=True)
    assert margin is None
    assert "自转换" in why


def test_missing_reference_is_untested_not_failed():
    margin, why = voice_qc._voice_margin(
        Path("out.wav"), Path("in.wav"), None, self_convert=False)
    assert margin is None and "自转换" in why


def test_untested_campplus_is_reported_as_untested(monkeypatch):
    """★ CAM++ 权重没缓存 ⇒ 如实「未测」——**不许联网下载**，也不许编数字。"""
    monkeypatch.setattr(voice_qc, "_cam_sim", lambda a, b: None)
    margin, why = voice_qc._voice_margin(
        Path("out.wav"), Path("in.wav"), Path("ref.wav"), self_convert=False)
    assert margin is None and "独立声纹不可用" in why


def test_overall_pass_lets_an_untested_item_through():
    """未测项（pass=None）不拦；只有明确 False 才拦 —— 两个方向都测。"""
    assert voice_qc._overall_pass({"emb_sim": _item(None), "f0_shift": _item(True)}) is True
    assert voice_qc._overall_pass({"emb_sim": _item(None), "f0_shift": _item(False)}) is False
    assert voice_qc._overall_pass({}) is False


def test_flag_prints_untested_instead_of_fail():
    assert (voice_qc._flag(True), voice_qc._flag(False), voice_qc._flag(None)) == \
        ("PASS", "FAIL", "未测")


# ---------------- 1b. 声纹项的形态（value/score 不动，pass 三态） ----------------

def test_emb_sim_item_keeps_the_self_space_value_and_awards_the_score(monkeypatch):
    """★ `value` 仍是**自空间余弦**、25 分制照旧折算 —— 只有 `pass` 换了判法。
    把 value 也换成 CAM++ 分，历史 JSON 就与新 JSON 不可比了（100 分制口径）。"""
    monkeypatch.setattr(voice_qc, "_emb", lambda p: [1.0, 0.0])
    monkeypatch.setattr(voice_qc, "_cam_sim",
                        lambda a, b: 0.7 if b.name == "ref.wav" else 0.1)
    item = voice_qc._emb_sim_item(Path("out.wav"), Path("in.wav"),
                                  Path("ref.wav"), self_convert=False)
    assert item["value"] == pytest.approx(1.0)
    assert item["pass"] is True
    assert item["margin"] == pytest.approx(0.6)
    assert item["score"] == pytest.approx(25.0)      # f = clamp01((1.0-0.90)/0.07) → 1.0
    assert "余量 +0.6000" in item["detail"]


def test_emb_sim_item_flags_an_output_that_still_sounds_like_the_source(monkeypatch):
    monkeypatch.setattr(voice_qc, "_emb", lambda p: [1.0, 0.0])
    monkeypatch.setattr(voice_qc, "_cam_sim",
                        lambda a, b: 0.2 if b.name == "ref.wav" else 0.6)
    item = voice_qc._emb_sim_item(Path("out.wav"), Path("in.wav"),
                                  Path("ref.wav"), self_convert=False)
    assert item["pass"] is False and item["margin"] < 0
    assert "更像源说话人" in item["detail"]


def test_emb_sim_item_is_untested_but_still_scored_when_the_margin_is_unavailable(monkeypatch):
    """未测**不额外扣分**：value/score 仍按自空间算（否则「未测」会被二次惩罚：
    既不计入判定、又白手扣掉 25 分，白分制就不再是 100 分制了）。"""
    monkeypatch.setattr(voice_qc, "_emb", lambda p: [0.6, 0.8])
    monkeypatch.setattr(voice_qc, "_cam_sim", lambda a, b: None)
    item = voice_qc._emb_sim_item(Path("out.wav"), Path("in.wav"),
                                  Path("ref.wav"), self_convert=False)
    assert item["pass"] is None and item["margin"] is None
    assert item["score"] == pytest.approx(25.0)
    assert "未测" in item["detail"]


# ---------------- 2. 余量的符号 ---------------- 

def test_positive_margin_when_the_output_is_closer_to_the_target(monkeypatch):
    sims = {("out.wav", "ref.wav"): 0.62, ("out.wav", "in.wav"): 0.11}
    monkeypatch.setattr(voice_qc, "_cam_sim",
                        lambda a, b: sims[(a.name, b.name)])
    margin, why = voice_qc._voice_margin(
        Path("out.wav"), Path("in.wav"), Path("ref.wav"), self_convert=False)
    assert margin == pytest.approx(0.51)
    assert "目标 0.6200 vs 输入者 0.1100" == why


def test_negative_margin_when_it_still_sounds_like_the_source(monkeypatch):
    """★ 反过来：更像源说话人 ⇒ 负余量（该判负）。少了这条，减号写成加号也不会红。"""
    sims = {("out.wav", "ref.wav"): 0.20, ("out.wav", "in.wav"): 0.55}
    monkeypatch.setattr(voice_qc, "_cam_sim",
                        lambda a, b: sims[(a.name, b.name)])
    margin, _ = voice_qc._voice_margin(
        Path("out.wav"), Path("in.wav"), Path("ref.wav"), self_convert=False)
    assert margin == pytest.approx(-0.35)


def test_reference_and_source_are_both_compared_against_the_output(monkeypatch):
    """比的是**同一份输出**对两侧（拿错成「参考 vs 输入」这对比就没意义了）。"""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(voice_qc, "_cam_sim",
                        lambda a, b: seen.append((a.name, b.name)) or 0.5)
    voice_qc._voice_margin(Path("out.wav"), Path("in.wav"), Path("ref.wav"),
                           self_convert=False)
    assert seen == [("out.wav", "ref.wav"), ("out.wav", "in.wav")]


# ---------------- 3. 短参考音只提示 ---------------- 

def test_short_reference_gets_a_warning_with_the_number():
    warn = voice_qc._short_ref_warning(4.2)     # 市场音色 preview 的真实时长
    assert warn and "4.2s" in warn and "5s" in warn


# ---------------- 4. 入库证据 ⟷ 上线判据（两把算盘必须打出同一个数） ----------------

def test_evidence_reproduces_the_shipped_criterion(monkeypatch):
    """★ 拿 committed 的 96 对实测分数，**用生产函数**（`voice_qc._voice_margin`）重算余量，
    必须与标定脚本从同一份证据算出的 `prod_margin` 逐份一致 ——
    一边是「报告里的建议」，一边是「上线的代码」，对不上就是有人改了其中一边没重标。

    模型在这里是替身：`_cam_sim` 被换成一个查表函数，值取自 committed `pairs.csv`
    （所以不需要 CAM++ 权重、不需要网络、不会因为模型版本变化而红）。
    """
    import csv
    import json

    import voice_qc_ruler_calib as calib

    ev = _ROOT / "docs/大创申报-2026-10/rvc-qc-判据标定"
    csv_path, json_path = ev / "pairs.csv", ev / "results.json"
    if not csv_path.exists() or not json_path.exists():
        pytest.skip(f"证据未入库：{ev}")

    rows = []
    with open(csv_path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            r["cam_cos"] = float(r["cam_cos"])
            r["qwen_cos"] = float(r["qwen_cos"])
            rows.append(r)
    # 名字就够用：输出的文件名唯一，参考音的「输入参考」与「目标参考」不同名
    table = {(Path(r["out_rel"]).name, Path(r["ref_rel"]).name): r["cam_cos"]
             for r in rows}
    monkeypatch.setattr(voice_qc, "_cam_sim", lambda a, b: table[(a.name, b.name)])

    committed = json.loads(json_path.read_text(encoding="utf-8"))["prod_margin"]
    by_out = {r["out_rel"]: r["margin"] for r in committed["rows"]}
    checked = 0
    for r in rows:
        if r["role"] != "same":
            continue
        margin, _ = voice_qc._voice_margin(
            Path(r["out_rel"]), Path("outputs/qc_calib/in") / f"{r['input']}.wav",
            Path(r["ref_rel"]), self_convert=False)
        assert margin == pytest.approx(by_out[r["out_rel"]], abs=1e-6), r["out_rel"]
        assert margin > 0, f"上线判据在这份输出上判负：{r['out_rel']}"
        checked += 1
    assert checked == committed["n_measured"] >= 6
    # 标定脚本那一份自己也算一遍 —— 两边都与证据对齐（双保险）
    assert calib.prod_margin(rows) == committed


def test_long_enough_reference_stays_silent():
    assert voice_qc._short_ref_warning(4.9) is not None   # 边界内侧
    assert voice_qc._short_ref_warning(5.0) is None       # 正好到下限就不啰嗦
    assert voice_qc._short_ref_warning(30.0) is None
