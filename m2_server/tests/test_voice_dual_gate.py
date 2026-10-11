# -*- coding: utf-8 -*-
"""双门槛声纹门禁单测（2026-10-11）。

被测：`tools/voice_dual_gate.py`（判据，纯函数）+ `tools/voice_report.similarity_lamp`
（把判词翻译成灯）+ `m2_server/voicepreserve.py`（随访门禁的取数口）。

★ 这个文件守的是「**门禁到底有没有在干活**」——判据类的东西最危险的失效方式
是**看着在跑、其实空转**，所以这里的断言都带「负例」：

  1. **真数据重放**：直接读入库的 `dual_embed/pairs.csv`（144 对实测分数），
     门禁必须 48/48 放行同人、0/96 放行异人。阈值松一格或紧一格，这里就红。
  2. **空转自检**：故意把阈值放宽/收紧，`audit()` 必须**报出来**
     （若不报，说明第 1 条其实什么也没测）。
  3. **标定不漂**：`MEASURED` 里的簇界必须与 pairs.csv 现算的一致 —— 抄错一位
     数、或改了证据文件却没改常量，直接红。
  4. **未测 ≠ 不合格**：主判拿不到时分档必须是 `unknown` 而**不是** `reject`
     （混在一起，断网的老师会看到「你的录音不是本人」）。
  5. **自证尺子不能单用**：把「0.95 单尺阈值离异人上界只有 0.0034」这个事实
     写成断言 —— 将来谁想改回单尺，先过这一条。
"""

import csv
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import voice_dual_gate as dg  # noqa: E402
import voice_report as vr  # noqa: E402
import voicepreserve as vp  # noqa: E402

_PAIRS = _ROOT / dg.EVIDENCE_DIR / "pairs.csv"


# ---------------- 夹具 ----------------


@pytest.fixture(autouse=True)
def iso_media(tmp_path, monkeypatch):
    """档案库指到 tmp —— 与 `test_voicepreserve.py` 同一理由：别写进真实 media/。"""
    monkeypatch.setattr(vp.cfg, "MEDIA_DIR", tmp_path / "media")
    return tmp_path / "media"


@pytest.fixture(scope="module")
def measured_pairs() -> list[dict]:
    assert _PAIRS.exists(), f"标定数据不在（证据被挪走了？）：{_PAIRS}"
    return dg.load_pairs(_PAIRS)


def _wav(path: Path, seconds: float = 1.0, sr: int = 22050, f0: float = 150.0) -> Path:
    """元音状合成 wav（与 `test_voicepreserve.py::_wav` 同款：有浊音段，指标模块接受）。"""
    import soundfile as sf

    t = np.arange(int(sr * seconds)) / sr
    y = np.zeros_like(t)
    for k in range(1, 20):
        if k * f0 > sr / 2:
            break
        y += 10 ** (-6.0 * (k - 1) / 20.0) * np.sin(2 * np.pi * k * f0 * t)
    peak = float(np.abs(y).max())
    y = (y / peak * 0.5) if peak > 0 else y
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), y, sr)
    return path


def _stub_convert(monkeypatch):
    def _fake(src, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(src).read_bytes())
        return True, ""

    monkeypatch.setattr(vp, "_to_wav", _fake)


def _stub_metrics(monkeypatch):
    monkeypatch.setattr(vp, "_metrics_for", lambda wav: {
        "metrics": {"hnr": 18.0, "h1_h2": 4.0, "spectral_tilt": -3.0,
                    "jitter_local": 0.01, "shimmer_local": 0.05,
                    "f0_median": 160.0, "voiced_ratio": 0.8, "duration_s": 12.0},
        "lamps": {"lamps": [{"key": "similarity", "label": "相似度", "value": None,
                             "lamp": None, "detail": "未测", "sources": []},
                            {"key": "naturalness", "label": "自然度", "value": 18.0,
                             "lamp": "green", "detail": "HNR 18.0 dB", "sources": ["hnr"]},
                            {"key": "strain", "label": "夹嗓子风险", "value": 4.0,
                             "lamp": "green", "detail": "H1-H2 +4.0 dB", "sources": ["h1_h2"]}],
                  "verdict": "可用", "tested_count": 2},
        "error": "",
    })


def _profile_with_ref(iso_media, monkeypatch) -> str:
    _stub_convert(monkeypatch)
    pid = vp.create_profile("王老师")["id"]
    vp.add_reference(pid, _wav(iso_media / "ref.wav", seconds=2.0), vp.REF_DURATIONS[-1])
    return pid


# ---------------- 1. 阈值：从实测分布推导 ----------------


class TestThresholdDerivation:
    def test_thresholds_sit_between_the_two_clusters(self):
        assert dg.check_thresholds() == [], "阈值没落在两簇之间（或与实测推导不一致）"

    def test_frozen_defaults_equal_the_derivation(self):
        """冻结值必须是推导结果 —— 手改常量会在这里现形。"""
        cam = dg.derive(dg.MEASURED["cam_cos"]["same_min"],
                        dg.MEASURED["cam_cos"]["cross_max"])
        qwen = dg.derive(dg.MEASURED["qwen_cos"]["same_min"],
                         dg.MEASURED["qwen_cos"]["cross_max"])
        assert (dg.DUAL.cam_pass, dg.DUAL.cam_warn) == (cam["pass"], cam["warn"])
        assert dg.DUAL.qwen_floor == qwen["pass"]

    def test_cluster_bounds_match_the_committed_evidence(self, measured_pairs):
        """标定常量 vs 入库逐条分数：必须一致（改证据不改常量 = 门禁与证据脱钩）。"""
        same = [p for p in measured_pairs if p["role"] == "same"]
        cross = [p for p in measured_pairs if p["role"] != "same"]
        for key in ("cam_cos", "qwen_cos"):
            same_min = round(min(p[key] for p in same), 4)
            cross_max = round(max(p[key] for p in cross), 4)
            assert same_min == dg.MEASURED[key]["same_min"], f"{key} 同人最低分与标定不一致"
            assert cross_max == dg.MEASURED[key]["cross_max"], f"{key} 异人最高分与标定不一致"

    def test_gap_is_split_seven_to_three(self):
        """间隔按 7:3 分：pass 靠同人一侧（回退余量大），warn 靠异人一侧。"""
        cam = dg.MEASURED["cam_cos"]
        gap = cam["same_min"] - cam["cross_max"]
        assert dg.DUAL.cam_pass == round(dg._floor_to(cam["cross_max"] + 0.7 * gap), 4)
        assert dg.DUAL.cam_warn == round(dg._floor_to(cam["cross_max"] + 0.3 * gap), 4)
        assert dg.DUAL.cam_warn < dg.DUAL.cam_pass

    def test_floor_to_does_not_leak_float_error(self):
        """★ `floor(x/step)*step` 会让 0.53 变成 0.5299999999999999（实测踩过），
        阈值是要写进记录、显示给用户看的数字，必须干净。"""
        v = dg._floor_to(0.5363)
        assert v == 0.53 and repr(v) == "0.53", repr(v)
        assert dg._floor_to(0.97) == 0.97

    def test_overlapping_clusters_raise_instead_of_inventing_a_threshold(self):
        """两簇重叠时**没有阈值可推** —— 报错比给一个会错判的数字安全。"""
        with pytest.raises(ValueError):
            dg.derive(same_min=0.40, cross_max=0.60)


# ---------------- 2. 真数据重放（防「门禁空转」） ----------------


class TestReplayOnMeasuredPairs:
    def test_replay_passes_every_same_and_rejects_every_impostor(self, measured_pairs):
        res = dg.audit(measured_pairs)
        assert (res["n_same"], res["n_cross"]) == (48, 96), "标定集规模变了"
        assert res["same_pass_rate"] == 1.0, f"误杀同人：{res['same']}"
        assert res["cross_pass_rate"] == 0.0, f"误放异人：{res['cross']}"
        assert res["cross"][dg.REJECT] == 96, f"异人应判 reject，实际 {res['cross']}"
        assert res["same"][dg.PASS] == 48

    def test_audit_notices_a_loosened_threshold(self, measured_pairs):
        """★ 这条是第 1 条的**负例**：阈值放宽必须被 audit 报出来，否则
        `same_pass_rate == 1.0` 可能只是因为判词永远返回 pass（门禁空转）。"""
        loose = dg.DualThresholds(cam_pass=0.20, cam_warn=0.10, qwen_floor=0.90)
        res = dg.audit(measured_pairs, loose)
        assert res["cross_pass_rate"] > 0, "阈值松到能放异人进门，audit 却没报"
        assert res["same_pass_rate"] == 1.0

    def test_audit_notices_a_tightened_threshold(self, measured_pairs):
        tight = dg.DualThresholds(cam_pass=0.90, cam_warn=0.80, qwen_floor=0.99)
        res = dg.audit(measured_pairs, tight)
        assert res["same_pass_rate"] < 1.0, "阈值紧到拦自己人，audit 却没报"
        assert res["cross_pass_rate"] == 0.0

    def test_single_ruler_self_space_has_almost_no_headroom(self):
        """★ 为什么不能只挂自空间：0.95 离异人上界只差 0.0034。
        这条断言就是「单尺方案」的墓志铭 —— 谁想改回去，先解释这个数字。"""
        margin = dg.MEASURED["qwen_cos"]["cross_max"] - 0.95
        assert 0 < margin < 0.01, f"自空间在 0.95 处的余量 = {margin:.4f}"
        assert dg.DUAL.qwen_floor > 0.95, "双门槛的自空间下限应高于旧单尺阈值"


# ---------------- 3. 判词 ----------------


class TestVerdict:
    def test_both_rulers_agree_pass(self):
        v = dg.verdict(cam_sim=0.70, qwen_sim=0.985)
        assert v["verdict"] == dg.PASS and v["decided_by"] == "cam_cos+qwen_cos"
        assert v["reasons"]

    def test_impostor_is_rejected_by_the_primary_ruler(self):
        v = dg.verdict(cam_sim=0.15, qwen_sim=0.94)   # 自空间老口径会当「有点低」，双门槛直接否决
        assert v["verdict"] == dg.REJECT and v["decided_by"] == "cam_cos"

    def test_mid_zone_is_review_not_reject(self):
        v = dg.verdict(cam_sim=round((dg.DUAL.cam_warn + dg.DUAL.cam_pass) / 2, 4),
                       qwen_sim=0.985)
        assert v["verdict"] == dg.REVIEW

    def test_contradiction_between_rulers_is_review(self):
        """主判过、保底不过 → 待复核：**不拿弱尺子否决强尺子**，但要有人听一下。"""
        v = dg.verdict(cam_sim=0.70, qwen_sim=0.94)
        assert v["verdict"] == dg.REVIEW and v["decided_by"] == "qwen_cos"

    def test_boundaries_are_inclusive(self):
        t = dg.DUAL
        assert dg.verdict(cam_sim=t.cam_pass, qwen_sim=t.qwen_floor)["verdict"] == dg.PASS
        assert dg.verdict(cam_sim=t.cam_warn, qwen_sim=0.99)["verdict"] == dg.REVIEW
        assert dg.verdict(cam_sim=round(t.cam_warn - 0.0001, 4),
                          qwen_sim=0.99)["verdict"] == dg.REJECT

    def test_missing_primary_is_unknown_not_reject(self):
        """★ 未测 ≠ 不合格：断网/没缓存时不能告诉用户「这不是本人」。"""
        for qwen in (None, 0.99, 0.50):
            v = dg.verdict(cam_sim=None, qwen_sim=qwen)
            assert v["verdict"] == dg.UNKNOWN and v["decided_by"] is None

    def test_missing_backstop_does_not_block_pass(self):
        v = dg.verdict(cam_sim=0.70, qwen_sim=None)
        assert v["verdict"] == dg.PASS
        assert "未测" in "；".join(v["reasons"])

    def test_thresholds_are_echoed_for_the_ui(self):
        assert dg.verdict(cam_sim=0.70, qwen_sim=0.985)["thresholds"] == dg.DUAL.to_dict()


# ---------------- 4. 灯：判词的翻译层 ----------------


class TestSimilarityLamp:
    def test_dual_path_colours_three_testable_levels(self):
        assert vr.similarity_lamp(emb_sim=0.985, cam_sim=0.70).lamp == vr.GREEN
        assert vr.similarity_lamp(emb_sim=0.985, cam_sim=0.45).lamp == vr.YELLOW  # 待复核区
        assert vr.similarity_lamp(emb_sim=0.94, cam_sim=0.70).lamp == vr.YELLOW   # 两尺矛盾
        assert vr.similarity_lamp(emb_sim=0.985, cam_sim=0.15).lamp == vr.RED     # 主判否决

    def test_lamp_value_is_the_primary_ruler_and_sources_name_both(self):
        lamp = vr.similarity_lamp(emb_sim=0.985, cam_sim=0.70)
        assert lamp.value == 0.70, "灯的值应是主判（独立声纹）"
        assert lamp.sources == ["cam_cos", "qwen_cos"]

    def test_detail_spells_out_both_rulers_so_a_yellow_is_explainable(self):
        det = vr.similarity_lamp(emb_sim=0.94, cam_sim=0.70).detail
        assert "独立声纹" in det and "自空间" in det and "0.94" in det

    def test_single_ruler_path_keeps_the_old_behaviour(self):
        """只给自空间分的旧调用方（voice_qc / 其它）行为不变 —— 但灯必须说明它是单尺。"""
        lamp = vr.similarity_lamp(emb_sim=0.96)
        assert lamp.lamp == vr.GREEN and lamp.value == 0.96
        assert "单尺" in lamp.detail

    def test_untested_is_none_not_red(self):
        lamp = vr.similarity_lamp()
        assert lamp.lamp is None and lamp.value is None
        assert vr.three_lamps()["lamps"][0]["lamp"] is None

    def test_three_lamps_forwards_cam_sim(self):
        out = vr.three_lamps(emb_sim=0.985, cam_sim=0.70, hnr=18.0, h1_h2=4.0,
                             spectral_tilt=-3.0)
        assert out["lamps"][0]["lamp"] == vr.GREEN
        assert out["lamps"][0]["value"] == 0.70


# ---------------- 5. 生产接线：随访门禁 ----------------


class TestFollowupGateWiring:
    def test_assess_sample_maps_scores_to_a_verdict(self, iso_media, monkeypatch):
        pid = _profile_with_ref(iso_media, monkeypatch)
        monkeypatch.setattr(vp, "_cam_sim_vs_baseline", lambda p, w: (0.70, ""))
        monkeypatch.setattr(vp, "_emb_sim_vs_baseline", lambda p, w: 0.985)
        a = vp.assess_sample(pid, _wav(iso_media / "s.wav"))
        assert a["cam_sim"] == 0.70 and a["qwen_sim"] == 0.985
        assert a["dual"]["verdict"] == dg.PASS
        assert a["dual"]["thresholds"] == dg.DUAL.to_dict()

    def test_followup_record_carries_cam_sim_and_verdict(self, iso_media, monkeypatch):
        pid = _profile_with_ref(iso_media, monkeypatch)
        _stub_metrics(monkeypatch)
        monkeypatch.setattr(vp, "_cam_sim_vs_baseline", lambda p, w: (0.70, ""))
        monkeypatch.setattr(vp, "_emb_sim_vs_baseline", lambda p, w: 0.985)
        rec = vp.add_followup(pid, _wav(iso_media / "f.wav"))
        assert rec.get("error") is None, rec
        assert rec["cam_sim"] == 0.70 and rec["emb_sim"] == 0.985
        assert rec["dual"]["verdict"] == dg.PASS and rec["cam_sim_error"] == ""
        # 灯跟着双门槛走（值 = 主判），且落盘后还能读回来
        assert rec["lamps"]["lamps"][0]["value"] == 0.70
        assert vp.read_followups(pid)[0]["dual"]["verdict"] == dg.PASS
        # 审计日志里能查到判定 —— 「谁、何时、做了什么、判成什么」
        ev = [e for e in vp.read_events(pid) if e["kind"] == "followup"]
        assert ev and ev[-1]["verdict"] == dg.PASS

    def test_cam_disabled_degrades_to_unknown_not_reject(self, iso_media, monkeypatch):
        """★ 门禁拿不到主判时必须说「未测」，不能靠自空间分替它下结论。"""
        pid = _profile_with_ref(iso_media, monkeypatch)
        _stub_metrics(monkeypatch)
        monkeypatch.setenv("VM_CAM_SIM", "0")
        monkeypatch.setattr(vp, "_emb_sim_vs_baseline", lambda p, w: 0.985)
        rec = vp.add_followup(pid, _wav(iso_media / "f.wav"))
        assert rec["cam_sim"] is None and "关闭" in rec["cam_sim_error"]
        assert rec["dual"]["verdict"] == dg.UNKNOWN
        assert rec["emb_sim"] == 0.985, "自空间分照旧记录（保底/历史口径都在）"

    def test_uncached_weights_are_skipped_with_a_reason_and_no_download(
            self, iso_media, monkeypatch):
        pid = _profile_with_ref(iso_media, monkeypatch)
        monkeypatch.setenv("VM_CAM_SIM", "1")
        monkeypatch.setattr(vp, "_cam_cache_ready", lambda: False)
        sim, why = vp._cam_sim_vs_baseline(pid, _wav(iso_media / "f.wav"))
        assert sim is None and "缓存" in why

    def test_cosine_is_computed_locally_without_the_worker(self, iso_media, monkeypatch):
        """独立声纹**不依赖 GPU worker** —— 随访不能因为 worker 没起就判不了。"""
        import speaker_sep

        pid = _profile_with_ref(iso_media, monkeypatch)
        monkeypatch.setenv("VM_CAM_SIM", "1")
        monkeypatch.setattr(vp, "_cam_cache_ready", lambda: True)
        monkeypatch.setattr(speaker_sep, "_read16k",
                            lambda p: np.array([1.0, 0.0, 0.0]) if Path(p).name == "f.wav"
                            else np.array([0.6, 0.8, 0.0]))
        monkeypatch.setattr(speaker_sep, "_sv_embed", lambda a: np.asarray(a, dtype=float))
        sim, why = vp._cam_sim_vs_baseline(pid, _wav(iso_media / "f.wav"))
        assert why == "" and sim == pytest.approx(0.6, abs=1e-4)

    def test_no_baseline_means_no_primary_score(self, iso_media, monkeypatch):
        monkeypatch.setenv("VM_CAM_SIM", "1")
        pid = vp.create_profile("甲")["id"]          # 刻意不加参考音
        sim, why = vp._cam_sim_vs_baseline(pid, _wav(iso_media / "f.wav"))
        assert sim is None and "参考音" in why

    def test_broken_gate_module_degrades_instead_of_crashing(self, iso_media, monkeypatch):
        """判据模块坏了 = 「未测」，不是 500 —— 随访主流程不许被加分项拖垮。"""
        pid = _profile_with_ref(iso_media, monkeypatch)
        monkeypatch.setattr(vp, "_cam_sim_vs_baseline", lambda p, w: (0.70, ""))
        monkeypatch.setattr(vp, "_emb_sim_vs_baseline", lambda p, w: 0.985)
        monkeypatch.setattr(dg, "verdict", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("炸")))
        a = vp.assess_sample(pid, _wav(iso_media / "s.wav"))
        assert a["dual"]["verdict"] == dg.UNKNOWN and a["dual"]["thresholds"] is None


# ---------------- 6. 证据与判据的对齐（防「文档说一套、代码算另一套」） ----------------


def test_evidence_csv_shape_is_the_contract(measured_pairs):
    """pairs.csv 的列名是标定与门禁之间的契约：换列名 = 重放静默失效。"""
    with open(_PAIRS, "r", encoding="utf-8", newline="") as f:
        cols = next(csv.reader(f))
    assert cols == ["role", "speaker", "dur", "cond", "sample_id", "ref_id",
                    "qwen_cos", "cam_cos"]
    assert all(p["role"] in ("same", "cross") for p in measured_pairs)
