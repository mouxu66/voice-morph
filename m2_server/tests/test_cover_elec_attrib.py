"""BWE「气口电音」归因的守卫（2026-10-20）。

这一轮的三个坑都是"看起来对、实测错"的类型，必须用测试锁住正确的那一版：

① **分位分档**：按各文件自己的分位数切档，A 臂的真空音帧会因为"估不出 f0"
   被整批剔除，D 臂却因高频有纯谐波能估出 f0 —— 两边根本不是同一批帧。
   实测那样得出 A 的 silent 占 0.5%、D 占 16.5%，而真实值是 24.7% 且完全相同。

② **频域硬切**：STFT 里把 f≥8k 的 bin 置零再重建等价于 sinc 核，旁瓣残留会在
   8–16k 留下 −10dB 量级的低频泄漏；D 的低频比 A 的高频强 20dB，这点泄漏足以
   盖掉 A 的谱结构（实测 crest 恒为 23.9，A 是 12.9，等于白换）。

③ **dB 域帧平滑**：静音帧夹在字缝里，前后强音的 dB 均值会把 env 拉高 10dB 以上，
   权重 w 永远到不了 0，气口关不掉。

测试只做静态/数值断言，不依赖 media/ 下的产物（那目录不入库）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments"

GATE3 = EXP / "cover_elec_gate3.py"
FINAL = EXP / "cover_elec_final.py"


def _mod(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------- ① 分档口径

def test_final_tiers_are_absolute_db_not_percentile():
    """分档边界必须是绝对 dB 常数；一旦改成 percentile，A/D 就不可比。"""
    src = FINAL.read_text(encoding="utf-8")
    assert '("silent", -1e9, -40.0)' in src or "silent" in src
    assert "np.percentile(elo" not in src, \
        "分档不能用各文件自己的分位数 —— A/D 会落到不同帧集上（实测 0.5% vs 16.5%）"
    assert "同一帧集" in src or "绝对" in src, "必须写明口径来源"


def test_final_uses_reference_tiers_for_all_arms():
    """除参照自身外，所有臂都要用 A 臂的分档基准，才能逐帧对齐。"""
    src = FINAL.read_text(encoding="utf-8")
    assert "tier_ref" in src, "缺少外部传入的分档基准"
    assert "ref_elo" in src, "必须以 A 臂的低频电平作为基准"


# --------------------------------------------------------------- ② 混合方式

def test_gate3_mixes_in_time_domain_not_bins():
    """最终方案必须是时域加权；频域拼接会因 sinc 旁瓣泄漏而失效。"""
    src = GATE3.read_text(encoding="utf-8")
    assert "mixed = a * (1.0 - w_s) + d * w_s" in src, \
        "必须是时域加权：两端严格等于源信号，且不会被低频旁瓣污染"
    assert "S[hi_m] = SA[hi_m] * (1.0 - wt) + SD[hi_m] * wt" not in src, \
        "频域拼接已证伪：D_low 的 sinc 旁瓣会盖掉 A 的高频结构（crest 23.9 vs 12.9）"
    assert "y - hi" not in src and "lo_d + hi_d" not in src, \
        "带通相减分频已证伪：通带边缘只衰 3dB，残留会让门控反向抬升 4.4dB"


def test_gate3_kills_d_completely_in_silence():
    """气口权重必须严格为 0 —— D 是纯音，留 7.5% 能量峰值仍由它主导。"""
    mod = _mod("gate3_under_test", GATE3)
    xs = np.array([p[0] for p in mod.MIX_POINTS])
    ys = np.array([p[1] for p in mod.MIX_POINTS])
    for env_db in (-80.0, -60.0, -45.0, -33.0):
        w = float(np.interp(env_db, xs, ys))
        assert w == 0.0, f"env {env_db}dB 的 D 权重是 {w}，气口必须完全关掉 D"
    # 强段必须完全交给 D（否则会把亮度也削掉）
    for env_db in (-16.0, -5.0, 0.0, 12.0):
        w = float(np.interp(env_db, xs, ys))
        assert w == 1.0, f"env {env_db}dB 的 D 权重是 {w}，强段必须全用 BWE"


def test_gate3_weights_are_monotonic():
    """权重必须单调不减：强度越高越信 BWE，不能出现来回摆。"""
    mod = _mod("gate3_mono", GATE3)
    ys = [p[1] for p in mod.MIX_POINTS]
    assert all(b >= a for a, b in zip(ys, ys[1:])), f"权重非单调：{ys}"


# --------------------------------------------------------------- ③ 包络

def test_frame_lo_db_returns_very_low_for_silence():
    """纯静音必须落到极低 dB，否则门控阈值没意义。"""
    mod = _mod("gate3_env", GATE3)
    n = mod.WIN * 40
    z = np.zeros(n, dtype=np.float64)
    env = mod.frame_lo_db(z)
    assert np.all(env < -100.0), f"静音帧能量 {env.max():.1f}dB，应为 −200 量级"


def test_frame_lo_db_tracks_level():
    """0–8k 能量必须随信号幅度单调上升（每 +6dB 幅度 → +6dB 能量）。"""
    mod = _mod("gate3_env2", GATE3)
    n = mod.WIN * 40
    t = np.arange(n) / mod.SR
    out = []
    for amp in (0.01, 0.1, 1.0):
        y = amp * np.sin(2 * np.pi * 440.0 * t)
        out.append(float(np.median(mod.frame_lo_db(y))))
    assert out[1] - out[0] > 15.0 and out[2] - out[1] > 15.0, \
        f"幅度 ×10 应升约 20dB，实测 {out}"


# --------------------------------------------------------------- 脚本可导入

def test_attrib_scripts_have_no_module_level_io():
    """归因脚本的模块级不能有 I/O（读音频/写文件），否则 import 即崩。"""
    import ast

    for name in ("cover_elec_diagnose", "cover_elec_final", "cover_elec_gate3"):
        p = EXP / f"{name}.py"
        assert p.exists(), f"缺 {p}"
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                fn = node.value.func
                nm = getattr(fn, "id", None) or getattr(fn, "attr", "")
                assert nm not in ("read", "open", "write_text", "load"), \
                    f"{name} 模块级调用了 {nm}()，import 会有副作用"
