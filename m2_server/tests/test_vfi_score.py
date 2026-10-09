"""VFI/VAS 计分单测（大创减负实测层，2026-10-09）。

被测：`tools/vfi_score.py`。

★ 守的错法（都是"不报错、只污染结论"那种）：
  1. 因子结构：11/5/3、共 19 条 —— 结构被误改（比如把 19 条抄成 20 条）
     会让所有分变成另一个量表，必须有一条结构哨兵。
  2. **非法值绝不夹紧**：v07=7 若被夹到 4，因子 1 的和就悄悄变了 ——
     与 voicebank_eval.merge-listen 同一口径：剔除并上报。
  3. **因子间独立**：因子 1 有非法值时，因子 2/3 该行的分必须照常给出
     （丢整行 = 把被试的有效判断也删了）。
  4. 汇总只平均给得出分的行（先算每人的分，再对人数平均）。
  5. Excel 导出的 utf-8-sig（带 BOM）CSV 要能直接读 —— 回收表是真人填的。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pytest  # noqa: E402

import vfi_score as vf  # noqa: E402


def _row(**overrides) -> dict:
    """全 2 分的合法行（因子和 22/10/6），按需覆盖。"""
    base = {c: "2" for c in vf.ITEM_COLS}
    base["participant_id"] = "s01"
    base["vas_pre"] = "60"
    base["vas_post"] = "35"
    base.update(overrides)
    for k, v in list(base.items()):
        if v is None:
            base.pop(k)  # 模拟整列缺失（DictReader 缺列时键不存在）
    return base


class TestStructure:
    def test_factor_layout_is_11_5_3(self):
        sizes = {k: len(v) for k, v in vf.FACTORS.items()}
        assert sizes == {"factor1": 11, "factor2": 5, "factor3": 3}
        all_items = sorted(i for v in vf.FACTORS.values() for i in v)
        assert all_items == list(range(1, 20))  # 19 条全覆盖、不重叠

    def test_template_header(self, capsys):
        rc = vf.main(["--template"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == \
            ",".join(vf.TEMPLATE_HEADER)


class TestScoring:
    def test_valid_row_sums(self):
        s = vf.score_row(_row())
        assert s["factor1"] == 22
        assert s["factor2"] == 10
        assert s["factor3"] == 6
        assert s["notes"] == ""
        assert s["vas_pre"] == 60.0 and s["vas_post"] == 35.0

    def test_boundary_values_0_and_4(self):
        row = _row()
        for i in range(1, 12):
            row[f"v{i:02d}"] = "4" if i % 2 else "0"
        s = vf.score_row(row)
        assert s["factor1"] == sum(4 if i % 2 else 0 for i in range(1, 12))

    def test_invalid_value_not_clamped(self):
        s = vf.score_row(_row(v07="7"))
        assert s["factor1"] is None          # 所在因子不给分，绝不夹到 4
        assert "v07" in s["notes"]
        assert s["factor2"] == 10            # 因子间独立，别丢整行
        assert s["factor3"] == 6

    def test_missing_item_marks_factor_invalid(self):
        s = vf.score_row(_row(v12=None))
        assert s["factor2"] is None
        assert "v12" in s["notes"]
        assert s["factor1"] == 22

    def test_non_numeric_rejected(self):
        s = vf.score_row(_row(v19="总是"))
        assert s["factor3"] is None
        assert "v19" in s["notes"]

    def test_vas_out_of_range_reported_not_clamped(self):
        s = vf.score_row(_row(vas_pre="101"))
        assert s["vas_pre"] is None
        assert "vas_pre" in s["notes"]
        assert s["vas_post"] == 35.0

    def test_fractional_rating_rejected(self):
        s = vf.score_row(_row(v01="2.5"))
        assert s["factor1"] is None


class TestSummary:
    def test_mean_only_over_valid_rows(self):
        rows = [_row(), _row(participant_id="s02", v05="4"), _row(v07="7")]
        scores, summary = vf.score_csv(rows)
        assert summary["n"] == 3
        assert summary["factor1"]["n_valid"] == 2   # 第三行因子 1 无效，不计入
        # 第二行 v05=4 → 因子 1 = 24；均值 = (22 + 24) / 2
        assert summary["factor1"]["mean"] == 23.0
        assert summary["factor2"]["n_valid"] == 3
        assert summary["factor2"]["mean"] == 10.0  # v05 在因子 1，因子 2 三行都是 10
        assert summary["n_rows_with_notes"] == 1

    def test_all_invalid_factor_gives_none_not_zero(self):
        _, summary = vf.score_csv([_row(v07="7")])
        assert summary["factor1"]["mean"] is None
        assert summary["factor1"]["n_valid"] == 0


class TestCsv:
    def test_utf8_sig_roundtrip(self, tmp_path, capsys):
        p = tmp_path / "sheet.csv"
        p.write_text("﻿" + ",".join(vf.TEMPLATE_HEADER) + "\n"
                     "张三," + ",".join(["2"] * 19) + ",60,35\n",
                     encoding="utf-8")
        out = tmp_path / "scores.csv"
        rc = vf.main(["--in", str(p), "--out", str(out)])
        assert rc == 0
        assert "张三" in out.read_text(encoding="utf-8-sig")
        assert '"factor1"' in capsys.readouterr().out  # 汇总打印里有因子分

    def test_merge_multiple_sheets(self, tmp_path):
        p1 = tmp_path / "a.csv"
        p2 = tmp_path / "b.csv"
        header = ",".join(vf.TEMPLATE_HEADER)
        p1.write_text(header + "\n甲," + ",".join(["2"] * 19) + ",50,30\n",
                      encoding="utf-8")
        p2.write_text(header + "\n乙," + ",".join(["3"] * 19) + ",40,20\n",
                      encoding="utf-8")
        scores, summary = vf.score_csv(vf.read_csv(p1) + vf.read_csv(p2))
        assert summary["n"] == 2
        assert summary["factor1"]["mean"] == pytest.approx((22 + 33) / 2)
        assert scores[0]["participant_id"] == "甲"
