"""嗓音保存评测引擎单测（2026-10-02，大创研究层）。

被测：`tools/voicebank_eval.py`（矩阵规划 / 客观测量 / 统计与相关性）。

★ 这个文件守的是**这个引擎最容易算错、而且算错了不会报错的地方**。
  统计代码的坏处就在这里：方向反了、秩算错了、样本不够硬凑一个数 —— 全都"跑得通"，
  只是结论是错的。所以下面每一条都对着一个具体的错法：

  1. **指标方向表**（`HIGHER_IS_BETTER`）：`spectral_tilt` 的「好」一侧是**数值更大**、
     `h1_h2` **越大越挤压**。方向写反 = 整份报告的结论反掉。
     这与 `docs/音色体检三灯.md` §3.2/§3.3 同口径，也和 `test_voice_report.py`
     里那两条方向断言互为印证。
  2. **Spearman 的并列必须取平均秩**：主观听测是 MOS 五档，**天然大量并列**。
     直接给并列项编不同序号会让相关系数虚高 —— 而虚高的相关系数正好会支撑
     「客观指标可以替代人工听测」这个结论，也就是**错得刚好迎合预期**。
  3. **样本不足不许编数字**：n < 3 或一侧无方差 → None，绝不返回 0.0
     （0.0 会被读成"测过了，不相关"，与"没测"是完全不同的结论）。
  4. **饱和点的三种"没找到"要分开**：档位不足 / 总增益 ≤ 0 / 逐档都在涨 ——
     都要返回 None 并让报告明说，而不是硬指一个档位。
  5. **时长档标签别写成分钟**：60s 必须还是 `60s`（写成 `1min` 会让 3s 变成 `0.05min`，
     目录既难读又难排序）。
  6. **矩阵要控制住文本**：同一说话人的不同时长档必须念**同一句**，
     否则「时长」这个自变量被文本混淆，RQ1 直接失效。
  7. **可选指标缺依赖要降级、不许中断**：一轮测量跑几十分钟，
     不该因为 worker 没起就全废（与 `voice_metrics` / `voice_qc` 的「绝不裸崩」同一约定）。
"""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "tools", _ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import voicebank_eval as ve  # noqa: E402


# ---------------- 合成音频 ----------------

def _write_wav(path: Path, seconds: float = 2.0, sr: int = 22050, f0: float = 150.0):
    """写一段元音状合成音（够长、有浊音段，能被指标模块接受）。"""
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


def _samples_dir(root: Path, exp: str) -> Path:
    return root / exp / "samples"


def _put_samples(root: Path, exp: str, rows: list[tuple[str, str, str]]):
    """按 (说话人, 时长档, 条件) 造样本文件。"""
    out = []
    for sp, tag, cond in rows:
        p = _samples_dir(root, exp) / sp / tag / f"{cond}.wav"
        out.append(_write_wav(p))
    return out


# ---------------- 1. 指标方向 ----------------

class TestMetricDirection:
    """方向表是整份报告的语义地基，写反了不会报错、只会得出相反结论。"""

    def test_tilt_is_higher_better(self):
        # tilt 越接近 0（数值更大）越健康 —— 与其余指标不同向，是三灯文档记过的坑
        assert ve.HIGHER_IS_BETTER["spectral_tilt"] is True

    def test_h1_h2_is_lower_better(self):
        # 越大 = 谱滚降越陡 = 越挤压（本机合成信号实测标定）
        assert ve.HIGHER_IS_BETTER["h1_h2"] is False

    def test_stability_metrics_lower_better(self):
        assert ve.HIGHER_IS_BETTER["jitter_local"] is False
        assert ve.HIGHER_IS_BETTER["shimmer_local"] is False

    def test_similarity_and_intelligibility_higher_better(self):
        assert ve.HIGHER_IS_BETTER["emb_sim"] is True
        assert ve.HIGHER_IS_BETTER["asr_overlap"] is True

    def test_every_metric_key_has_a_direction(self):
        """新增指标忘了登记方向 → 统计时 KeyError 或静默按"越大越好"算。"""
        assert set(ve.METRIC_KEYS) <= set(ve.HIGHER_IS_BETTER)


# ---------------- 2/3. Spearman ----------------

class TestSpearman:
    def test_perfect_monotonic_is_one(self):
        assert ve.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)

    def test_reverse_monotonic_is_minus_one(self):
        assert ve.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)

    def test_nonlinear_but_monotonic_is_still_one(self):
        """秩相关不假设线性 —— 这正是选它而不是 Pearson 的原因。"""
        assert ve.spearman([1, 2, 3, 4], [1, 100, 101, 100000]) == pytest.approx(1.0)

    def test_ties_use_average_ranks(self):
        """★ 主观听测是五档 MOS，天然大量并列。

        若把并列项编成**不同序号**，相关系数会虚高 —— 而虚高恰好支撑
        「客观指标能替代人工听测」这个结论，属于**错得刚好迎合预期**。
        这里按平均秩手算：(1,2,3,4) vs (1,1,2,2) → 0.8944。
        （把并列编成 1,2,3,4 会算出 1.0，本断言会红。）

        ⚠️ 但这条**抓不到所有**并列错法：若错成「组内下界」（如 1,1,3,3），
        那只是对正确秩做了常数平移，而 Pearson 对平移不变 → 相关系数一模一样。
        所以「平均秩」这件事真正靠的是下面 `test_ranks_are_averaged` 的**直测**。
        这条是 2026-10-02 变异验证实测出来的，别再以为只留上面这条就够了。
        """
        r = ve.spearman([1, 2, 3, 4], [1, 1, 2, 2])
        assert r == pytest.approx(0.8944, abs=1e-3)

    def test_all_tied_on_one_side_gives_none(self):
        """一侧完全没有方差 → 相关性无定义，必须 None 而不是 0.0。

        0.0 会被读成「测过了，不相关」；None 才是「没测出来」。
        """
        assert ve.spearman([1, 1, 1, 1], [1, 2, 3, 4]) is None

    def test_too_few_pairs_gives_none(self):
        assert ve.spearman([1, 2], [3, 4]) is None

    def test_pairs_with_none_are_dropped_then_counted(self):
        # 5 对里剔掉 2 对（含 None），剩 3 对刚好够
        xs = [1, None, 3, 4, None]
        ys = [10, 20, 30, 40, 50]
        assert ve.spearman(xs, ys) == pytest.approx(1.0)

    def test_dropping_below_min_n_gives_none(self):
        xs = [1, None, None, 4, None]
        ys = [10, 20, 30, 40, 50]
        assert ve.spearman(xs, ys) is None

    def test_ranks_are_averaged(self):
        """★ 这是「平均秩」**唯一真正的守卫**（直测秩的值）。

        为什么不能只靠相关系数的断言：把并列错成「组内下界」（1.5→1、3.5→3）
        只是常数平移，Pearson 对平移不变 → 相关系数不变、断言全绿。
        只有直接看秩的数值才抓得到。2026-10-02 变异验证实测。
        """
        assert ve._ranks([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]
        assert ve._ranks([5, 5, 5]) == [2.0, 2.0, 2.0]
        assert ve._ranks([1, 2, 3]) == [1.0, 2.0, 3.0]


# ---------------- 4. 饱和点 ----------------

class TestSaturationPoint:
    def test_saturates_at_the_plateau_start(self):
        # 3s→10s 涨 0.20，10s→30s 涨 0.05，30s→60s 只涨 0.01（< 10% 总增益）
        # → 饱和档是 30s：录到 30s 就够了
        sat = ve.saturation_point([3, 10, 30, 60], [0.50, 0.70, 0.75, 0.76])
        assert sat is not None
        assert sat["status"] == "saturated"
        assert sat["duration_tag"] == "30s"

    def test_still_climbing_is_reported_not_as_saturation(self):
        """逐档都在涨 = 还没饱和。硬指一个档位会得出"录 3s 就够"的错误建议。"""
        sat = ve.saturation_point([3, 10, 30, 60], [0.10, 0.20, 0.30, 0.40])
        assert sat is not None
        assert sat["status"] == "still_climbing"
        assert sat["duration_s"] is None

    def test_non_monotonic_is_not_reported_as_saturation(self):
        """★ 端到端演示实测踩到的 bug（2026-10-02）。

        指标序列 [-21.9, -22.4, -20.6, -20.5] 的**首个相邻增量是负的**（回退）。
        旧判据只看「首个增量 < 阈值」→ 会报成「3s 就饱和了」，而它根本没饱和。
        正确行为：出现负增量就不下饱和结论，如实说不可判定。
        """
        sat = ve.saturation_point([3, 10, 30, 60],
                                  [-21.9, -22.4, -20.6, -20.5])
        assert sat is not None
        assert sat["status"] == "non_monotonic"
        assert sat["duration_s"] is None
        assert "非单调" in sat["note"]

    def test_no_total_gain_returns_none(self):
        """总增益 ≤ 0 = 数据不足以判定（与"非单调"、"未饱和"都不是一回事）。"""
        assert ve.saturation_point([3, 10, 30], [0.50, 0.50, 0.40]) is None

    def test_single_duration_returns_none(self):
        assert ve.saturation_point([3], [0.5]) is None

    def test_length_mismatch_returns_none(self):
        assert ve.saturation_point([3, 10, 30], [0.5, 0.6]) is None

    def test_lower_is_better_is_normalised_inside(self):
        """★ jitter 是「越小越好」。调用方不用自己取反 —— 方向归一收在函数内部，
        这样全模块只有一个地方需要担心方向。"""
        sat = ve.saturation_point([3, 10, 30, 60], [5.0, 3.0, 2.5, 2.4],
                                  higher_is_better=False)
        assert sat is not None
        assert sat["status"] == "saturated"
        assert sat["duration_tag"] == "30s"

    def test_same_numbers_flip_with_direction(self):
        """同一组数字，方向不同 → 结论必须不同（防止"忘了传方向"）。

        这组数字是**单调下降**的：
        - 当"越小越好"（如 jitter）时它是进步，且在 30s 饱和；
        - 当"越大越好"时它整体是退步 → 总增益 ≤ 0 → 数据不足以判定（None），
          **不能**报成一个饱和档位。
        """
        vals = [5.0, 3.0, 2.5, 2.4]
        durs = [3, 10, 30, 60]
        assert ve.saturation_point(durs, vals,
                                   higher_is_better=False)["status"] == "saturated"
        assert ve.saturation_point(durs, vals, higher_is_better=True) is None


# ---------------- 5. 时长档标签 ----------------

class TestDurationTags:
    def test_sixty_seconds_stays_seconds(self):
        """★ 60s 不许写成 1min —— 那 3s 就得写成 0.05min，目录既难读又难排序。"""
        assert ve._dur_tag(60) == "60s"

    def test_long_durations_use_minutes(self):
        assert ve._dur_tag(300) == "5min"

    def test_round_trip(self):
        for sec in (3, 10, 30, 60, 300):
            assert ve._parse_dur_tag(ve._dur_tag(sec)) == float(sec)

    def test_unparsable_tag_gives_none(self):
        """认不出来要返回 None 让调用方记 error，不许猜一个默认值。"""
        assert ve._parse_dur_tag("abc") is None
        assert ve._parse_dur_tag("") is None

    def test_uppercase_and_spaces_tolerated(self):
        assert ve._parse_dur_tag(" 5MIN ") == 300.0


# ---------------- 6. 矩阵 ----------------

class TestBuildMatrix:
    def test_size_is_product(self):
        rows = ve.build_matrix(["a", "b"], [3, 10], ["same", "cross"])
        assert len(rows) == 2 * 2 * 2

    def test_sample_ids_unique(self):
        rows = ve.build_matrix(["a", "b"], [3, 10, 30], list(ve.CONDITIONS))
        ids = [r["sample_id"] for r in rows]
        assert len(ids) == len(set(ids))

    def test_same_text_across_durations_for_one_speaker(self):
        """★ 控制变量：同一说话人不同时长档必须念同一句。

        否则「10s 档比 3s 档好」可能只是抽到了更好念的句子 —— RQ1 直接失效。
        """
        rows = ve.build_matrix(["a"], [3, 10, 30, 60], ["cross"])
        texts = {r["text"] for r in rows}
        assert len(texts) == 1

    def test_paths_point_at_the_documented_layout(self):
        r = ve.build_matrix(["a"], [10], ["same"])[0]
        assert r["ref"] == "refs/a/10s.wav"
        assert r["truth"] == "truth/a/same.wav"
        assert r["sample"] == "samples/a/10s/same.wav"

    def test_every_condition_has_repro_text(self):
        """新增条件忘了配语料 → 矩阵里出现空文本，合成出来是静音，且不报错。"""
        for cond in ve.CONDITIONS:
            assert ve.REPRO_TEXTS.get(cond), f"条件 {cond} 没有复现文本"


# ---------------- 7. plan ----------------

class TestPlan:
    def test_writes_matrix_and_corpus_and_skeleton(self, tmp_path):
        res = ve.plan("vb01", ["spk01", "spk02"], root=tmp_path)
        assert res["error"] is None
        base = tmp_path / "vb01"
        assert (base / "matrix.json").exists()
        assert (base / "corpus.json").exists()
        for d in ("refs", "truth", "samples"):
            assert (base / d).is_dir()
        assert (base / "refs" / "spk01").is_dir()
        assert (base / "truth" / "spk01").is_dir()
        assert (base / "samples" / "spk01" / "10s").is_dir()

    def test_matrix_json_is_reloadable(self, tmp_path):
        ve.plan("vb01", ["spk01"], root=tmp_path)
        data = json.loads((tmp_path / "vb01" / "matrix.json").read_text(encoding="utf-8"))
        assert data["n_samples"] == len(data["matrix"]) == 1 * 4 * 4

    def test_warns_when_duration_exceeds_script_capacity(self, tmp_path):
        """★ 参考脚本只有约 90s 的容量。要 300s 档却不扩脚本 → 录不满，
        必须**提前警告**，而不是等录到一半发现没词可念。"""
        res = ve.plan("vb01", ["spk01"], durations=[3, 300], root=tmp_path)
        assert any("超出参考脚本容量" in w for w in res["warnings"])

    def test_no_warning_for_durations_within_capacity(self, tmp_path):
        res = ve.plan("vb01", ["spk01"], durations=[3, 10, 30, 60], root=tmp_path)
        assert not [w for w in res["warnings"] if "超出参考脚本容量" in w]

    def test_empty_speakers_is_an_error_not_a_crash(self, tmp_path):
        res = ve.plan("vb01", [], root=tmp_path)
        assert res["error"] and res["error_stage"] == "参数"

    def test_custom_conditions(self, tmp_path):
        res = ve.plan("vb01", ["spk01"], conditions=["same"], root=tmp_path)
        assert res["n_samples"] == 4
        assert res["conditions"] == ["same"]


# ---------------- 8. measure ----------------

class TestMeasure:
    def test_missing_samples_dir_is_error_not_crash(self, tmp_path):
        res = ve.measure("nope", root=tmp_path)
        assert res["error"] and res["error_stage"] == "样本目录"

    def test_empty_samples_dir_is_error(self, tmp_path):
        (tmp_path / "vb01" / "samples").mkdir(parents=True)
        res = ve.measure("vb01", root=tmp_path)
        assert res["error"] and res["error_stage"] == "样本扫描"

    def test_runs_over_synthetic_wavs_and_writes_results(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ve.voice_metrics, "extract",
                            lambda p: {"hnr": 12.0, "h1_h2": 3.0, "spectral_tilt": -7.0,
                                       "jitter_local": 0.5, "shimmer_local": 2.0,
                                       "f0_median": 150.0, "voiced_ratio": 0.8,
                                       "duration_s": 2.0, "error": None})
        _put_samples(tmp_path, "vb01", [("spk01", "10s", "same"),
                                        ("spk01", "10s", "cross")])
        res = ve.measure("vb01", root=tmp_path)
        assert res["error"] is None
        assert res["count"] == 2 and res["measured"] == 2 and res["errors"] == 0
        assert (tmp_path / "vb01" / "results.json").exists()
        assert (tmp_path / "vb01" / "results.csv").exists()

    def test_sample_id_and_duration_are_parsed_from_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ve.voice_metrics, "extract",
                            lambda p: {"error": None, "hnr": 1.0})
        _put_samples(tmp_path, "vb01", [("spk07", "30s", "emotion")])
        res = ve.measure("vb01", root=tmp_path)
        s = res["samples"][0]
        assert s["sample_id"] == "spk07/30s/emotion"
        assert s["speaker"] == "spk07" and s["condition"] == "emotion"
        assert s["duration_s"] == 30.0

    def test_unparsable_duration_tag_is_recorded_not_crashed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ve.voice_metrics, "extract", lambda p: {"error": None})
        _write_wav(_samples_dir(tmp_path, "vb01") / "spk01" / "weird" / "same.wav")
        res = ve.measure("vb01", root=tmp_path)
        assert res["errors"] == 1
        assert "无法解析时长档" in res["samples"][0]["error"]

    def test_metric_error_counts_as_error_but_keeps_the_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ve.voice_metrics, "extract",
                            lambda p: {"error": "音频损坏"})
        _put_samples(tmp_path, "vb01", [("spk01", "3s", "same")])
        res = ve.measure("vb01", root=tmp_path)
        assert res["errors"] == 1 and res["measured"] == 0
        assert res["samples"][0]["error"] == "音频损坏"

    def test_missing_worker_degrades_instead_of_failing(self, tmp_path, monkeypatch):
        """★ 可选指标缺依赖必须降级：一轮测量几十分钟，不该因为 worker 没起就全废。"""
        monkeypatch.setattr(ve.voice_metrics, "extract",
                            lambda p: {"error": None, "hnr": 1.0})

        def _boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", _boom)
        _put_samples(tmp_path, "vb01", [("spk01", "10s", "same")])
        res = ve.measure("vb01", root=tmp_path, with_emb=True, with_asr=True)
        assert res["error"] is None
        assert res["measured"] == 1
        assert any("worker 8001 未启动" in n for n in res["notes"])
        assert res["samples"][0]["metrics"].get("emb_sim") is None

    def test_real_metrics_never_crash_on_synthetic_audio(self, tmp_path):
        """不 monkeypatch：真的走一遍 voice_metrics。
        parselmouth 缺失时指标为 None —— 但**绝不允许抛**（"绝不裸崩"约定）。"""
        _put_samples(tmp_path, "vb01", [("spk01", "10s", "same")])
        res = ve.measure("vb01", root=tmp_path)
        assert res["error"] is None
        assert len(res["samples"]) == 1


# ---------------- 9. analyze ----------------

def _write_results(root: Path, exp: str, rows: list[dict]):
    base = root / exp
    base.mkdir(parents=True, exist_ok=True)
    (base / "results.json").write_text(json.dumps(
        {"exp": exp, "count": len(rows), "measured": len(rows), "errors": 0,
         "samples": rows}, ensure_ascii=False), encoding="utf-8")
    return base


def _row(sp: str, tag: str, cond: str, **metrics) -> dict:
    return {"sample_id": f"{sp}/{tag}/{cond}", "speaker": sp, "duration_tag": tag,
            "duration_s": ve._parse_dur_tag(tag), "condition": cond,
            "path": "", "metrics": metrics, "error": None, "note": None}


class TestAnalyze:
    def test_missing_results_is_error_not_crash(self, tmp_path):
        res = ve.analyze("nope", root=tmp_path)
        assert res["error"] and res["error_stage"] == "测量结果"

    def test_empty_samples_is_error(self, tmp_path):
        _write_results(tmp_path, "vb01", [])
        res = ve.analyze("vb01", root=tmp_path)
        assert res["error"]

    def test_rq1_groups_by_duration_and_finds_saturation(self, tmp_path):
        rows = []
        for sp, base in (("a", 0.50), ("b", 0.60)):
            for tag, val in (("3s", base), ("10s", base + 0.20),
                             ("30s", base + 0.25), ("60s", base + 0.26)):
                rows.append(_row(sp, tag, "same", emb_sim=val))
        _write_results(tmp_path, "vb01", rows)
        res = ve.analyze("vb01", root=tmp_path)
        assert res["error"] is None
        rq1 = res["rq1"]["emb_sim"]
        assert set(rq1["by_duration"]) == {"3s", "10s", "30s", "60s"}
        assert rq1["direction"] == "higher_better"
        assert rq1["saturation"]["duration_tag"] == "30s"

    def test_rq1_reports_lower_better_direction(self, tmp_path):
        rows = [_row("a", t, "same", jitter_local=v) for t, v in
                (("3s", 5.0), ("10s", 3.0), ("30s", 2.5), ("60s", 2.4))]
        _write_results(tmp_path, "vb01", rows)
        res = ve.analyze("vb01", root=tmp_path)
        assert res["rq1"]["jitter_local"]["direction"] == "lower_better"
        assert res["rq1"]["jitter_local"]["saturation"]["duration_tag"] == "30s"

    def test_rq2_groups_by_condition(self, tmp_path):
        rows = [_row("a", "10s", "same", emb_sim=0.9),
                _row("a", "10s", "cross", emb_sim=0.7)]
        _write_results(tmp_path, "vb01", rows)
        res = ve.analyze("vb01", root=tmp_path)
        assert res["rq2"]["same"]["emb_sim"] == pytest.approx(0.9)
        assert res["rq2"]["cross"]["emb_sim"] == pytest.approx(0.7)

    def test_no_subjective_gives_no_coefficients(self, tmp_path):
        """没给主观评分 → 相关系数必须为空，且报告里明说，不许补 0。"""
        _write_results(tmp_path, "vb01", [_row("a", "10s", "same", emb_sim=0.9)])
        res = ve.analyze("vb01", root=tmp_path)
        assert res["rq3"]["correlations"] == {}
        md = (tmp_path / "vb01" / "report.md").read_text(encoding="utf-8")
        assert "不编造" in md

    def test_subjective_join_produces_correlation(self, tmp_path):
        rows = [_row("a", "10s", "same", emb_sim=v) for v in (0.5, 0.6, 0.7, 0.8)]
        _write_results(tmp_path, "vb01", rows)
        csv_p = tmp_path / "sub.csv"
        csv_p.write_text("sample_id,mos\n"
                         "a/10s/same,2\n", encoding="utf-8")
        # 只有 1 行 → 匹配不足，不给系数
        res = ve.analyze("vb01", root=tmp_path, subjective=csv_p)
        assert res["rq3"]["matched"] == 1
        assert res["rq3"]["correlations"] == {}
        assert any("不编造数字" in n for n in res["notes"])

    def test_subjective_with_enough_rows_gives_spearman(self, tmp_path):
        rows = [_row("a", tag, "same", emb_sim=v) for tag, v in
                (("3s", 0.5), ("10s", 0.6), ("30s", 0.7), ("60s", 0.8))]
        _write_results(tmp_path, "vb01", rows)
        csv_p = tmp_path / "sub.csv"
        csv_p.write_text(
            "sample_id,mos,abx\n"
            "a/3s/same,2,0\n"
            "a/10s/same,3,1\n"
            "a/30s/same,4,1\n"
            "a/60s/same,5,1\n", encoding="utf-8")
        res = ve.analyze("vb01", root=tmp_path, subjective=csv_p)
        assert res["rq3"]["matched"] == 4
        # emb_sim 与 mos 完全同序 → +1.0
        assert res["rq3"]["correlations"]["mos"]["emb_sim"] == pytest.approx(1.0)

    def test_duplicate_sample_ids_are_reported_not_silently_misjoined(self, tmp_path):
        """★ 重复的 sample_id 会让「主观评分 → 客观指标」的查找退化成取最后一条，
        于是同一组客观值被配到多个主观分上 —— 相关系数**照样算得出来**，只是错的。
        这种情况必须留痕（note），不能静默。"""
        rows = [_row("a", "10s", "same", emb_sim=v) for v in (0.5, 0.6, 0.7, 0.8)]
        _write_results(tmp_path, "vb01", rows)
        res = ve.analyze("vb01", root=tmp_path)
        assert any("sample_id 重复" in n for n in res["notes"])

    def test_subjective_file_missing_is_noted_not_crashed(self, tmp_path):
        _write_results(tmp_path, "vb01", [_row("a", "10s", "same", emb_sim=0.9)])
        res = ve.analyze("vb01", root=tmp_path,
                         subjective=tmp_path / "nope.csv")
        assert res["error"] is None
        assert any("不存在" in n for n in res["notes"])

    def test_report_files_are_written(self, tmp_path):
        _write_results(tmp_path, "vb01", [_row("a", "10s", "same", emb_sim=0.9)])
        ve.analyze("vb01", root=tmp_path)
        assert (tmp_path / "vb01" / "report.json").exists()
        assert (tmp_path / "vb01" / "report.md").exists()


# ---------------- 10. 可懂度口径 ----------------

class TestCharOverlap:
    def test_identical_is_one(self):
        assert ve.char_overlap("今天天气很好", "今天天气很好") == pytest.approx(1.0)

    def test_disjoint_is_zero(self):
        assert ve.char_overlap("abc", "xyz") == pytest.approx(0.0)

    def test_commas_are_ignored(self):
        """与 voice_qc._char_overlap 同口径：逗号不计入集合。"""
        assert ve.char_overlap("你好，世界", "你好世界") == pytest.approx(1.0)

    def test_empty_is_zero_not_crash(self):
        assert ve.char_overlap("", "") == pytest.approx(0.0)


# ---------------- 11. 语料 ----------------

class TestCorpus:
    def test_reference_script_has_enough_content(self):
        assert len(ve.REFERENCE_SCRIPT) >= 20

    def test_reference_script_sentences_are_nonempty_and_unique(self):
        assert all(s.strip() for s in ve.REFERENCE_SCRIPT)
        assert len(ve.REFERENCE_SCRIPT) == len(set(ve.REFERENCE_SCRIPT))

    def test_long_condition_text_is_actually_long(self):
        """long 条件的语义就是「>30 字」。写短了 RQ2 的"长句"这一档就没意义。"""
        for t in ve.REPRO_TEXTS["long"]:
            assert ve._char_count(t["text"]) > 30

    def test_same_condition_text_is_in_the_reference_script(self):
        """★ same 条件必须是「参考音里念过的句子」，否则它和 cross 没区别。"""
        for t in ve.REPRO_TEXTS["same"]:
            assert t["text"] in ve.REFERENCE_SCRIPT

    def test_cross_condition_text_is_not_in_the_reference_script(self):
        """★ cross 条件的语义是「参考音里没有的句子」。混进去就毁了泛化考察。"""
        for t in ve.REPRO_TEXTS["cross"]:
            assert t["text"] not in ve.REFERENCE_SCRIPT
