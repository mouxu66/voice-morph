# -*- coding: utf-8 -*-
"""`experiments/cover_ckpt_sweep.py` 与 `cover_metrics.normalize_configs` 的守卫。

守什么（两处都是"错了不会报错、只会给出错结论"的地方）：

1. **`resolve_pth` 只找权重、不找 index**。本扫描的**唯一变量**必须是权重：
   若它顺手带上 `logs/<voice>/added_*.index`，`convert_audio` 就会开检索 ——
   而不同实验的 index 来自**不同数据集**，于是"权重差异"与"检索库差异"混进同一个数，
   结论直接失效（且不会报错）。所以这里钉死"找不到 index 也要能跑"。
2. **`normalize_configs` 对两种报告都要给出行**。旋钮报告（`configs`）与权重报告
   （`rows`）字段名不同；哪一边的键名改了而没同步，体检就会**静默地一条都不打分**
   —— 页面照出、`metrics.json` 照写，只是里面空的（同族 §8.21 白绿病）。
   所以除了"两种形状都给行"，还要有一条"缺文件的条目会被剔掉"的反向断言。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CKPT = _ROOT / "experiments" / "cover_ckpt_sweep.py"
_METRICS = _ROOT / "experiments" / "cover_metrics.py"
for _p in (_ROOT / "m2_server",):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_sweep_resolve_pth_ignores_index(tmp_path, monkeypatch):
    """★★ 本扫描的命门：**只认 .pth，绝不带 index**（否则两个变量同时动）。"""
    ck = _load("cover_ckpt_sweep_t1", _CKPT)
    (tmp_path / "logs" / "v1").mkdir(parents=True)
    (tmp_path / "assets" / "weights").mkdir(parents=True)
    weight = tmp_path / "assets" / "weights" / "v1.pth"
    weight.write_bytes(b"x")
    # 同时在 logs/ 下放一个 index —— 它**不该**被用到
    (tmp_path / "logs" / "v1").mkdir(exist_ok=True)
    (tmp_path / "logs" / "v1" / "added_abc.index").write_bytes(b"idx")
    (tmp_path / "logs" / "v1" / "v1.pth").write_bytes(b"x")

    monkeypatch.setenv("VM_RVC_ROOT", str(tmp_path))
    got = ck.resolve_pth("v1")
    assert got.suffix == ".pth", f"拿到的是 {got}（必须是权重）"
    assert got == tmp_path / "logs" / "v1" / "v1.pth" or got == weight


def test_sweep_groups_only_reference_real_names():
    """扫描表里不许出现"手滑打错"的权重名 —— 那会变成一次静默的空跑。

    只做形态断言（本机可能没有这些权重）：名字必须是非空字符串、组内不重复。
    真正"存不存在"由运行时打印的跳过信息负责，不在这里假装能测。
    """
    ck = _load("cover_ckpt_sweep_t2", _CKPT)
    assert ck.GROUPS, "扫描表空了 —— 实验会一条都不跑却退出 0"
    for group, names in ck.GROUPS.items():
        assert names, f"组 {group} 是空的"
        assert all(isinstance(n, str) and n.strip() for n in names), names
        assert len(names) == len(set(names)), f"组 {group} 里有重复项：{names}"


def test_normalize_configs_handles_both_report_shapes(tmp_path):
    """★ 两种报告形状都要给出行 —— 缺一边就是"静默零打分"。"""
    cm = _load("cover_metrics_t1", _METRICS)
    w1 = tmp_path / "a.wav"
    w1.write_bytes(b"x")

    knobs_shape = {"voice": "v", "seed": 1, "pitch": -12,
                   "configs": [{"name": "A", "why": "why", "wav": str(w1)}]}
    ckpt_shape = {"rows": [{"voice": "v_e10", "group": "g", "wav": str(w1)}]}

    a = cm.normalize_configs(knobs_shape)
    b = cm.normalize_configs(ckpt_shape)
    assert [r["name"] for r in a] == ["A"], a
    # 权重扫描的名字带组名前缀（同一 voice 可能合法地出现在多个组里）
    assert [r["name"] for r in b] == ["g__v_e10"], b
    assert all(r["wav"] for r in a + b)


def test_normalize_configs_allows_same_voice_in_two_groups(tmp_path):
    """★ 同一个 voice 出现在**两个组**里是合法的，不许被重名守卫判错。

    `cover_ckpt_sweep.GROUPS` 刻意让 `myvoice_ppt12_full_e40_s3960` 同时进
    `epochs_12min`（当 epoch 组最后一档）与 `dur_4_8_12min`（当 12min 档）——
    那是同一权重的两个用途。2026-10-14 我加的重名守卫第一版把这种合法情形
    判成了 ValueError（全量门禁当场红），所以这里钉住它必须被放过。
    """
    cm = _load("cover_metrics_t5", _METRICS)
    w = tmp_path / "a.wav"
    w.write_bytes(b"x")
    rep = {"rows": [
        {"voice": "v_e40", "group": "g1", "wav": str(w)},
        {"voice": "v_e40", "group": "g2", "wav": str(w)},
    ]}
    got = [r["name"] for r in cm.normalize_configs(rep)]
    assert got == ["g1__v_e40", "g2__v_e40"], got


def test_normalize_configs_still_catches_true_duplicates(tmp_path):
    """但**真的**重名（同组同名两条）仍必须抛 —— 那才会让测量互相覆盖。"""
    cm = _load("cover_metrics_t6", _METRICS)
    w = tmp_path / "a.wav"
    w.write_bytes(b"x")
    rep = {"rows": [
        {"voice": "v1", "group": "g", "wav": str(w)},
        {"voice": "v1", "group": "g", "wav": str(w)},
    ]}
    with pytest.raises(ValueError, match="重复"):
        cm.normalize_configs(rep)


def test_normalize_configs_drops_missing_files(tmp_path):
    """反向：报告里指向不存在文件的条目要**剔掉**。

    留着会让体检在使用户栈里找文件、报一个看不懂的错；而"剔掉"必须是有意的行为，
    所以这条把它钉住（否则"剔"和"全没解析出来"在结果上分不开 —— 那是 §8.21）。
    """
    cm = _load("cover_metrics_t2", _METRICS)
    real = tmp_path / "real.wav"
    real.write_bytes(b"x")
    rep = {"configs": [
        {"name": "ok", "why": "在", "wav": str(real)},
        {"name": "gone", "why": "不在", "wav": str(tmp_path / "nope.wav")},
        {"name": "empty", "why": "没有 wav", "wav": None},
    ]}
    got = [r["name"] for r in cm.normalize_configs(rep)]
    assert got == ["ok"], f"应只留存在的那个，实际 {got}"


def test_report_meta_covers_both_shapes():
    """`report_meta` 要给两份报告都取出"这项实验是什么"（页面 intro 用它）。"""
    cm = _load("cover_metrics_t3", _METRICS)
    knobs = {"voice": "myvoice", "seed": 1234, "pitch": -12}
    ckpt_only = {"pitch": -12, "seed": 1234}   # 权重扫描没有单一 voice
    m1, m2 = cm.report_meta(knobs), cm.report_meta(ckpt_only)
    assert m1["voice"] == "myvoice" and m1["pitch"] == -12
    assert m2["voice"], "权重扫描也要有一个能打印的说明，不能是 None"
    assert m2["pitch"] == -12


def test_real_reports_on_disk_are_parseable():
    """真产物回归：本机跑过的那两份报告必须仍能被规整出非空行。

    产物在 `media/`（gitignore），所以**文件不存在就跳过** —— 但不能因为跳过
    就假装通过（这条在 CI 上没有产物是正常的）。
    """
    import pytest

    cm = _load("cover_metrics_t4", _METRICS)
    found = 0
    for rel in ("media/covers/ckpt/ckpt_sweep.json",
                "media/covers/ckpt_s4321/ckpt_sweep.json"):
        p = _ROOT / rel
        if not p.exists():
            continue
        rows = cm.normalize_configs(json.loads(p.read_text(encoding="utf-8")))
        assert rows, f"{rel} 规整出 0 行 —— 键名是不是改了？"
        found += 1
    if not found:
        pytest.skip("本机没有 checkpoint 扫描产物（media/ 不进仓库）")
