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


class TestCorpusIsSingleSource:
    """★ 语料只能有一份定义。

    研究测的是 A 脚本、产品却让用户念 B 脚本 → 结论对不上任何真实使用，
    而且**两边都不会报错**。所以这里断言评测工具用的是**产品侧那一份对象本身**
    （`is` 同一性），而不是"内容恰好相等"—— 内容相等挡不住有人复制一份出来。
    """

    def test_eval_reuses_the_product_corpus_object(self):
        import voicepreserve as vp

        assert ve.REFERENCE_SCRIPT is vp.RECORD_SCRIPT, (
            "评测工具自己复制了一份参考脚本 —— 语料的唯一真相源在 "
            "m2_server/voicepreserve.py，必须 import 而不是复制"
        )
        assert ve.REPRO_TEXTS is vp.REPRO_TEXTS, (
            "复现文本被复制了一份 —— 同上"
        )

    def test_char_count_delegates_instead_of_reimplementing(self):
        """字数口径也必须只有一份：漂了会直接改变时长档的容量判断。"""
        import voicepreserve as vp

        assert ve._char_count("你好，世界") == vp.char_count("你好，世界")
        assert ve._script_capacity_s() == vp.script_capacity_s()

    def test_duration_ladder_matches_the_product_side(self):
        """★ 档位口径也必须只有一份 —— 这是**研究自变量**。

        评测工具与产品两侧各自定义一份档位，一旦漂移（例如方案文档里曾写成含
        300s 的五档），"同一段录音切四档"的矩阵就会与产品实际能录出的档位不符，
        而**两边都不会报错** —— 只会得到一组对不上任何真实录音的结论。
        文档 `docs/嗓音保存评测协议.md` §3.1 是同一口径。
        """
        import voicepreserve as vp

        assert ve.DURATION_LADDER == vp.REF_DURATIONS, (
            "评测工具的档位与产品侧不一致 —— 改档位必须三处同改："
            "m2_server/voicepreserve.py::REF_DURATIONS / "
            "tools/voicebank_eval.py::DURATION_LADDER / docs/嗓音保存评测协议.md §3.1"
        )
        assert ve.DURATION_LADDER == (3.0, 10.0, 30.0, 60.0)


# ---------------- split：一条录音 → 前缀四档 ----------------

def _write_speech_like(path: Path, segments: int = 5, seg_s: float = 4.0,
                       gap_s: float = 0.6, sr: int = 22050, f0: float = 150.0):
    """写一段「有明确句末停顿」的合成录音，句末位置可预测。

    ★ 为什么需要它而不是 `_write_wav`：切分逻辑测的是**句末在哪里**，
      而 `_write_wav` 是一整段连续元音、**一个停顿都没有** →
      `detect_silences` 探到 0 段，对齐路径永远走不到，测了等于没测。
    """
    import soundfile as sf

    parts = []
    for _ in range(segments):
        t = np.arange(int(sr * seg_s)) / sr
        y = np.zeros_like(t)
        for k in range(1, 20):
            if k * f0 > sr / 2:
                break
            y += 10 ** (-6.0 * (k - 1) / 20.0) * np.sin(2 * np.pi * k * f0 * t)
        peak = float(np.abs(y).max())
        parts.append((y / peak * 0.5).astype(np.float32) if peak > 0 else y)
        parts.append(np.zeros(int(sr * gap_s), dtype=np.float32))
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.concatenate(parts), sr)
    return path


class TestCutPoints:
    """切点选择是**协议 §4.3 的落点**，它错了自变量就错了。

    两条规则必须同时成立（只满足一条都是错的）：
      ① 有合适的句末 → 对齐到句末（别切在半句上）；
      ② 句末离标称太远 → **宁可硬切**（无上限的对齐会把 60s 档切成 35s，
         比硬切危险得多，见 `ve.ALIGN_MAX_DEVIATION`）。
    """

    def test_no_silence_falls_back_to_hard_cut(self):
        got = ve.cut_points([3.0, 10.0], [], total_s=60.0)
        assert got[3.0]["cut"] == 3.0 and got[3.0]["aligned"] is False
        assert got[10.0]["cut"] == 10.0 and got[10.0]["aligned"] is False

    def test_aligns_to_the_nearest_sentence_end(self):
        """句末必须在**目标之前**（前缀截取不能往前伸）。

        静音起点 2.8 / 9.5：都在 25% 容差内 → 都要对齐。
        ⚠️ 别把句末放在目标之后（比如给 3s 档一个 3.5s 的句末）——
        那不是"前缀截取 3 秒"了，与协议 §3.1 的定义矛盾。
        """
        sil = [(2.8, 3.3), (9.5, 10.1)]
        got = ve.cut_points([3.0, 10.0], sil, total_s=12.0)
        assert got[3.0]["cut"] == 2.8 and got[3.0]["aligned"] is True
        assert got[10.0]["cut"] == 9.5 and got[10.0]["aligned"] is True

    def test_far_sentence_end_is_rejected_in_favour_of_hard_cut(self):
        """★ 这条守的是实测踩到的真坑：句末 35.4s 对 60s 档（偏离 41%）。

        若接受它，60s 档实际只有 35.4s，而 `analyze` 按标称 `duration_s`
        分组 → "60s"那一列装的是 35 秒的音频，整张 RQ1 表都错位。
        """
        got = ve.cut_points([60.0], [(35.4, 35.6)], total_s=65.0)
        assert got[60.0]["cut"] == 60.0
        assert got[60.0]["aligned"] is False
        assert "偏离" in got[60.0]["reason"]

    def test_deviation_boundary_is_inclusive(self):
        """正好卡在容差边界上（25%）应当**算对齐** —— 边界值要有确定行为。"""
        got = ve.cut_points([10.0], [(7.5, 7.9)], total_s=20.0)
        assert got[10.0]["cut"] == 7.5 and got[10.0]["aligned"] is True

    def test_target_longer_than_recording_uses_whole_thing(self):
        got = ve.cut_points([60.0], [], total_s=20.0)
        assert got[60.0]["cut"] == 20.0 and got[60.0]["aligned"] is False


class TestSplitCommand:
    def test_one_recording_yields_the_four_tiers(self, tmp_path):
        """★ 核心契约：**一条录音** → 四档，且每档实际秒数**不偏离标称超过容差**。

        ⚠️ 断言写的是"在容差内"而**不是"精确等于标称"** —— 对齐到句末
        （协议 §4.3）本就会让实际值略小于标称，例如 30s 档对齐到 27.5s。
        要求精确等于标称等于要求引擎**放弃对齐**，那是把协议要求测反了。
        真正要守的是 `ALIGN_MAX_DEVIATION` 那条线。
        """
        src = _write_speech_like(tmp_path / "rec.wav", segments=20, seg_s=3.0, gap_s=0.5)
        res = ve.split("e1", ["me"], src, root=tmp_path, durations=[3.0, 10.0, 30.0, 60.0])
        assert res.get("error") is None, res
        tags = [t["tag"] for t in res["tiers"]]
        assert tags == ["3s", "10s", "30s", "60s"]
        for t in res["tiers"]:
            assert t["actual_s"] is not None
            dev = abs(t["actual_s"] - t["duration_s"]) / t["duration_s"]
            assert dev <= ve.ALIGN_MAX_DEVIATION + 1e-6, t
            assert t["bytes"] > 0

    def test_refuses_more_than_one_speaker(self, tmp_path):
        """★ 一条录音只能属于一个人 —— 写多个人就是复制同一份音频当多个被试。"""
        src = _write_speech_like(tmp_path / "rec.wav")
        res = ve.split("e1", ["a", "b"], src, root=tmp_path)
        assert res["error"] and res["error_stage"] == "参数"

    def test_short_recording_is_reported_with_the_shortfall(self, tmp_path):
        src = _write_speech_like(tmp_path / "rec.wav", segments=2, seg_s=2.0, gap_s=0.3)
        res = ve.split("e1", ["me"], src, root=tmp_path)
        assert res["error_stage"] == "时长不足"
        assert res["needed_s"] == 60.0 and res["total_s"] < 60.0

    def test_missing_source_is_reported_not_thrown(self, tmp_path):
        res = ve.split("e1", ["me"], tmp_path / "nope.wav", root=tmp_path)
        assert res["error"] and res["error_stage"] == "读取录音"

    def test_actual_seconds_are_recorded_per_tier(self, tmp_path):
        """协议 §4.3：**标称与实际必须分开记** —— 报结论要用实际值。"""
        src = _write_speech_like(tmp_path / "rec.wav", segments=20, seg_s=3.0, gap_s=0.5)
        res = ve.split("e1", ["me"], src, root=tmp_path, durations=[10.0])
        t = res["tiers"][0]
        assert t["duration_s"] == 10.0          # 标称
        assert isinstance(t["actual_s"], float)  # 实际（读回来的）
        assert t["deviation_s"] is not None

    def test_split_json_records_what_was_actually_used(self, tmp_path):
        """切点与门限必须留档 —— 换了门限结果就变，报结论时要能追溯。"""
        src = _write_speech_like(tmp_path / "rec.wav", segments=20, seg_s=3.0, gap_s=0.5)
        res = ve.split("e1", ["me"], src, root=tmp_path)
        doc = json.loads((Path(res["root"]) / "split.json").read_text(encoding="utf-8"))
        assert doc["noise_db"] is not None
        assert doc["speaker"] == "me"
        assert len(doc["tiers"]) == 4
        assert "silence_spans" in doc

    def test_noisy_source_relaxes_threshold_until_it_finds_silences(self, tmp_path):
        """★ 自适应必须**能放宽** —— 单一门限会落在检出边缘，探不到就静默退化成硬切。

        这里给录音叠一层噪声：门限收在 RMS-15dB 时探不到，放宽后才能探到。
        """
        import soundfile as sf

        src = _write_speech_like(tmp_path / "rec.wav", segments=20, seg_s=3.0, gap_s=0.5)
        y, sr = sf.read(str(src))
        rng = np.random.default_rng(0)
        y = (y + rng.normal(0, 0.004, size=len(y))).astype(np.float32)
        noisy = tmp_path / "noisy.wav"
        sf.write(str(noisy), y, sr)

        sil, db, trials = ve.detect_silences(noisy)
        assert trials, "至少要试过一次"
        assert len(sil) >= ve.MIN_SILENCES_FOR_ALIGN, (
            f"自适应门限放宽到 {db}dB 仍探不到静音 —— 切点会全部退化成硬切，"
            f"而这只会在 warnings 里留一句话"
        )

    def test_explicit_threshold_is_not_relaxed(self, tmp_path):
        """显式给了门限就**不再放宽** —— 否则结果不可复现。"""
        src = _write_speech_like(tmp_path / "rec.wav", segments=20, seg_s=3.0, gap_s=0.5)
        _sil, db, trials = ve.detect_silences(src, noise_db=-34.0)
        assert db == -34.0
        assert len(trials) == 1


# ---------------- 盲听材料（listen / merge-listen） ----------------

def _listen_results(root: Path, exp: str, n_speakers: int = 1) -> Path:
    """造一个带 truth 与合成音频的测量结果：每说话人 4 档 × 4 条件。

    ★ `path` 必须是**真实存在**的文件：`listen` 会把"找不到音频"的样本从
      评分表里剔掉，所以造数据时不能只填 `sample_id`。
    """
    rows = []
    base = root / exp
    for i in range(n_speakers):
        sp = f"sp{i}"
        for tag in ("3s", "10s", "30s", "60s"):
            for cond in ve.CONDITIONS:
                sdir = base / "samples" / sp / tag
                sdir.mkdir(parents=True, exist_ok=True)
                sfile = sdir / f"{cond}.wav"
                sfile.write_bytes(b"RIFF")
                tdir = base / "truth" / sp
                tdir.mkdir(parents=True, exist_ok=True)
                (tdir / f"{cond}.wav").write_bytes(b"RIFF")
                r = _row(sp, tag, cond, emb_sim=0.9, hnr=15.0)
                r["path"] = str(sfile)
                r["truth"] = f"truth/{sp}/{cond}.wav"
                rows.append(r)
    _write_results(root, exp, rows)
    return base


def _read_csv(path) -> list[dict]:
    """读 CSV。**用 with 打开** —— `DictReader(open(...))` 会把文件句柄漏掉
    （ruff SIM115 会抓，Windows 上还可能因占用而删不掉临时文件）。"""
    import csv as _csv
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(_csv.DictReader(f))


class TestListenMaterials:
    def test_missing_results_is_error_not_crash(self, tmp_path):
        res = ve.listen("nope", root=tmp_path)
        assert res["error"] and res["error_stage"] == "测量结果"

    def test_sheet_carries_no_identity(self, tmp_path):
        """★ 最要紧的一条：发给听者的评分表**不能**泄露说话人/档位/条件。

        泄露了就不是盲听 —— 听者会按「60s 应该更好」的先验打分，
        而这正是 RQ3 要检验的东西。
        """
        _listen_results(tmp_path, "vb01")
        res = ve.listen("vb01", root=tmp_path)
        sheet = Path(res["sheet"]).read_text(encoding="utf-8-sig")
        header = sheet.splitlines()[0]
        assert header.strip() == "blind_id,mos,abx,note", header
        # 表头与正文都不该出现身份线索
        for token in ("sp0", "/", "3s", "10s", "30s", "60s",
                      "same", "cross", "emotion", "long", ".wav"):
            assert token not in sheet, f"评分表泄露了 {token!r} —— 盲听失效"

    def test_key_map_is_complete_and_one_to_one(self, tmp_path):
        _listen_results(tmp_path, "vb01")
        res = ve.listen("vb01", root=tmp_path)
        key = Path(res["key"]).read_text(encoding="utf-8-sig").splitlines()
        body = [ln for ln in key[1:] if ln.strip()]
        assert len(body) == res["n_items"] == 16
        blinds = [ln.split(",")[0] for ln in body]
        sids = [ln.split(",")[2] for ln in body]
        assert len(set(blinds)) == len(blinds), "盲号必须唯一"
        assert len(set(sids)) == len(sids), "一个样本只能有一个盲号"

    def test_playlist_order_matches_key_and_is_seeded(self, tmp_path):
        """播放清单顺序 = key 里的顺序，且同种子可复现。"""
        _listen_results(tmp_path, "vb01")
        r1 = ve.listen("vb01", root=tmp_path, seed=42)
        p1 = [ln.split("\t")[0] for ln in
              Path(r1["playlist"]).read_text(encoding="utf-8").splitlines()
              if ln and not ln.startswith("#")]
        k1 = [ln.split(",")[0] for ln in
              Path(r1["key"]).read_text(encoding="utf-8-sig").splitlines()[1:]
              if ln.strip()]
        assert p1 == k1, "播放清单与揭盲表的盲号顺序必须一致"

        r2 = ve.listen("vb01", root=tmp_path, seed=42)
        p2 = [ln.split("\t")[0] for ln in
              Path(r2["playlist"]).read_text(encoding="utf-8").splitlines()
              if ln and not ln.startswith("#")]
        assert p1 == p2, "同种子必须复现同一顺序"

    def test_order_is_not_identity_order(self, tmp_path):
        """随机化真的生效：顺序不等于按 sample_id 排序的结果。

        ★ 断言「不等」而不是「是某个特定排列」—— 只要种子固定，
        具体排列是实现细节，钉死它会让测试变成变更检测器。
        """
        _listen_results(tmp_path, "vb01")
        res = ve.listen("vb01", root=tmp_path, seed=20261005)
        key = [ln.split(",") for ln in
               Path(res["key"]).read_text(encoding="utf-8-sig").splitlines()[1:]
               if ln.strip()]
        sids = [k[2] for k in key]
        assert sids != sorted(sids), "顺序没被随机化（位置效应会混进条件效应）"

    def test_missing_truth_is_reported_not_faked(self, tmp_path):
        """缺 truth 的样本只能评 MOS —— 上报，不静默少给。"""
        base = _listen_results(tmp_path, "vb01")
        for cond in ("emotion", "long"):
            (base / "truth" / "sp0" / f"{cond}.wav").unlink()
        res = ve.listen("vb01", root=tmp_path)
        assert res["n_missing_truth"] == 8  # 2 条件 × 4 档
        assert res["n_paired"] == res["n_items"] - 8
        assert any("truth" in w for w in res["warnings"])

    def test_missing_audio_is_dropped_with_warning(self, tmp_path):
        """合成音频不存在 → 从评分表剔除并警告（不能生成一张放不出来的清单）。"""
        base = _listen_results(tmp_path, "vb01")
        # 删掉 sp0 的 3s 档四个音频 → 这 4 条要消失，其余 12 条留下
        for cond in ve.CONDITIONS:
            (base / "samples" / "sp0" / "3s" / f"{cond}.wav").unlink()
        res = ve.listen("vb01", root=tmp_path)
        assert res["n_dropped"] == 4
        assert res["n_items"] == 12
        assert any("音频文件不存在" in w for w in res["warnings"])

    def test_blank_path_counts_as_missing(self, tmp_path):
        """★ 空路径必须算「缺」。

        `Path("")` 会静默变成 `"."`，而 `"."` 是**存在**的 —— 于是"没有路径"
        会被判成"文件在"。本轮实测踩到这个坑（`_row` 的 path 是空串，
        `Path("").exists()` 返回 True），所以专门钉一条。
        """
        base = _listen_results(tmp_path, "vb01")
        data = json.loads((base / "results.json").read_text(encoding="utf-8"))
        for s in data["samples"]:
            s["path"] = ""
        (base / "results.json").write_text(json.dumps(data), encoding="utf-8")
        res = ve.listen("vb01", root=tmp_path)
        assert res["n_dropped"] == 16, "空路径被判成「文件存在」了"
        assert res["n_items"] == 0

    def test_empty_samples_is_error(self, tmp_path):
        """results.json 里没有 samples 字段 → 报「样本」阶段，不崩。"""
        base = _listen_results(tmp_path, "vb01")
        data = json.loads((base / "results.json").read_text(encoding="utf-8"))
        data.pop("samples", None)
        (base / "results.json").write_text(json.dumps(data), encoding="utf-8")
        res = ve.listen("vb01", root=tmp_path)
        assert res["error"] and res["error_stage"] == "样本"


class TestMergeListen:
    def _mk_sheets(self, tmp_path, res, n=2):
        """按揭盲表编号造 n 份评分表。"""
        import csv as _csv
        key = _read_csv(res["key"])
        out = []
        for i in range(n):
            p = tmp_path / f"listener{i}.csv"
            with open(p, "w", newline="", encoding="utf-8-sig") as f:
                w = _csv.DictWriter(f, fieldnames=["blind_id", "mos", "abx", "note"])
                w.writeheader()
                for j, r in enumerate(key):
                    w.writerow({"blind_id": r["blind_id"],
                                "mos": 3.0 + (j % 3) * 0.5 + i * 0.2,
                                "abx": 1 if j % 2 == 0 else 0,
                                "note": ""})
            out.append(p)
        return out

    def test_missing_key_is_error(self, tmp_path):
        res = ve.merge_listen("nope", [tmp_path / "a.csv"], root=tmp_path)
        assert res["error"] and res["error_stage"] == "揭盲表"

    def test_merge_produces_analyze_ready_csv(self, tmp_path):
        """★ 合并产出的 CSV 必须能被 analyze --subjective 直接吃。"""
        _listen_results(tmp_path, "vb01")
        res = ve.listen("vb01", root=tmp_path)
        sheets = self._mk_sheets(tmp_path, res)
        m = ve.merge_listen("vb01", sheets, root=tmp_path)
        assert m["error"] is None
        assert m["n_samples"] == 16 and m["n_listeners"] == 2

        # 用真 analyze 跑一遍：RQ3 必须出系数（而不是「主观评分行数 0」）
        out = ve.analyze("vb01", root=tmp_path, subjective=Path(m["out"]))
        assert out["rq3"]["subjective_rows"] == 16
        assert out["rq3"]["matched"] == 16
        assert out["rq3"]["correlations"].get("mos"), (
            "合并后的评分表没能让 RQ3 出系数 —— 列名契约断了")

    def test_listener_weight_is_equal_not_row_weight(self, tmp_path):
        """评得多的听者不该有更大权重。

        甲评了 2 条、乙评了 1 条同一批样本：均值应是 (甲均值 + 乙均值)/2，
        而不是把 3 个数字平摊。
        """
        import csv as _csv
        _listen_results(tmp_path, "vb01")
        res = ve.listen("vb01", root=tmp_path)
        key = _read_csv(res["key"])
        b1, b2 = key[0]["blind_id"], key[1]["blind_id"]

        a = tmp_path / "a.csv"
        with open(a, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(["blind_id", "mos", "abx"])
            w.writerow([b1, 5.0, 1])
            w.writerow([b2, 1.0, 0])
        b = tmp_path / "b.csv"
        with open(b, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(["blind_id", "mos", "abx"])
            w.writerow([b1, 1.0, 0])

        m = ve.merge_listen("vb01", [a, b], root=tmp_path)
        rows = {r["sample_id"]: r for r in _read_csv(m["out"])}
        sid1 = key[0]["sample_id"]
        # 甲给 5.0、乙给 1.0 → 等权听者 = 3.0（按行平摊会得到 3.67）
        assert float(rows[sid1]["mos"]) == pytest.approx(3.0, abs=0.01), (
            f"听者权重不对：{rows[sid1]['mos']}（等权应为 3.0）")

    def test_unmapped_blind_id_is_reported(self, tmp_path):
        import csv as _csv
        _listen_results(tmp_path, "vb01")
        ve.listen("vb01", root=tmp_path)
        p = tmp_path / "wrong.csv"
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(["blind_id", "mos", "abx"])
            w.writerow(["S-9999", 4.0, 1])
        m = ve.merge_listen("vb01", [p], root=tmp_path)
        assert len(m["unmapped"]) == 1
        assert any("查不到" in w for w in m["warnings"])

    def test_invalid_values_are_dropped_not_clamped(self, tmp_path):
        """非法 mos（如 9 或 abc）剔除并报告 —— **不夹到 5**。

        悄悄夹紧会让一条填错的数据混进相关性分析，而且看不出来。

        ★ 字段是**各自独立**校验的：mos 非法但 abx 合法时，该行仍以 abx 存活。
        这不是宽容，是正确的 —— 丢掉整行等于把听者一条有效判断也删了。
        所以这里断言的是「非法 mos **没有**进入输出」，而不是「整行消失」。
        """
        import csv as _csv
        _listen_results(tmp_path, "vb01")
        res = ve.listen("vb01", root=tmp_path)
        key = _read_csv(res["key"])
        p = tmp_path / "bad.csv"
        with open(p, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(["blind_id", "mos", "abx"])
            w.writerow([key[0]["blind_id"], 9.0, 1])      # mos 超上限，abx 合法
            w.writerow([key[1]["blind_id"], "abc", ""])   # mos 非数字，abx 空 → 整行无有效值
            w.writerow([key[2]["blind_id"], 4.0, 7])      # abx 非 0/1，mos 合法
        m = ve.merge_listen("vb01", [p], root=tmp_path)
        assert len(m["invalid"]) == 3, m["invalid"]
        rows = {r["sample_id"]: r for r in _read_csv(m["out"])}
        # 整行无有效值的样本不该出现
        assert key[1]["sample_id"] not in rows
        # 非法 mos 不得被夹成 5.0 混进来
        assert rows[key[0]["sample_id"]]["mos"] == ""
        assert float(rows[key[0]["sample_id"]]["abx"]) == pytest.approx(1.0)
        # mos 合法、abx 非法 → mos 保留、abx 留空
        assert float(rows[key[2]["sample_id"]]["mos"]) == pytest.approx(4.0)
        assert rows[key[2]["sample_id"]]["abx"] == ""

    def test_single_listener_warns_about_consistency(self, tmp_path):
        _listen_results(tmp_path, "vb01")
        res = ve.listen("vb01", root=tmp_path)
        sheets = self._mk_sheets(tmp_path, res, n=1)
        m = ve.merge_listen("vb01", sheets, root=tmp_path)
        assert any("只有 1 名听者" in w for w in m["warnings"])

    def test_output_is_sorted_for_diffability(self, tmp_path):
        """排序后落盘：同输入 → 同文件，diff 才有意义。"""
        _listen_results(tmp_path, "vb01")
        res = ve.listen("vb01", root=tmp_path)
        sheets = self._mk_sheets(tmp_path, res)
        m1 = ve.merge_listen("vb01", sheets, root=tmp_path)
        first = Path(m1["out"]).read_text(encoding="utf-8-sig")
        m2 = ve.merge_listen("vb01", list(reversed(sheets)), root=tmp_path)
        assert Path(m2["out"]).read_text(encoding="utf-8-sig") == first, (
            "评分表顺序变了输出就变 —— 落盘前排序失效")

    def test_missing_sheet_is_noted_not_crashed(self, tmp_path):
        _listen_results(tmp_path, "vb01")
        res = ve.listen("vb01", root=tmp_path)
        sheets = self._mk_sheets(tmp_path, res, n=1)
        m = ve.merge_listen("vb01", sheets + [tmp_path / "ghost.csv"], root=tmp_path)
        assert m["error"] is None
        assert any("不存在" in n for n in m["notes"])
