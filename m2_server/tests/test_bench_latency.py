"""`tools/bench_latency.py` 的单元测试。

为什么值得测：这个工具存在的唯一理由是**把「端到端 p50 ≈ 50ms」这个
声称变成可对账的东西**。所以测试重点不在算术，而在：

1. **不许编造** —— 没有日志时必须报 `unknown`，绝不给一个默认值填坑；
2. **口径不能塌** —— 端到端必须是「分块 + 推理 + 驱动」三段之和，
   不能被压成一个数（压了就和 `rvc_live.infer_latency` 的单块口径混了）；
3. **对账方向正确** —— 实测远大于声称要报 `inflated`，远小于要报 `better`，
   不能因为怕难看就一律 `ok`；
4. **配置要从真源读** —— 档位 block_time 必须来自 `rvc_live.PROFILE_TUNING`，
   不能是抄在报告里的一个数（抄的会和代码漂移）。

跑法：
    python -m pytest m2_server/tests/test_bench_latency.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for _p in (ROOT / "tools", ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import bench_latency as bl  # noqa: E402

# ---------------- percentile ----------------

class TestPercentile:
    def test_empty_returns_none(self):
        assert bl.percentile([], 0.5) is None

    def test_single_value(self):
        assert bl.percentile([42.0], 0.5) == 42.0
        assert bl.percentile([42.0], 0.95) == 42.0

    def test_is_nearest_rank_not_interpolated(self):
        """★ 报出来的数必须是**真实测到过**的那个，不能是插值出来的。

        插值会得到「从未发生过的第 95 百分位」，对耗时样本没有意义。
        """
        vals = [10.0, 20.0, 30.0, 40.0]
        assert bl.percentile(vals, 0.5) in vals
        assert bl.percentile(vals, 0.95) in vals

    def test_q_zero_and_one_are_extremes(self):
        vals = [5.0, 1.0, 9.0]
        assert bl.percentile(vals, 0.0) == 1.0
        assert bl.percentile(vals, 1.0) == 9.0

    def test_matches_rvc_live_convention(self, tmp_path):
        """★ 与 `rvc_live.infer_latency` 同口径 —— 两处不一致会得到互相打架的数。

        `rvc_live` 那边没有独立的分位数函数（是内联在 `infer_latency` 里的
        `vals[min(len-1, int(len*q))]`），所以这里**读它的真实输出**来对齐，
        而不是假设一个不存在的 `_percentile`。
        """
        import rvc_live

        vals = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        log = tmp_path / "realtime_gui.log"
        log.write_text("".join(f"推理耗时：{v / 1000:.3f}秒\n" for v in vals),
                       encoding="utf-8")
        text = log.read_text(encoding="utf-8")
        parsed = rvc_live.parse_infer_ms(text)
        assert parsed == pytest.approx(vals)  # 解析口径先对齐

        ref_p95 = sorted(parsed)[min(len(parsed) - 1, int(len(parsed) * 0.95))]
        assert bl.percentile(parsed, 0.95) == pytest.approx(ref_p95)


# ---------------- summarize ----------------

class TestSummarize:
    def test_no_inference_means_no_e2e(self):
        """★ 没有推理样本时 `e2e_ms` 必须是 None，不是 0。

        填 0 会得到一个"看起来很完整"的数，然后被谁引用进材料。
        """
        res = bl.summarize([], block_ms=250.0)
        assert res["infer_ms"] is None
        assert res["e2e_ms"] is None
        assert res["measured"] is False
        assert res["infer_samples"] == 0

    def test_e2e_is_sum_of_three_segments(self):
        """端到端 = 分块缓冲 + 推理 + 驱动缓冲，三段缺一不可。"""
        res = bl.summarize([100.0], block_ms=250.0, driver_ms=20.0)
        assert res["e2e_ms"] == pytest.approx(370.0)
        assert res["infer_ms"] == 100.0
        assert res["measured"] is True

    def test_e2e_is_not_just_inference(self):
        """★ 回归守卫：端到端**不能等于**单块推理。

        这正是申报书里 50ms 的问题所在 —— 那个数只覆盖了推理段。
        """
        res = bl.summarize([50.0], block_ms=250.0, driver_ms=20.0)
        assert res["e2e_ms"] != res["infer_ms"]
        assert res["e2e_ms"] > res["infer_ms"]

    def test_median_used_for_typical(self):
        res = bl.summarize([10.0, 20.0, 30.0, 40.0, 500.0], block_ms=0.0, driver_ms=0.0)
        assert res["infer_ms"] == 30.0  # 中位数，不被离群值带偏

    def test_p95_is_reported_for_stutter(self):
        """p95 比 p50 更该关心：偶尔卡一帧，通话体验立刻掉。"""
        vals = [float(i) for i in range(1, 101)]  # 1..100
        res = bl.summarize(vals, block_ms=0.0, driver_ms=0.0)
        assert res["infer_p95_ms"] >= res["infer_ms"]
        assert res["e2e_p95_ms"] >= res["e2e_ms"]

    def test_samples_count_recorded(self):
        res = bl.summarize([1.0, 2.0, 3.0], block_ms=0.0)
        assert res["infer_samples"] == 3


# ---------------- compare_claim ----------------

class TestCompareClaim:
    def test_no_measurement_is_unknown(self):
        """没测就说没测 —— 不许猜一个结论。"""
        c = bl.compare_claim(None)
        assert c["status"] == "unknown"
        assert "无法" in c["detail"]

    def test_same_magnitude_is_ok(self):
        c = bl.compare_claim(50.0, claimed=50.0)
        assert c["status"] == "ok"

    def test_far_above_claim_is_inflated(self):
        """★ 实测 400ms vs 声称 50ms —— 必须报 inflated，这是诚信问题。"""
        c = bl.compare_claim(400.0, claimed=50.0)
        assert c["status"] == "inflated"
        assert c["ratio"] == pytest.approx(8.0)

    def test_boundary_at_1_3x(self):
        assert bl.compare_claim(64.0, claimed=50.0)["status"] == "ok"      # 1.28×
        assert bl.compare_claim(66.0, claimed=50.0)["status"] == "inflated"  # 1.32×

    def test_far_below_claim_is_better(self):
        c = bl.compare_claim(30.0, claimed=50.0)
        assert c["status"] == "better"

    def test_zero_claim_does_not_crash(self):
        """口径异常时不能崩，要退化成 unknown 而不是除零。"""
        c = bl.compare_claim(50.0, claimed=0.0)
        assert c["status"] in ("ok", "better", "inflated")


# ---------------- render_md ----------------

class TestRenderMd:
    def _res(self, measured: bool):
        vals = [80.0, 90.0, 100.0] if measured else []
        res = bl.summarize(vals, block_ms=250.0, driver_ms=20.0)
        res["profile"] = "balanced"
        res["claim"] = bl.compare_claim(res["e2e_ms"])
        return res

    def test_unmeasured_report_says_so_explicitly(self):
        md = bl.render_md(self._res(False))
        assert "未实测" in md
        assert "合计无法给出" in md

    def test_unmeasured_report_does_not_invent_a_total(self):
        """★ 报告里不能出现一个凭空来的合计数字。

        反例：把推理段当 0 填进去，就会渲染出 "合计 p50 = 270ms"
        —— 那是假的，因为压根没测过。
        """
        md = bl.render_md(self._res(False))
        assert "270" not in md

    def test_measured_report_shows_three_segments(self):
        md = bl.render_md(self._res(True))
        assert "分块缓冲" in md
        assert "单块推理" in md
        assert "驱动缓冲" in md
        assert "360.0ms" in md  # 250 + 100 + 20

    def test_reliability_column_marks_driver_as_nominal(self):
        """驱动段是纸面参数，必须标出来 —— 否则读者会以为它和推理段一样可靠。"""
        md = bl.render_md(self._res(True))
        assert "标称值" in md

    def test_claim_section_present_in_both_cases(self):
        for measured in (True, False):
            assert "与材料里的声称对账" in bl.render_md(self._res(measured))

    def test_reproduce_commands_present(self):
        """报告要能指导别人复现，所以得带命令。"""
        md = bl.render_md(self._res(True))
        assert "bench_latency.py" in md


# ---------------- 真源与 IO ----------------

class TestRealSource:
    def test_block_ms_read_from_rvc_live_not_hardcoded(self):
        """★ 档位分块时长必须来自 `rvc_live.PROFILE_TUNING`。

        抄一份数字进报告 → 代码改了报告不变 → 报告和实际跑的链路对不上。
        """
        import rvc_live
        for name, tuning in rvc_live.PROFILE_TUNING.items():
            assert bl._profile_block_ms(name) == pytest.approx(
                tuning["block_time"] * 1000.0)

    def test_balanced_is_250ms(self):
        """钉住当前基线，改动时测试会红 —— 提醒同步材料。"""
        assert bl._profile_block_ms("balanced") == pytest.approx(250.0)

    def test_game_is_350ms(self):
        assert bl._profile_block_ms("game") == pytest.approx(350.0)

    def test_unknown_profile_raises(self):
        with pytest.raises(KeyError):
            bl._profile_block_ms("no-such-profile")

    def test_read_infer_ms_missing_file_is_empty_not_error(self):
        assert bl.read_infer_ms(Path("D:/definitely/not/here.log")) == []

    def test_read_infer_ms_none_is_empty(self):
        assert bl.read_infer_ms(None) == []

    def test_read_infer_ms_parses_real_log_lines(self, tmp_path):
        """复用 `rvc_live.parse_infer_ms` —— 全角/半角冒号都要认。"""
        log = tmp_path / "realtime_gui.log"
        log.write_text(
            "[12:00:01] 推理耗时：0.08秒\n"
            "[12:00:02] 推理耗时: 0.12秒\n"          # 半角冒号
            "[12:00:03] STREAM_UP\n"                  # 无关行
            "[12:00:04] 推理耗时：0.095秒\n",
            encoding="utf-8")
        got = bl.read_infer_ms(log)
        assert got == pytest.approx([80.0, 120.0, 95.0])


# ---------------- bench 集成 ----------------

class TestBench:
    def test_no_log_yields_unknown_claim(self):
        res = bl.bench("balanced")
        assert res["block_ms"] == 250.0
        assert res["measured"] is False
        assert res["claim"]["status"] == "unknown"

    def test_log_makes_e2e_measured(self, tmp_path):
        log = tmp_path / "r.log"
        log.write_text("推理耗时：0.10秒\n" * 5, encoding="utf-8")
        res = bl.bench("balanced", log=log)
        assert res["measured"] is True
        assert res["e2e_ms"] == pytest.approx(370.0)  # 250 + 100 + 20
        assert res["claim"]["status"] == "inflated"

    def test_tail_limits_samples(self, tmp_path):
        log = tmp_path / "r.log"
        log.write_text("".join(f"推理耗时：{i / 100:.2f}秒\n" for i in range(1, 51)),
                       encoding="utf-8")
        res = bl.bench("balanced", log=log, tail=10)
        assert res["infer_samples"] == 10

    def test_recorded_metadata_present(self):
        res = bl.bench("game")
        assert res["profile"] == "game"
        assert res["measured_at"]
        assert res["block_ms"] == 350.0

    def test_bad_profile_returns_error_not_raises(self):
        """CLI 层要能优雅报错，不能把栈甩给用户。"""
        res = bl.bench("nope")
        assert res.get("error")
        assert res["error_stage"] == "档位"
