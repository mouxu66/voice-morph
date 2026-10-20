# -*- coding: utf-8 -*-
"""`experiments/cover_bwe_chengdu_full.py`（整曲五臂 + 伴奏）的结构与逻辑守卫。

守六件"跑之前看不出来、跑完也看不出来"的事：

1. **混音命令必须与产品逐字一致**（`cover_api._mix_once`）：这是"四臂共用产品口径"
   的唯一硬证据。抄错一个 `normalize=0` 就会把人声压掉一半，而**产物听起来只是
   '人声小一点'**，不会报错。
2. **混音增益必须从产品 `arms.json` 读，缺字段要炸**：静默回落 1.0 会让四臂都变成
   "人声太小"，被误判成音色问题。
3. **自检失败必须 SystemExit**（① 与产品 post / ② 与产品 mix）：print 会被当噪声。
4. **两遍防削波的容差**：产品记录的增益已缩过，重跑探测会读到 0.9800x ——
   不加容差每次再缩 0.0001 并误报"超顶已缩"。
5. **页面不许内嵌 base64**：整曲九条 × 6.8MB 会把浏览器打死（切片版才内嵌）。
6. **整曲版不许有预热前导**：产品 post 是全曲跑的，加前导反而破坏单变量。

集成测试用全合成音频，`bwe.enhance` / `apply_filters` / `_to_mp3` / `hfb.metrics`
全部 monkeypatch，不读 media/、不起 ffmpeg、不跑模型。
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
_FULL_SRC = _EXP / "cover_bwe_chengdu_full.py"
_API_SRC = _ROOT / "m2_server" / "cover_api.py"

_MIX_FILTER = ("[0:a]volume={vg}[v];[1:a]volume={ag}[a];"
               "[v][a]amix=inputs=2:duration=longest:normalize=0")


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _EXP / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


full = _load("cover_bwe_chengdu_full_under_test", "cover_bwe_chengdu_full.py")


# ---------------------------------------------------------------- 结构守卫

def test_module_imports_without_model_env():
    """主 venv import 成功 = 模块级没有 universr/torch/timm。"""
    import ast

    tops = [n for n in ast.parse(_FULL_SRC.read_text(encoding="utf-8")).body
            if isinstance(n, (ast.Import, ast.ImportFrom))]
    names: set[str] = set()
    for n in tops:
        names |= {a.name.split(".")[0] for a in n.names}
    for banned in ("universr", "torch", "timm", "torchdiffeq"):
        assert banned not in names, names


def test_no_preroll_constant_in_full_version():
    """整曲版不许有预热前导：产品 post 是全曲跑链的，起点一致才可比。"""
    assert not hasattr(full, "PRE_ROLL_S")
    src = _FULL_SRC.read_text(encoding="utf-8")
    assert "预热前导 + 切段" not in src  # 切片版那句注释不许被抄过来
    assert "cut_excerpt" not in src


def test_mix_filter_matches_product_verbatim():
    """`_mix_once` 的滤镜串必须与 `cover_api` 逐字相同（含 normalize=0）。

    `normalize=0` 是**相加**；默认的 normalize=1 会按输入数除权 → 人声掉 6dB，
    产物只是"听起来人声小"，不报错。
    """
    assert _MIX_FILTER in _FULL_SRC.read_text(encoding="utf-8")
    assert _MIX_FILTER in _API_SRC.read_text(encoding="utf-8")


def test_mix_ceiling_matches_product():
    api = _API_SRC.read_text(encoding="utf-8")
    assert "MIX_CEILING = 0.98" in api
    assert full.MIX_CEILING == 0.98


def test_arm_and_mix_tables_cover_the_same_four_arms():
    """干声臂与混音臂必须一一对应 —— 少一条就是页面上少一个"带伴奏"的按钮。"""
    assert set(full.ARM_FILES) == set(full.MIX_FILES) == {
        "A_dry", "D1_bwe", "D2_bwe_no_air", "D3_bwe_tilted"}
    assert len(set(full.ARM_FILES.values())) == 4
    assert len(set(full.MIX_FILES.values())) == 4


def test_rows_list_all_five_listening_targets():
    keys = [k for k, _, _ in full.ROWS]
    assert keys == ["orig", "A_dry", "D1_bwe", "D2_bwe_no_air", "D3_bwe_tilted"], keys
    assert set(keys[1:]) == set(full.ARM_FILES)


def test_output_dir_is_under_media_so_it_stays_out_of_git():
    """产物必须落在 media/ 下（.gitignore 挡住）；整曲 wav 上 GB，进仓库就是事故。"""
    assert (full.OUT_DIR.parent.name).startswith("midsong")
    assert full.OUT_DIR.parent.parent.parent.name == "media"


# ---------------------------------------------------------------- 增益读取

def _arms_json(tmp_path: Path, **over) -> Path:
    node = {"pitch": 0, "vocal_gain": 1.7232, "acc_gain": 0.6977}
    node.update(over)
    p = tmp_path / "arms.json"
    p.write_text(json.dumps({"_meta": {"song": "x"}, "P0_原调": node},
                            ensure_ascii=False), encoding="utf-8")
    return p


def test_mix_gains_reads_product_values(tmp_path):
    assert full.mix_gains_from_arms(_arms_json(tmp_path)) == (1.7232, 0.6977)


def test_mix_gains_missing_field_raises(tmp_path):
    """缺 acc_gain 必须炸 —— 猜 1.0 会让四臂整体变"人声太小"。"""
    p = tmp_path / "arms.json"
    p.write_text(json.dumps({"P0_原调": {"vocal_gain": 1.7}}, ensure_ascii=False),
                 encoding="utf-8")
    with pytest.raises(KeyError, match="acc_gain"):
        full.mix_gains_from_arms(p)


def test_mix_gains_missing_arm_raises(tmp_path):
    with pytest.raises(KeyError, match="没有臂"):
        full.mix_gains_from_arms(_arms_json(tmp_path), arm="不存在的臂")


def test_mix_gains_rejects_non_positive(tmp_path):
    with pytest.raises(ValueError, match="增益非正"):
        full.mix_gains_from_arms(_arms_json(tmp_path, vocal_gain=0.0))


# ---------------------------------------------------------------- compare 三级判定

def _wav(path: Path, y: np.ndarray, sr: int = 40000) -> Path:
    sf.write(str(path), np.asarray(y, dtype=np.float32), sr, subtype="PCM_16")
    return path


def test_compare_identical_is_sample_exact(tmp_path):
    rng = np.random.default_rng(1)
    y = (rng.standard_normal(4000) * 0.2).astype(np.float32)
    a = _wav(tmp_path / "a.wav", y)
    b = _wav(tmp_path / "b.wav", y)
    r = full.compare(a, b)
    assert r["ok"] and r["tier"] == "逐样本一致"
    assert r["max_abs_diff"] <= full.SELFCHECK_TOL


def test_compare_implementation_level_difference_is_equivalent(tmp_path):
    """模拟真实差异（×0.999 + 0.001 噪声，实测 0.032/corr 0.99993）→ 等效一致。"""
    rng = np.random.default_rng(2)
    y = (0.4 * np.sin(np.linspace(0, 200, 40000))).astype(np.float32)
    a = _wav(tmp_path / "a.wav", y)
    b = _wav(tmp_path / "b.wav", (y * 0.999 + rng.standard_normal(len(y)) * 0.001).clip(-1, 1))
    r = full.compare(a, b)
    assert r["ok"] and r["tier"] == "等效一致"


def test_compare_gain_swap_is_rejected(tmp_path):
    """换增益（×1.05）必须判不一致 —— 峰值差 5% 超过 2% 容差。"""
    y = (0.4 * np.sin(np.linspace(0, 200, 40000))).astype(np.float32)
    a = _wav(tmp_path / "a.wav", y)
    b = _wav(tmp_path / "b.wav", y * 1.05)
    r = full.compare(a, b)
    assert not r["ok"] and r["tier"] == "不一致"


def test_compare_length_mismatch_is_rejected(tmp_path):
    a = _wav(tmp_path / "a.wav", np.zeros(4000, np.float32))
    b = _wav(tmp_path / "b.wav", np.zeros(3990, np.float32))
    assert not full.compare(a, b)["ok"]


def test_compare_different_samplerate_is_reported(tmp_path):
    a = _wav(tmp_path / "a.wav", np.zeros(4000, np.float32), 40000)
    b = _wav(tmp_path / "b.wav", np.zeros(4000, np.float32), 44100)
    r = full.compare(a, b)
    assert r["tier"] == "采样率不同" and not r["ok"]


# ---------------------------------------------------------------- 两遍防削波

def _fake_mix_once(vocals: Path, accomp: Path, gains, out: Path, *, float_out=False):
    v, _ = sf.read(str(vocals), dtype="float32")
    a, _ = sf.read(str(accomp), dtype="float32")
    n = max(len(v), len(a))
    y = np.zeros(n, np.float32)
    y[:len(v)] += v * gains[0]
    y[:len(a)] += a * gains[1]
    sf.write(str(out), np.stack([y, y], 1), 44100,
             subtype="FLOAT" if float_out else "PCM_16")


def test_mix_track_does_not_rescale_when_already_at_ceiling(tmp_path, monkeypatch):
    """产品增益本身已是缩放后的 → 重跑探测读到 0.9800x，不许再缩、不许误报。"""
    monkeypatch.setattr(full, "_mix_once", _fake_mix_once)
    monkeypatch.setattr(full, "_true_peak", lambda p: 0.98001)
    v = _wav(tmp_path / "v.wav", np.full(4000, 0.1, np.float32))
    a = _wav(tmp_path / "a.wav", np.full(4000, 0.1, np.float32))
    rep = full.mix_track(v, a, tmp_path / "m.wav", (1.7232, 0.6977))
    assert rep["scaled"] is False
    assert rep["applied"] == [1.7232, 0.6977]


def test_mix_track_scales_when_truly_over_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(full, "_mix_once", _fake_mix_once)
    monkeypatch.setattr(full, "_true_peak", lambda p: 1.20)
    v = _wav(tmp_path / "v.wav", np.full(4000, 0.1, np.float32))
    a = _wav(tmp_path / "a.wav", np.full(4000, 0.1, np.float32))
    rep = full.mix_track(v, a, tmp_path / "m.wav", (2.0, 1.0))
    assert rep["scaled"] is True
    assert rep["applied"][0] == pytest.approx(2.0 * 0.98 / 1.20, abs=1e-4)
    assert rep["applied"][1] == pytest.approx(1.0 * 0.98 / 1.20, abs=1e-4)


# ---------------------------------------------------------------- 集成（全合成）

def _fake_chain(y: np.ndarray, filters: str) -> np.ndarray:
    return y * (1.1 if "treble" in filters else 1.0)


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    sr = 40000
    rng = np.random.default_rng(7)
    t = np.arange(int(12.0 * sr)) / sr
    dry = (0.30 * np.sin(2 * np.pi * 183.0 * t)
           + 0.05 * rng.standard_normal(len(t))).astype(np.float32)
    dry_path = _wav(tmp_path / "dry.wav", dry, sr)
    post_path = _wav(tmp_path / "post.wav", _fake_chain(dry, full.post_filters(**full.PRODUCT_CHAIN)), sr)
    accomp_path = _wav(tmp_path / "accomp.wav", (0.08 * np.sin(2 * np.pi * 110 * t)).astype(np.float32), sr)
    vocal_path = _wav(tmp_path / "vocals.wav", dry, sr)
    # 产品混合版 = 同一份 A 臂按产品增益相加（fake_mix_once 的语义）
    a_post = _fake_chain(dry, full.post_filters(**full.PRODUCT_CHAIN))
    acc = (0.08 * np.sin(2 * np.pi * 110 * t)).astype(np.float32)
    mix_src = tmp_path / "mix.wav"
    sf.write(str(mix_src), np.stack([a_post * 1.7232 + acc * 0.6977] * 2, 1), 44100, subtype="PCM_16")

    monkeypatch.setattr(full, "DRY_SRC", dry_path)
    monkeypatch.setattr(full, "POST_SRC", post_path)
    monkeypatch.setattr(full, "MIX_SRC", mix_src)
    monkeypatch.setattr(full, "ACCOMP", accomp_path)
    monkeypatch.setattr(full, "VOCAL_SRC", vocal_path)
    monkeypatch.setattr(full, "ORIG_MP3", _wav(tmp_path / "orig.wav", dry, sr))
    monkeypatch.setattr(full, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(full, "ARMS_JSON", _arms_json(tmp_path))
    monkeypatch.setattr(full, "REF", tmp_path / "ref.wav")
    monkeypatch.setattr(full, "_mix_once", _fake_mix_once)
    monkeypatch.setattr(full, "_true_peak", lambda p: 0.60)

    calls: dict = {}

    def fake_enhance(src: Path, dst: Path, *, input_sr: int, seed: int) -> dict:
        calls["enhance"] = {"input_sr": input_sr, "seed": seed}
        y, s = sf.read(str(src), dtype="float32")
        hf = 0.03 * np.random.default_rng(5).standard_normal(len(y)).astype(np.float32)
        sf.write(str(dst), y + hf, s, subtype="PCM_16")
        return {"seconds_in": 12.0, "load_s": 0.1, "run_s": 1.0, "ode": "midpoint",
                "seed": seed, "peak": 0.5, "chunks": 3, "chunk_s": 5.0}

    def fake_apply(src: Path, dst: Path, filters: str) -> None:
        y, s = sf.read(str(src), dtype="float32")
        calls.setdefault("chains", []).append(filters)
        sf.write(str(dst), _fake_chain(y, filters), s, subtype="PCM_16")

    def fake_to_40k(src: Path, dst: Path) -> Path:
        y, s = sf.read(str(src), dtype="float32")
        sf.write(str(dst), y, 40000, subtype="PCM_16")
        return dst

    def fake_to_40k_mono(src: Path, dst: Path, *, start=0.0, seconds=0.0) -> Path:
        y, s = sf.read(str(src), dtype="float32")
        sf.write(str(dst), y, 40000, subtype="PCM_16")
        return dst

    monkeypatch.setattr(full.bwe, "enhance", fake_enhance)
    monkeypatch.setattr(full.bwe, "_to_40k", fake_to_40k)
    monkeypatch.setattr(full.hfb, "_to_40k_mono", fake_to_40k_mono)
    monkeypatch.setattr(full, "apply_filters", fake_apply)
    monkeypatch.setattr(full, "_to_mp3", lambda src, dst, bitrate="128k": dst)
    monkeypatch.setattr(full.hfb, "metrics",
                        lambda p, ref=None: {"hf_rel0_4k_db": -50.0, "mid_rel0_4k_db": -30.0,
                                             "centroid_hz": 800, "rolloff95_hz": 3000,
                                             "slope_1k_8k": -20.0, "hf_mod_cv": 1.4, "cos": 0.27})
    return calls


def test_run_full_writes_four_arms_and_four_mixes(wired, tmp_path):
    out = tmp_path / "out"
    arms = full.run(out)
    assert set(arms) == set(full.ARM_FILES)
    for p in arms.values():
        assert sf.info(str(p)).samplerate == 40000
    meta = json.loads((out / "arms.json").read_text(encoding="utf-8"))
    assert meta["scope"] == "full_song"
    assert meta["mix_gains"] == {"vocal": 1.7232, "acc": 0.6977}
    assert set(meta["mix_report"]) == set(full.MIX_FILES)
    assert meta["selfcheck_post"]["ok"] and meta["selfcheck_mix"]["ok"]
    assert meta["tilt"]["db_per_oct"] == full.TILT_DB_PER_OCT
    assert wired["enhance"] == {"input_sr": 16000, "seed": 1234}
    # BWE 只跑一次（三臂共享），后期链跑四次（A/D1/D2/D3）
    assert len(wired["chains"]) == 4
    assert sum("treble" in c for c in wired["chains"]) == 3


def test_run_fails_when_product_post_differs(wired, tmp_path):
    y, s = sf.read(str(full.POST_SRC), dtype="float32")
    sf.write(str(full.POST_SRC), y * 0.5, s, subtype="PCM_16")
    with pytest.raises(SystemExit, match="自检①"):
        full.run(tmp_path / "out")


def test_run_fails_when_product_mix_differs(wired, tmp_path):
    """混音口径被暗换（例如改成各自 auto 配平）→ 必须炸，不能"听起来还行"就放行。"""
    y, s = sf.read(str(full.MIX_SRC), dtype="float32")
    sf.write(str(full.MIX_SRC), y * 0.5, s, subtype="PCM_16")
    with pytest.raises(SystemExit, match="自检②"):
        full.run(tmp_path / "out")


def test_page_has_location_sync_and_no_base64(wired, tmp_path):
    """整曲页必须靠位置同步 + 外部 mp3；内嵌 base64 会让页面涨到 60MB。"""
    out = tmp_path / "out"
    full.run(out)
    page = full.build_page(out)
    html = page.read_text(encoding="utf-8")
    for _, title, _ in full.ROWS:
        assert title in html, title
    assert "缺文件" not in html
    assert "data:audio" not in html, "整曲页不许内嵌 base64"
    assert "id=\"sync\"" in html and "保持播放位置" in html
    assert "audio/" in html
    assert "不做每臂自动配平" in html
