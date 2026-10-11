# -*- coding: utf-8 -*-
"""RVC 变声验收判据标定（`tools/voice_qc_ruler_calib.py`）单测（2026-10-11）。

★ 守的是「**换不换判据不许靠人拍**」这三件事：
  1. **配对标签跟着说话人走**，不跟着文件路径走 —— 标签写错整份标定都是假的，
     而它**不会报错**（AUC 照样是 1.0，只是意义全反），所以必须直接断标签规则。
  2. **`decide()` 的三个分支与边界**（keep / fragile / switch）：这是「换不换」的
     唯一依据，谁改公式/改余量阈值都得先过这张表 —— 尤其是**误杀**与**误收**
     两个方向各要有一条（只测一边会漏掉方向反了的实现）。
  3. **入库证据可复现**：拿 committed `pairs.csv` 现算 `analyze()`，判定、余量、
     0.95 通过率必须与入库 `results.json` 一致 —— 改公式/改判据而没重标，这里就红
     （与 pilotC 的重放测试同一条纪律：证据与代码不许脱钩）。
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402
import voice_qc_ruler_calib as calib  # noqa: E402

EVIDENCE_DIR = _ROOT / "docs/大创申报-2026-10/rvc-qc-判据标定"


# ---------------- 1. 配对标签：跟说话人，不跟路径 ----------------

def _refs(pool: dict[str, tuple[str, str]]) -> dict[str, dict]:
    """`{ref_id: (speaker, kind)}` → 用到的只有这两个字段（打分阶段另有数据）。"""
    # 路径必须落在 ROOT 之下（`build_pairs` 会 `relative_to(ROOT)`），文件不必存在
    return {k: {"path": calib.ROOT / "fake" / f"{k}.wav", "speaker": sp, "kind": kind}
            for k, (sp, kind) in pool.items()}


def test_same_pairs_only_when_speaker_matches_and_ref_is_target():
    refs = _refs({
        "kangaroo_target": ("kangaroo", "target"),
        "other_target": ("other", "target"),
        "other_input": ("other", "input"),
    })
    outputs = [{"exp": "kangaroo_v2", "speaker": "kangaroo", "input": "aishell3",
                "ref": "x", "path": "out/kangaroo_v2__aishell3.wav"}]
    pairs = calib.build_pairs(outputs, refs)
    same = [p for p in pairs if p["role"] == "same"]
    cross = [p for p in pairs if p["role"] == "cross"]
    assert [p["ref_id"] for p in same] == ["kangaroo_target"], "同人只能是自己的目标参考"
    assert {p["ref_id"] for p in cross} == {"other_target", "other_input"}
    # 说话人标签两列都写进去了 —— 后人看数据不用反查路径
    assert all(p["target_speaker"] == p["ref_speaker"] for p in same)
    assert all(p["target_speaker"] != p["ref_speaker"] for p in cross)


def test_target_speaker_cannot_get_a_same_pair_from_an_input_ref():
    """★ 同一说话人若也当过输入，同人标签必须优先给 target —— 否则同人簇混进
    一段被截过的输入参考，同人分被系统性拉低（标定会白做）。"""
    refs = _refs({
        "kangaroo_target": ("kangaroo", "target"),
        "kangaroo_input": ("kangaroo", "input"),
    })
    outputs = [{"exp": "kangaroo_v2_40k", "speaker": "kangaroo", "input": "aishell3",
                "ref": "x", "path": "out/kangaroo_v2_40k__aishell3.wav"}]
    pairs = calib.build_pairs(outputs, refs)
    assert [p["ref_id"] for p in pairs if p["role"] == "same"] == ["kangaroo_target"]
    assert "kangaroo_input" not in {p["ref_id"] for p in pairs if p["role"] == "same"}


def test_every_output_yields_exactly_one_same_pair():
    refs = _refs({**{"a_target": ("A", "target"), "b_target": ("B", "target")},
                  **{f"x{i}": (f"x{i}", "cross_only") for i in range(3)}})
    outputs = [
        {"exp": "mA", "speaker": "A", "input": "x0", "ref": "r", "path": f"out/mA__x{i}.wav"}
        for i in range(3)
    ] + [
        {"exp": "mB", "speaker": "B", "input": "x1", "ref": "r", "path": f"out/mB__x{i}.wav"}
        for i in range(3)
    ]
    pairs = calib.build_pairs(outputs, refs)
    same = [p for p in pairs if p["role"] == "same"]
    assert len(same) == len(outputs), "每份输出必须恰好有一条同人对"
    assert len({p["out_rel"] for p in same}) == len(outputs)


# ---------------- 2. decide()：三分支 + 两个方向 ----------------

def test_decide_keeps_a_healthy_margin_on_both_sides():
    """两侧余量都 ≥0.02 → keep（现役 0.95 站得住，不改判据）。"""
    d = calib.decide(same_min=0.97, cross_max=0.90)
    assert d["verdict"] == "keep"
    assert d["margin_low"] == pytest.approx(0.02)
    assert d["margin_high"] == pytest.approx(0.05)


def test_decide_flags_fragile_when_any_side_has_no_headroom():
    """能用但没有余量 → fragile（不改阈值，但必须把 audit 加进回归）。"""
    assert calib.decide(same_min=0.96, cross_max=0.90)["verdict"] == "fragile"   # 同人贴线
    assert calib.decide(same_min=0.97, cross_max=0.94)["verdict"] == "fragile"   # 异人贴线
    # 正好压线也算 fragile（余量 0 时一改就崩），但**不是** switch（还没误杀）
    assert calib.decide(same_min=0.95, cross_max=0.94)["verdict"] == "fragile"


def test_decide_switches_in_both_directions():
    """★ 两个方向都要测：只测一边会漏掉「把公式写反」的实现。"""
    killed = calib.decide(same_min=0.94, cross_max=0.90)   # 同人被误杀
    assert killed["verdict"] == "switch" and killed["margin_low"] < 0
    leaked = calib.decide(same_min=0.97, cross_max=0.96)   # 异人被放进来
    assert leaked["verdict"] == "switch" and leaked["margin_high"] < 0


def test_decide_reports_the_floor_it_was_asked_about():
    d = calib.decide(same_min=0.70, cross_max=0.20, floor=0.53)
    assert d["floor"] == 0.53 and d["verdict"] == "keep"


# ---------------- 3. 入库证据可复现（证据与代码不许脱钩） ----------------

def _load_evidence() -> tuple[list[dict], dict]:
    pairs_csv = EVIDENCE_DIR / "pairs.csv"
    results_json = EVIDENCE_DIR / "results.json"
    if not pairs_csv.exists() or not results_json.exists():
        pytest.skip(f"证据未入库：{EVIDENCE_DIR}")
    rows = []
    with open(pairs_csv, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            r["qwen_cos"] = float(r["qwen_cos"])
            r["cam_cos"] = float(r["cam_cos"])
            rows.append(r)
    return rows, json.loads(results_json.read_text(encoding="utf-8"))


def test_committed_pairs_reproduce_the_committed_decision():
    rows, committed = _load_evidence()
    assert len(rows) == committed["n_same"] + committed["n_cross"], "配对数与结论脱钩"
    fresh = calib.analyze(rows)
    assert fresh["legacy_decision"] == committed["legacy_decision"], \
        "判定或余量算不出入库那份 —— 改了代码没重标，或改了证据没改结论"
    for key in ("qwen_cos", "cam_cos"):
        assert fresh["rulers"][key]["same"] == committed["rulers"][key]["same"], key
        assert fresh["rulers"][key]["cross"] == committed["rulers"][key]["cross"], key
        assert fresh["rulers"][key]["same_pass_at_legacy"] == \
            committed["rulers"][key]["same_pass_at_legacy"], key
        assert fresh["rulers"][key]["cross_pass_at_legacy"] == \
            committed["rulers"][key]["cross_pass_at_legacy"], key
    # 相对排序这一节也是结论的一部分，同样不许与代码脱钩
    assert fresh["rank"] == committed["rank"], \
        "排序分析算不出入库那份 —— 改了 rank 逻辑没重标，或改了证据没改结论"


# ---------------- 4. rank_analysis()：绝对阈值之外的相对判据 ----------------

def _row(exp: str, inp: str, ref_id: str, role: str, qwen: float, cam: float) -> dict:
    return {"exp": exp, "input": inp, "ref_id": ref_id, "role": role,
            "qwen_cos": qwen, "cam_cos": cam}


def test_rank_analysis_credits_a_target_that_beats_every_rival():
    rows = [
        _row("m", "in", "target", "same", 0.95, 0.70),
        _row("m", "in", "rival", "cross", 0.94, 0.30),
        _row("m", "in", "input_speaker", "cross", 0.93, 0.20),
    ]
    r = calib.rank_analysis(rows)
    assert r["n_outputs"] == 1
    assert (r["qwen_rank1_rate"], r["cam_rank1_rate"]) == (1.0, 1.0)
    assert r["cam_margin_min"] == pytest.approx(0.40)
    assert r["rows"][0]["refs"] == 3


def test_rank_analysis_exposes_an_overlapping_ruler_the_absolute_gate_misses():
    """★ 这是本标定的核心情形：绝对刻度重叠（目标 0.30 与竞争者 0.55 已经错位），
    但**排序仍然看得见**这个错位 —— rank>1、余量为负。若这个函数只会数「同人分
    比异人分高」，这条最关键的发现就会被悄悄埋掉。"""
    rows = [
        _row("m", "in", "target", "same", 0.30, 0.30),
        _row("m", "in", "rival", "cross", 0.55, 0.60),
    ]
    r = calib.rank_analysis(rows)
    assert r["cam_rank1_rate"] == 0.0 and r["qwen_rank1_rate"] == 0.0
    assert r["cam_margin_min"] < 0 and r["qwen_margin_min"] < 0
    assert r["rows"][0]["cam_rank"] == 2


def test_rank_analysis_refuses_rows_that_cannot_be_paired_back():
    """只有分数、没配对键（如裁过的证据副本）时如实报错 —— 不硬凑一个 rank 出来。"""
    r = calib.rank_analysis([{"role": "same", "qwen_cos": 0.9, "cam_cos": 0.5}])
    assert "error" in r and "rows" not in r


def test_committed_evidence_is_actually_decisive():
    """证据本身必须有牙齿：n 太小、或两把尺子全没测到，标定等于没做。"""
    rows, committed = _load_evidence()
    assert committed["n_same"] >= 6, "同人对太少，结论没有统计意义"
    assert committed["n_cross"] >= 24, "异人对太少，误收率测不出来"
    for key in ("qwen_cos", "cam_cos"):
        assert committed["rulers"][key]["auc"] is not None, f"{key} 没算出 AUC"
    assert committed["legacy_decision"]["verdict"] in ("keep", "fragile", "switch")
    # 「若改双门槛会是哪三个数」也必须入库 —— 但**推不出来时必须如实报错**：
    # 两簇重叠时硬给一个阈值比报错危险得多（任何阈值都有错判），所以两种形态
    # 只能有一种：要么有序的 pass/warn，要么带原因的 error。
    for key in ("cam_cos", "qwen_cos"):
        d = committed["dual_derived"][key]
        if "error" in d:
            assert "pass" not in d, f"{key} 推不出来却编了阈值"
        else:
            assert d["pass"] > d["warn"], f"{key} 推导出来的双门槛必须有序"
    assert committed["dual_audit"]["n_same"] == committed["n_same"]
    # 相对排序也必须有牙齿：本批的决定性证据就是「独立声纹排序分得开」
    assert committed["rank"]["n_outputs"] > 0, "排序分析没入库"
    assert committed["rank"]["cam_rank1_rate"] == 1.0, \
        "独立声纹在这条链路上排不出第 1 —— 那它也没法当相对判据"
