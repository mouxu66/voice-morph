# -*- coding: utf-8 -*-
"""`experiments/cover_bwe_universr.py` + `tools/universr_enhance.py` 的结构守卫。

守三件在跑之前看不出来、跑完也看不出来的事：

1. **主 venv 不许 import universr/torch 之外的模型栈** —— UniverSR 的 requirements 钉
   torch 2.7.1，装进主 .venv 会顶掉生产 torch 2.9.1+cu128（5060 是 sm_120，只认 cu128）。
   所以"实验脚本必须能在主 venv 里 import，模型只能在独立 venv 的**子进程**里跑"是一条硬边界。
2. **必须默认播种** —— `seed=None` 时 UniverSR 每次采样新噪声，A/B 就不是单变量了。
3. **页面与产物路径表必须一致**（今天在 `cover_hf_bands` 上踩过一次：`page` 单独跑会读到
   没过后期链的中间产物，A 臂报"缺文件"而页面看着正常）。
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_EXP = _ROOT / "experiments"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _EXP / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bwe = _load("cover_bwe_universr_under_test", "cover_bwe_universr.py")
RUNNER = _ROOT / "tools" / "universr_enhance.py"


def _runner_tree() -> ast.Module:
    return ast.parse(RUNNER.read_text(encoding="utf-8"))


def test_experiment_imports_without_the_model_env():
    """实验脚本在主 venv 里 import 成功 = 它没有把 universr/torch 拉到模块级。"""
    tops = [n for n in ast.parse((_EXP / "cover_bwe_universr.py").read_text(encoding="utf-8")).body
            if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = set()
    for n in tops:
        names |= {a.name.split(".")[0] for a in n.names}
    assert "universr" not in names and "torch" not in names, names


def test_runner_keeps_model_import_inside_main():
    """跑手脚本同样：`import universr` 必须在 main() 里（否则主 venv 一 import 就炸）。"""
    tree = _runner_tree()
    module_level = set()
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            module_level |= {a.name.split(".")[0] for a in n.names}
    assert "universr" not in module_level, module_level


def test_runner_seeds_by_default():
    """--seed 的默认值必须是 int（None = 每次新噪声 = 破坏单变量）。"""
    tree = _runner_tree()
    src = RUNNER.read_text(encoding="utf-8")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            args = [a.value for a in node.args if isinstance(a, ast.Constant)]
            if "--seed" not in args:
                continue
            kw = {k.arg: k.value for k in node.keywords}
            assert isinstance(kw.get("default"), ast.Constant) and isinstance(kw["default"].value, int), \
                "runner 的 --seed 默认值必须是 int"
            assert "seed=a.seed" in src, "必须把 seed 真的传给 enhance()"
            return
    raise AssertionError("runner 里找不到 --seed 参数")


def test_runner_input_sr_is_effective_bandwidth_set():
    """`--input-sr` 只能是 8/12/16/24k（模型支持集），默认 16k(≈8k 截止)。"""
    tree = _runner_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
            args = [a.value for a in node.args if isinstance(a, ast.Constant)]
            if "--input-sr" not in args:
                continue
            kw = {k.arg: k.value for k in node.keywords}
            assert kw["default"].value == 16000
            assert sorted(e.value for e in kw["choices"].elts) == [8000, 12000, 16000, 24000]
            return
    raise AssertionError("runner 里找不到 --input-sr 参数")


def test_runner_is_launched_as_a_subprocess_with_explicit_interpreter():
    """跑手必须是**子进程 + 显式解释器路径**（不是 import 进来调用）。

    为什么没建独立 venv 而用主 .venv：cu128 轮子在这条线路上 ~217KB/s（1.3GB≈100 分钟），
    而主 venv 已有同版本 torch；代价是必须用 `--no-deps` 补纯 Python 依赖、且靠子进程
    隔离（后端不把 timm/torchdiffeq/模型带进来）。这两条的不变量钉在下面。
    """
    p = str(bwe.USR_PY).replace("\\", "/")
    assert p.endswith("/.venv/Scripts/python.exe"), p
    src = (_EXP / "cover_bwe_universr.py").read_text(encoding="utf-8")
    assert "subprocess.run(cmd" in src, "必须用 subprocess 启动跑手"
    assert '"--seed", str(seed)' in src, "必须把 seed 传给跑手（不固定就不是单变量）"


def test_rows_and_arm_files_agree():
    keys = [k for k, _, _ in bwe.ROWS]
    assert set(keys) <= set(bwe.ARM_FILES), f"行里出现了表外的臂：{set(keys) - set(bwe.ARM_FILES)}"
    vals = list(bwe.ARM_FILES.values())
    assert len(set(vals)) == len(vals), f"两个臂指向同一个文件：{vals}"
    # 本次新增的两臂必须在表里（否则页面会静默少显示"生成"这条路）
    for k in ("D1_bwe", "D2_bwe_no_air"):
        assert k in bwe.ARM_FILES


def test_d2_differs_only_by_air_eq():
    """D1/D2 的唯一差别是空气感 EQ —— 若把 D2 写成别的链，两臂就不再是单变量。"""
    src = (_EXP / "cover_bwe_universr.py").read_text(encoding="utf-8")
    assert "post_filters(air_db=0.0)" in src
    assert src.count("apply_filters(d40") == 2


def test_runner_prefers_local_weights():
    """权重必须**本地优先**：HF 在这条线路上 ~0.4MB/s，每次现拉不现实（也不能离线跑）。"""
    src = RUNNER.read_text(encoding="utf-8")
    assert "universr_repo" in src and "ckpts" in src, "跑手应默认指向 universr_repo/ckpts/universr-audio"
    assert "pytorch_model.bin" in src, "本地权重存在性判据应看 pytorch_model.bin"


# ---------------------------------------------------------------- 跑手的自检守卫（2026-10-20）

def _runner_mod():
    """加载 `tools/universr_enhance.py`（模块级只有 argparse/pathlib/json，可安全 import）。"""
    spec = importlib.util.spec_from_file_location("universr_enhance_under_test", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["universr_enhance_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_f0_skips_leading_silence():
    """整曲开头是静音时，f0 自检必须往后找有声窗，不能读 0 就放行。

    《成都》前 10s 是 −81dB；旧实现取前 3s → 0.0 → `else: octave=0.0` 静默放行，
    整曲那次 UniverSR 的自检其实是**假绿**（2026-10-20 实测 octave_shift 记成 0.0）。
    """
    import numpy as np

    sr = 16000
    silence = np.zeros(int(2.5 * sr))
    t = np.arange(int(3.0 * sr)) / sr
    tone = 0.4 * np.sin(2 * np.pi * 200.0 * t)
    f0 = _runner_mod()._f0(np.concatenate([silence, tone]), sr)
    assert 150.0 < f0 < 260.0, f"前导静音把 f0 读成了 {f0}"


def test_f0_returns_zero_only_for_fully_silent_input():
    import numpy as np

    sr = 16000
    assert _runner_mod()._f0(np.zeros(sr * 8), sr) == 0.0


def test_runner_has_duration_guard_and_does_not_fake_octave():
    """时长守卫必须在（拉慢 3 倍一定改时长）；读不出 f0 时 octave 记 None 而不是 0.0。"""
    src = RUNNER.read_text(encoding="utf-8")
    assert "secs_out - secs_in" in src, "缺时长守卫：拉慢 3 倍最硬的判据是时长"
    assert '"f0_checked"' in src, "必须如实记录 f0 自检有没有生效"
    assert '"octave_shift": round(octave, 3) if octave is not None else None' in src, \
        "读不出 f0 时不许把 octave 写成 0.0（那会把「没验过」伪装成「没偏移」）"
