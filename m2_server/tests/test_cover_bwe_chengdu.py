# -*- coding: utf-8 -*-
"""`experiments/cover_bwe_chengdu.py` 的结构守卫与纯函数/集成测试。

守五件跑之前看不出来、跑完也看不出来的事：

1. **实验脚本在主 venv 里 import 成功**（universr/torch 不许到模块级 —— 模型只能在
   独立 venv 的子进程里跑，这条硬边界与 `test_cover_bwe_universr.py` 同源）。
2. **A 臂必须与产品后期版同段逐样本一致**：这是"没有暗换变量"的唯一硬证据。
   自检失败必须 SystemExit，不能只 print（print 会被当成噪声读过去）。
3. **seed 必须显式传给 enhance()**：不固定就不是单变量（每次新噪声）。
4. **臂清单与臂产物同一次写**（10-18 教训：arms.json 漏存 → 页面 KeyError）。
5. **页面四行必须在表里**：少一行 = 页面静默少显示一条路，而页面"看起来正常"。

集成测试用**全合成音频**（不读 media/、不起 ffmpeg、不跑模型）：
`bwe.enhance` / `hfb._to_40k_mono` / `apply_filters` / `hfb.metrics` / `_embed_mp3`
全部 monkeypatch，仍然覆盖 run() 的真实控制流与自检逻辑。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

_ROOT = Path(__file__).resolve().parents[2]
_EXP = _ROOT / "experiments"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _EXP / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cd = _load("cover_bwe_chengdu_under_test", "cover_bwe_chengdu.py")


# ---------------------------------------------------------------- 结构守卫

def test_experiment_imports_without_the_model_env():
    """主 venv import 成功 = 模块级没有 universr/torch。"""
    import ast

    tops = [n for n in ast.parse((_EXP / "cover_bwe_chengdu.py").read_text(encoding="utf-8")).body
            if isinstance(n, (ast.Import, ast.ImportFrom))]
    names: set[str] = set()
    for n in tops:
        names |= {a.name.split(".")[0] for a in n.names}
    assert "universr" not in names and "torch" not in names, names


def test_rows_and_arm_files_agree():
    """页面四行必须在臂表里、文件名互不重复（少一行 = 静默少显示一条路）。"""
    keys = [k for k, _, _ in cd.ROWS]
    assert set(keys) <= set(cd.ARM_FILES), set(keys) - set(cd.ARM_FILES)
    vals = list(cd.ARM_FILES.values())
    assert len(set(vals)) == len(vals), vals
    for k in ("00_source", "A_dry", "D1_bwe", "D2_bwe_no_air"):
        assert k in cd.ARM_FILES, k


def test_seed_is_a_fixed_int():
    """seed=None = 每次新噪声 = A/B 不是单变量。"""
    assert isinstance(cd.SEED, int) and cd.SEED == 1234


def test_product_chain_matches_chengdu_arms_json():
    """PRODUCT_CHAIN 必须逐字复现《成都》arms.json 里记的产品后期链。

    否则 A 臂自检（vs 产品后期版）会在真实数据上失败——那是变量被暗换的信号。
    """
    chain = cd.post_filters(**cd.PRODUCT_CHAIN)
    assert "treble=g=6:f=7000" in chain, chain
    assert "deesser=i=0.4:m=0.5:f=0.5" in chain, chain
    assert "acompressor=threshold=-18dB:ratio=2.5:attack=12:release=180:makeup=2dB" in chain, chain


def test_d2_removes_only_the_air_eq():
    """D1/D2 的唯一差别是 +6dB@7k（否则两臂不再是单变量）。"""
    d1 = cd.post_filters(**cd.PRODUCT_CHAIN)
    d2 = cd.post_filters(**{**cd.PRODUCT_CHAIN, "air_db": 0.0})
    assert "treble" in d1 and "treble" not in d2
    assert d2 in d1.replace("treble=g=6:f=7000,", "")


# ---------------------------------------------------------------- 纯函数

def _synth_f0(total_s: float = 140.0, frame_hz: float = 100.0):
    """0–60s 静音；60–100s 浊音 200Hz；100–140s 浊音 350Hz。"""
    n = int(total_s * frame_hz)
    t = np.arange(n) / frame_hz
    f0 = np.full(n, np.nan)
    vflag = np.zeros(n, dtype=bool)
    m1 = (t >= 60) & (t < 100)
    m2 = (t >= 100) & (t < 140)
    f0[m1], vflag[m1] = 200.0, True
    f0[m2], vflag[m2] = 350.0, True
    return f0, vflag


def test_pick_excerpt_prefers_target_window_and_skips_high_and_silent():
    f0, vflag = _synth_f0()
    best = cd.pick_excerpt(f0, vflag, frame_hz=100.0, target_hz=183.8, seconds=20.0)
    assert best["start"] == 60.0, best
    assert best["f0_median_hz"] == pytest.approx(200.0, abs=1.0)
    assert best["frac_ge300"] == 0.0


def test_pick_excerpt_rejects_when_everything_is_too_high():
    f0, vflag = _synth_f0()
    f0[f0 == 200.0] = 400.0  # 连"低"的那段也推到高音区
    with pytest.raises(ValueError, match="找不到"):
        cd.pick_excerpt(f0, vflag, frame_hz=100.0, target_hz=183.8, seconds=20.0)


def test_pick_excerpt_is_deterministic():
    f0, vflag = _synth_f0()
    a = cd.pick_excerpt(f0, vflag, frame_hz=100.0, target_hz=183.8, seconds=20.0)
    b = cd.pick_excerpt(f0, vflag, frame_hz=100.0, target_hz=183.8, seconds=20.0)
    assert a == b


def test_cut_excerpt_exact_samples_and_bounds():
    y = np.arange(100, dtype=np.float32)
    got = cd.cut_excerpt(y, sr=10, start=1.5, seconds=2.0)
    assert got.shape == (20,)
    assert got[0] == 15 and got[-1] == 34
    with pytest.raises(ValueError, match="越界"):
        cd.cut_excerpt(y, sr=10, start=9.5, seconds=2.0)


def test_excerpt_stats_on_synthetic_singing():
    sr = 16000
    t = np.arange(int(2.0 * sr)) / sr
    y = 0.5 * np.sin(2 * np.pi * 183.0 * t) + 0.05 * np.random.default_rng(0).standard_normal(len(t))
    s = cd.excerpt_stats(y.astype(np.float32), sr)
    assert s["voiced"] > 0.9
    assert s["f0_median_hz"] == pytest.approx(183.0, abs=8.0)
    assert s["frac_ge300"] == 0.0


# ---------------------------------------------------------------- 集成（全合成）

def _write_wav(path: Path, y: np.ndarray, sr: int) -> Path:
    sf.write(str(path), np.asarray(y, dtype=np.float32), sr, subtype="PCM_16")
    return path


def _fake_chain(y: np.ndarray, sr: int, filters: str) -> np.ndarray:
    """合成版后期链：有 treble 就 ×1.1（让 D1/D2 可区分），否则原样。"""
    return y * (1.1 if "treble" in filters else 1.0)


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """把 cd 的输入/输出/外部依赖全部接到 tmp_path + 假实现上。"""
    sr = 40000
    rng = np.random.default_rng(7)
    t = np.arange(int(30.0 * sr)) / sr
    dry = (0.4 * np.sin(2 * np.pi * 183.0 * t)
           + 0.1 * np.sin(2 * np.pi * 366.0 * t)
           + 0.02 * rng.standard_normal(len(t))).astype(np.float32)
    dry_path = _write_wav(tmp_path / "dry.wav", dry, sr)
    # 产品后期版 = 同一份干声过同一条链（自检的对照物）
    post_path = _write_wav(tmp_path / "post.wav",
                           _fake_chain(dry, sr, cd.post_filters(**cd.PRODUCT_CHAIN)), sr)
    vocal_path = _write_wav(tmp_path / "vocals.wav", dry, 44100)

    monkeypatch.setattr(cd, "DRY_SRC", dry_path)
    monkeypatch.setattr(cd, "POST_SRC", post_path)
    monkeypatch.setattr(cd, "VOCAL_SRC", vocal_path)
    monkeypatch.setattr(cd, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(cd, "REF", tmp_path / "ref.wav")

    calls: dict = {}

    def fake_enhance(src: Path, dst: Path, *, input_sr: int, seed: int) -> dict:
        calls["enhance"] = {"input_sr": input_sr, "seed": seed}
        y, s = sf.read(str(src), dtype="float32")
        sf.write(str(dst), y, s, subtype="PCM_16")
        return {"seconds_in": 28.0, "load_s": 0.1, "run_s": 1.0,
                "ode": "midpoint", "seed": seed, "peak": 0.5}

    def fake_to_40k_mono(src: Path, dst: Path, *, start: float = 0.0, seconds: float = 0.0) -> Path:
        """合成版：切段 + 线性重采样到 40k（真实现走 ffmpeg，测试不依赖它）。"""
        y, s = sf.read(str(src), dtype="float32", always_2d=True)
        y = y.mean(1)
        seg = y[int(round(start * s)): int(round((start + seconds) * s))] if seconds else y
        n_out = int(round(len(seg) * 40000 / s))
        t_out = np.arange(n_out) / 40000
        t_in = np.arange(len(seg)) / s
        sf.write(str(dst), np.interp(t_out, t_in, seg).astype(np.float32),
                 40000, subtype="PCM_16")
        return dst

    def fake_apply(src: Path, dst: Path, filters: str) -> None:
        y, s = sf.read(str(src), dtype="float32")
        calls.setdefault("chains", []).append(filters)
        sf.write(str(dst), _fake_chain(y, s, filters), s, subtype="PCM_16")

    monkeypatch.setattr(cd.bwe, "enhance", fake_enhance)
    monkeypatch.setattr(cd.hfb, "_to_40k_mono", fake_to_40k_mono)
    monkeypatch.setattr(cd, "apply_filters", fake_apply)
    monkeypatch.setattr(cd, "_embed_mp3", lambda p: "data:audio/mpeg;base64,AAAA")
    monkeypatch.setattr(cd.hfb, "metrics",
                        lambda p, ref=None: {"hf_rel0_4k_db": -50.0, "mid_rel0_4k_db": -30.0,
                                             "centroid_hz": 800, "rolloff95_hz": 3000,
                                             "slope_1k_8k": -20.0, "hf_mod_cv": 1.4, "cos": 0.27})
    return calls


def test_run_writes_all_arms_with_selfcheck_and_seed(wired, tmp_path):
    out = tmp_path / "out"
    arms = cd.run(out, start=5.0, seconds=10.0)
    assert set(arms) == set(cd.ARM_FILES)
    for p in arms.values():
        assert p.exists(), p
        info = sf.info(str(p))
        assert info.samplerate == 40000 and info.frames == int(10.0 * 40000)
    meta = json.loads((out / "arms.json").read_text(encoding="utf-8"))
    assert set(meta["arms"]) == set(cd.ARM_FILES)  # 臂清单与臂产物同一次写
    assert meta["seed"] == 1234 and meta["input_sr"] == 16000
    assert meta["selfcheck"]["tier"] == "逐样本一致"
    assert meta["selfcheck"]["max_abs_diff"] < cd.SELFCHECK_TOL
    assert wired["enhance"] == {"input_sr": 16000, "seed": 1234}
    # D1 有 treble、D2 没有 → 两者必须不同（单变量差的是空气 EQ）
    d1, _ = sf.read(str(arms["D1_bwe"]), dtype="float32")
    d2, _ = sf.read(str(arms["D2_bwe_no_air"]), dtype="float32")
    assert float(np.max(np.abs(d1 - d2))) > 1e-3
    # A 臂必须与产品后期版同段一致（自检真的在跑，不是装饰）
    a, _ = sf.read(str(arms["A_dry"]), dtype="float32")
    prod, _ = sf.read(str(cd.POST_SRC), dtype="float32")
    ref_exc = cd.cut_excerpt(prod, 40000, 5.0, 10.0)
    assert float(np.max(np.abs(a - ref_exc))) == pytest.approx(0.0, abs=1e-4)


def test_run_selfcheck_failure_is_fatal(wired, tmp_path):
    """产品后期版被换成别的东西 → 必须 SystemExit（不能只 print）。"""
    y, s = sf.read(str(cd.POST_SRC), dtype="float32")
    sf.write(str(cd.POST_SRC), y * 0.5, s, subtype="PCM_16")
    with pytest.raises(SystemExit, match="不一致"):
        cd.run(tmp_path / "out", start=5.0, seconds=10.0)


def test_run_rejects_start_below_preroll(wired, tmp_path):
    """切段起点 < 预热前导 → 直接拒绝（否则前导会切成负索引/静默截短）。"""
    with pytest.raises(SystemExit, match="预热前导"):
        cd.run(tmp_path / "out", start=1.0, seconds=10.0)


def test_run_selfcheck_accepts_implementation_level_difference(wired, tmp_path):
    """产品后期版与本地复现存**滤波器实现级**差异（噪声 0.02rms）→ 判"等效一致"放行。

    这就是 2026-10-19 真实踩到的：app 管线的后期链与本机 ffmpeg 复现差 0.027/corr
    0.99995 —— 逐样本卡死会把它误判成"暗换变量"，所以自检必须是两级。
    """
    y, s = sf.read(str(cd.POST_SRC), dtype="float32")
    rng = np.random.default_rng(3)
    # 实现级差异的量级：微增益差 + 微噪声（真实案例 0.027/corr 0.99995 的合成版）
    noisy = (y * 0.999 + rng.standard_normal(len(y)).astype(np.float32) * 0.001).clip(-1, 1)
    sf.write(str(cd.POST_SRC), noisy, s, subtype="PCM_16")
    out = tmp_path / "out"
    cd.run(out, start=5.0, seconds=10.0)
    sc = json.loads((out / "arms.json").read_text(encoding="utf-8"))["selfcheck"]
    assert sc["tier"] == "等效一致（滤波器实现级差异）"
    assert sc["max_abs_diff"] > cd.SELFCHECK_TOL and sc["max_abs_diff"] <= 0.05
    assert sc["corr"] >= 0.999


def test_build_page_shows_every_row(wired, tmp_path):
    out = tmp_path / "out"
    cd.run(out, start=5.0, seconds=10.0)
    page = cd.build_page(out)
    html = page.read_text(encoding="utf-8")
    for _, title, _ in cd.ROWS:
        assert title in html, title
    assert "缺文件" not in html
    assert "<audio" in html
    report = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
    assert set(report) == set(cd.ARM_FILES)
    assert "hf_gap_vs_src_db" in report["D1_bwe"]
