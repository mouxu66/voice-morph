"""`experiments/seedvc_shift_sweep.py` 的守卫测试。

为什么值得测（三条都是**不会报错、只会给出错误结论**的退化）：

1. **入口选错**：本脚本存在的唯一理由就是调 f0 版 `inference.py`。
   若被改回 `inference_v2.py`，它照样跑完、照样出分，但走的是 config 里
   `f0_condition: false` 的那条分支 —— 结构上唱不了歌（2026-09-27 实测）。
2. **旗标掉了**：`--f0-condition` / `--auto-f0-adjust` 缺任一个，输出变成念白，
   而 CAM++ 只会给出另一个数，**不会失败**。
3. **采样点被砍**：单点采样会掩盖「开头崩」—— 上一轮只测 40–70s 得出
   「音色 0.571 没损失」，全量一测开头 0–20s 只有 0.25。
   所以 `WINDOWS` 必须保持多点，这条是本文件里最该钉死的一条。

测试策略：只测纯函数与模块常量，不起子进程、不加载模型。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SWEEP = _PROJECT_ROOT / "experiments" / "seedvc_shift_sweep.py"


def _load_sweep_module():
    """按**路径**加载 `experiments/seedvc_shift_sweep.py`。

    不能靠 `import seedvc_shift_sweep`：`experiments/` 不是包，裸 import 找不到它
    （同 `test_verify_backend_sync.py` 里那条说明）。
    """
    spec = importlib.util.spec_from_file_location("seedvc_shift_sweep", _SWEEP)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["seedvc_shift_sweep"] = mod
    spec.loader.exec_module(mod)
    return mod


sweep = _load_sweep_module()


# ------------------------------------------------------- 入口必须是 f0 版


def test_targets_f0_entrypoint_not_v2():
    """★ 调 f0 版 inference.py，不是 inference_v2.py。

    v2 的 config 两处 `f0_condition: false`，`convert_voice_with_streaming()` 签名里
    根本没有 f0 —— 跑 v2 得到的任何「歌声」结论都是无效的。
    """
    assert sweep.INFER_F0.name == "inference.py"
    assert "v2" not in sweep.INFER_F0.name


def test_infer_f0_is_not_the_v2_script_next_to_it():
    """确认同目录下确实存在另一个入口 —— 否则上面的断言可能是同义反复。"""
    assert (sweep.SEEDVC_REPO / "inference_v2.py").exists(), "v2 入口不在预期位置，断言失去意义"
    assert sweep.INFER_F0 != sweep.SEEDVC_REPO / "inference_v2.py"


# ------------------------------------------------- 命令行旗标（缺了就静默变念白）


def _cmd(shift: int = 0):
    return sweep.build_cmd(shift, Path("s.wav"), Path("t.wav"), Path("o"))


def test_cmd_requests_f0_condition_and_auto_adjust():
    """f0 条件与 auto_f0_adjust 必须在命令行里显式为 True。"""
    cmd = _cmd()
    assert "--f0-condition" in cmd
    assert cmd[cmd.index("--f0-condition") + 1] == "True"
    assert "--auto-f0-adjust" in cmd
    assert cmd[cmd.index("--auto-f0-adjust") + 1] == "True"


def test_cmd_passes_shift_through_and_keeps_fixed_params():
    """shift 透传；固定量（步数/cfg/长度）与已标定的最优一致。"""
    cmd = _cmd(7)
    assert cmd[cmd.index("--semi-tone-shift") + 1] == "7"
    assert cmd[cmd.index("--diffusion-steps") + 1] == str(sweep.STEPS)
    assert cmd[cmd.index("--inference-cfg-rate") + 1] == str(sweep.CFG_RATE)
    assert cmd[cmd.index("--length-adjust") + 1] == str(sweep.LENGTH_ADJUST)


def test_cmd_defaults_are_the_calibrated_optimum():
    """默认值就是 2026-09-27 标定的那一组；被人顺手改小会静默降质。"""
    assert sweep.STEPS == 80
    assert sweep.CFG_RATE == 1.2
    assert sweep.LENGTH_ADJUST == 1.0


def test_every_shift_gets_a_distinct_cmd():
    """不同 shift 必须产生不同命令行 —— 否则整轮扫描是同一档重复 10 次。"""
    cmds = {"|".join(sweep.build_cmd(s, Path("a"), Path("b"), Path("o"))) for s in range(10)}
    assert len(cmds) == 10


# ------------------------------------------------------------------ 半音换算


@pytest.mark.parametrize("f_from,f_to,expect", [
    (100.0, 200.0, 12.0),      # 翻倍 = 一个整八度
    (200.0, 100.0, -12.0),     # 降八度
    (100.0, 100.0, 0.0),
    (272.5, 134.7, -12.20),    # ★ 真值：原唱干声 → 袋鼠参考音的实测间距
])
def test_semitones(f_from, f_to, expect):
    assert sweep.semitones(f_from, f_to) == pytest.approx(expect, abs=0.01)


def test_semitones_returns_zero_for_unmeasurable_pitch():
    """判不出音高（0/负数）时给 0，不能抛也不能给假偏差。"""
    assert sweep.semitones(0.0, 200.0) == 0.0
    assert sweep.semitones(200.0, 0.0) == 0.0
    assert sweep.semitones(-1.0, -1.0) == 0.0


def test_semitones_is_additive_with_shift():
    """降一个八度后再 +12 半音回到原点 —— 这正是 auto_f0_adjust 与 shift 的配合方式。"""
    base = sweep.semitones(272.5, 134.7)
    assert base + 12 == pytest.approx(0.0, abs=0.3)


# ------------------------------------------------------------ 多点采样窗口


def test_windows_are_multi_point():
    """★ 至少 3 个采样点 —— 单点会掩盖「开头崩」（0.25 vs 0.571）。"""
    assert len(sweep.WINDOWS) >= 3


def test_first_window_covers_the_opening():
    """第一个窗口必须从 0s 开始：崩掉的地方恰是开头。"""
    assert sweep.WINDOWS[0][0] == 0


def test_windows_are_non_overlapping_and_ordered():
    for (a0, a1), (b0, b1) in zip(sweep.WINDOWS, sweep.WINDOWS[1:], strict=False):
        assert a1 <= b0, f"窗口重叠/乱序: {a0}-{a1} 与 {b0}-{b1}"
    for t0, t1 in sweep.WINDOWS:
        assert t0 < t1


def test_window_slices_uses_16k_offsets():
    got = sweep.window_slices(300.0)
    assert got[0] == (0.0, 0, 20 * sweep.SR16)
    assert len(got) == len(sweep.WINDOWS)


def test_window_slices_drops_windows_beyond_duration():
    """时长不够的窗口直接丢掉 —— 宁可少一个点，也不拿静音凑数。"""
    assert sweep.window_slices(10.0) == []
    assert len(sweep.window_slices(100.0)) < len(sweep.WINDOWS)
    full = sweep.window_slices(271.2)          # 本次实测素材的真实时长
    assert len(full) == len(sweep.WINDOWS)


# ------------------------------------------------- 从路径里抠 shift（--measure-only）


@pytest.mark.parametrize("rel,expect", [
    ("final_s11/vc_vocals_x.wav", 11),      # 档号只在父目录里（历史产物就是这么存的）
    ("s03_vc_x.wav", 3),                    # 本脚本自己的命名
    ("s0.wav", 0),
    ("s15.wav", 15),
    ("vc_vocals_meituan_rat_002_1.0_80_1.2.wav", None),   # 参数编码里没有档号
    ("steps80_x.wav", None),                # `s80` 不能当 shift=80
    ("vocals_30s.wav", None),
])
def test_guess_shift(rel, expect):
    assert sweep.guess_shift(Path(rel)) == expect


def test_guess_shift_rejects_out_of_range():
    """档号上限 24 —— 否则 `s80` 这类会被当成 shift。"""
    assert sweep.guess_shift(Path("s80.wav")) is None
    assert sweep.guess_shift(Path("final_s99/vc.wav")) is None


# ---------------------------------------------------------- 整八度候选挑选


def _row(shift, sim, dev):
    return {"shift": shift, "sim_ref_whole": sim, "pitch_dev_mean": dev}


def test_pick_octave_candidate_prefers_timbre_within_tolerance():
    rows = [_row(0, 0.68, -12.7), _row(1, 0.67, -11.8), _row(5, 0.55, -7.8)]
    # −12.7 与 −11.8 都落在 −12±1 内，取音色更高的那档
    assert sweep.pick_octave_candidate(rows)["shift"] == 0


def test_pick_octave_candidate_returns_none_rather_than_forcing():
    """没有落进容差的就返回 None —— 不硬凑一个「最接近八度」的档当结论。"""
    assert sweep.pick_octave_candidate([_row(9, 0.9, -3.7)]) is None
    assert sweep.pick_octave_candidate([]) is None


def test_pick_octave_candidate_ignores_rows_without_pitch():
    """缺 pitch_dev_mean 的行不能参与挑选而炸掉（防御，不是当前调用路径）。"""
    assert sweep.pick_octave_candidate([{"shift": 3, "sim_ref_whole": 0.9}]) is None
    with_pitch = _row(1, 0.6, -12.1)
    assert sweep.pick_octave_candidate([{"shift": 3, "sim_ref_whole": 0.9}, with_pitch]) is with_pitch


def test_octave_target_is_minus_twelve():
    assert sweep.OCTAVE == -12.0
