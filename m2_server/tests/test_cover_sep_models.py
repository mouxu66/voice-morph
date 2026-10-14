# -*- coding: utf-8 -*-
"""`experiments/cover_sep_models.py` 与 `cover_sep_ruler_check.py` 的守卫。

守什么（这份实验最危险的失效方式：**尺子失灵被读成"没差异"**）：
    `cover_sep_models` 的结论是"两个模型差不多"。而"差不多"有两种来源 ——
    ① 它们真的差不多；② **判据分辨不出差异**。这两种在报告里长得一模一样。
    所以 `cover_sep_ruler_check.py` 用**已知不同**的一对（整首歌 vs 分离出的人声）
    给尺子定分辨率下界。本文件把这条对照钉住：
      · 尺子必须能在已知不同的两例上给出**大幅**差异（否则"没差异"的结论无效）；
      · `frame_db` 必须真的量出能量差（正反两条）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "m2_server", _ROOT / "experiments"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def csm():
    return _load("cover_sep_models_t", _ROOT / "experiments" / "cover_sep_models.py")


def _wav(path: Path, amp: float, secs: float = 1.0, sr: int = 44100) -> Path:
    t = np.arange(int(sr * secs)) / sr
    mono = (amp * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    sf.write(str(path), np.stack([mono, mono], axis=1), sr, subtype="PCM_16")
    return path


def test_frame_db_measures_level_difference(csm, tmp_path):
    """★ 尺子的正向：两个相差 20 dB 的信号必须被量出 ~20 dB。

    这条防的是"尺子把任何输入都量成同一档"（那样"没差异"就永远是假结论）。
    """
    loud = csm.frame_db(_wav(tmp_path / "loud.wav", 0.5))
    quiet = csm.frame_db(_wav(tmp_path / "quiet.wav", 0.05))   # −20 dB
    diff = float(loud.mean() - quiet.mean())
    assert diff == pytest.approx(20.0, abs=0.5), f"量出 {diff:.2f} dB，期望 ≈20"


def test_frame_db_reports_very_low_for_near_silence(csm, tmp_path):
    """反向：安静信号要按真实幅度量，不被**人为地板**抬起来。

    ⚠️ 别拿"PCM_16 + 极小幅度"造静音：16 位量化会把任何小于半个 LSB 的信号
    钉到 **1 LSB ≈ −93.3 dBFS**（2026-10-14 实测：amp=1e-6 写出后绝对值最大
    恰好 3.052e-05 = 1 LSB）。那是**文件格式的下限，不是尺子的**——
    所以我第一版把门槛写成 "−100 dB 以下" 时，红的原因与尺子无关。

    改用 float32 写，才能真正探到"没有人为地板"这件事：
    `frame_db` 只有 `+1e-12`（≈ −240 dB）的兜底，不额外加地板。
    """
    sr = 44100
    t = np.arange(sr) / sr
    mono = (1e-7 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)
    p = tmp_path / "sil.f32.wav"
    sf.write(str(p), np.stack([mono, mono], axis=1), sr, subtype="FLOAT")

    got = float(csm.frame_db(p).mean())
    assert got < -120, (
        f"float32 下的近静音应量成很低（兜底 ≈−240 dB），实际 {got:.1f} dB —— "
        "是不是给 frame_db 加了人为地板？那会把『人声安静处其实很脏』盖住"
    )


def test_ruler_check_known_different_pair_is_far_apart():
    """★★ 核心：`ruler_check` 的**已知不同**那一对必须差得很大。

    若有人把这把尺子的口径改坏（例如换成某种归一化后的比值），
    "整首歌 vs 分离人声"就会塌到接近 0 —— 于是 `cover_sep_models` 的
    "两个模型差不多"变成**无法证伪**的结论。这条让那种改动立刻红。

    ⚠️ 门槛为什么是 **40 dB** 而不是"随便一个 >20"（2026-10-14 实测校准）：
    第一版写 >20，然后我做变异（把 `db - db.mean()` 抹掉绝对电平）时它**照样绿**——
    归一化后这一对仍差 21.6 dB。而真实口径下是 **48.24 dB**。
    ⇒ 门槛必须卡在"真实值"与"被削弱后仍能过"之间：取 40，
    这样归一化那种削弱必红，而真口径有 8 dB 余量（不是贴着边过）。

    产物在 `media/`（gitignore），没有就跳过 —— 但不能因为跳过就假装通过。
    """
    p = _ROOT / "media/covers/sepmodels/ruler_check.json"
    if not p.exists():
        pytest.skip("本机没有分离对照产物（media/ 不进仓库）")
    import json

    rows = json.loads(p.read_text(encoding="utf-8"))
    by = {r["label"]: r for r in rows}
    full = next((v for k, v in by.items() if "整首歌" in k), None)
    voc = next((v for k, v in by.items() if "原唱人声" in k), None)
    assert full and voc, f"对照行缺了：{list(by)}"
    spread = full["quiet15_dbfs"] - voc["quiet15_dbfs"]
    assert spread > 40, (
        f"已知不同的两例只差 {spread:.2f} dB（真实口径应为 ~48）；"
        "低于 40 说明尺子被削弱（例如换成了归一化口径），"
        "那么 cover_sep_models 的『两个模型差不多』就不可信了"
    )


def test_models_land_far_below_the_known_different_pair():
    """模型之间的小差异 vs 尺子的分辨率：必须**远小于**已知不同的那一对。

    这条把结论的**逻辑**钉住（而不只是数字）：既然两个模型落在
    "原唱人声"附近（差 ≤ 几 dB），而"整首歌"差了 48 dB，
    那"两模型分辨不出"就是**在这把尺子的分辨率之内**的结论，不是没量。
    """
    p = _ROOT / "media/covers/sepmodels/ruler_check.json"
    if not p.exists():
        pytest.skip("本机没有分离对照产物（media/ 不进仓库）")
    import json

    rows = json.loads(p.read_text(encoding="utf-8"))
    quiet = {r["label"]: r["quiet15_dbfs"] for r in rows}
    full = next(v for k, v in quiet.items() if "整首歌" in k)
    models = [v for k, v in quiet.items() if "htdemucs" in k and "人声轨" in k]
    assert len(models) == 2, f"应有两条模型人声轨，实际 {len(models)}"
    pair_gap = abs(models[0] - models[1])
    ruler_gap = abs(full - models[0])
    # 「同量级」纪律（#167）：要下"分辨不出"的结论，被测差异必须**远小于**尺子的分辨率。
    # 取 1/5 而不是 1/10：真实数据是 2.64 dB vs 48.24 dB（约 1/18），
    # 阈值留出余量但不会宽松到放过"同量级"的情形。
    assert pair_gap < ruler_gap / 5, (
        f"模型间差异 {pair_gap:.2f} dB 与尺子分辨率 {ruler_gap:.2f} dB 同量级 —— "
        "这种差距不能用『分辨不出』下结论（见 #167 的同量级纪律）"
    )


def test_fixed_frame_result_is_time_aligned_not_per_model_percentile():
    """★★ 钉住"固定帧集"这个口径 —— 它是本轮结论反转的原因。

    第一版（`cover_sep_segments.py`）让**每个模型取自己的 15 分位**，
    于是"整体更响"的那个分位点也更高，差值里混进了整体响度差：
    当时读出"ft 在副歌段更干净 −1.66 dB"。改成**同一批时间点**后方向**反转**
    （3/3 为负，中位 −1.20 dB，即 ft 在该帧集上残留更多）。

    这条守卫两件事：
      ① 真产物回归 —— 固定帧集的口径下，两条模型都必须有 3 次读数；
      ② **反向** —— 别再退回"各自分位"的写法：那种写法下 base 与 ft 的
         整轨 RMS 差（~1 dB）会串进结论里。
    """
    p = _ROOT / "media/covers/sepmodels/fixed_frames/fixed_frames.json"
    if not p.exists():
        pytest.skip("本机没有固定帧集产物（media/ 不进仓库）")
    import json

    rep = json.loads(p.read_text(encoding="utf-8"))
    cases = {c["label"]: c for c in rep["cases"]}
    assert "副歌段" in cases, f"缺副歌段对照：{list(cases)}"
    chorus = cases["副歌段"]
    for m in ("htdemucs", "htdemucs_ft"):
        assert len(chorus["values"][m]) == rep["n_runs"], (
            f"{m} 的读数不足 {rep['n_runs']} 次 —— 少了就看不出方向稳不稳"
        )
    # 本轮实测：副歌段 3/3 为负（ft 残留更多）。这条锁住"它是个稳定方向"而非噪声。
    assert all(d < 0 for d in chorus["diffs"]), (
        f"副歌段差值不再全是负的：{chorus['diffs']} —— "
        "若这是有意改动（例如换素材/换模型），请同步更新报告 m1i 的结论"
    )
