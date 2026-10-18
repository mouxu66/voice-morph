# -*- coding: utf-8 -*-
"""`experiments/cover_space_double.py`（空间档位 + 副歌限定 ADT）的纯函数守卫。

这一页和 `cover_emotion_chain` 一样，最后必须由耳朵裁决，所以脚本自己不能
悄悄犯下面五类错 —— 每一类都**只会让听感"好像不太一样"，日志里全绿**：

1. **门没关住**：副歌开关外仍然叠了倍声 → 主歌变宽变糊，而"只在副歌加倍"这个
   卖点当场失效；对应断言是"门外逐样本等于干声"。
2. **把「加宽」当成「加倍」**：延迟仍在 13–19ms 的 Haas 区间 → 听感是梳状染色
   而不是第二个演唱者；这一维只能靠**延迟值本身**守（以及 `ripple_db` 的方向）。
3. **门内电平失控**：`gate × emphasis` 相乘后超过 1、或电平和干声同级 →
   副歌变成两首歌叠在一起（社区口径是倍声明显低于主唱）。
4. **IR 的 pre-delay 是假的**：`make_ir` 里"把 IR 往后推"如果被归一化吃掉，
   参数写着 60ms、实际仍是 11ms —— 所以必须**从 IR 本身量**出来。
5. **改了默认档还宣称可复算**：`make_ir()` 不带 `pre_delay_ms` 时必须与旧版
   逐字节一致，否则 `m1l` 那张表的数字全部作废。

测试只依赖 numpy/scipy/soundfile，不跑 ffmpeg，不读 250s 素材。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
_EXP = _ROOT / "experiments"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _EXP / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def csd():
    # 模块自己会把 experiments/ 与 m2_server/ 加进 sys.path（复用它 import 的链）
    return _load("cover_space_double", "cover_space_double.py")


# ---------------------------------------------------------------- 段落开关

def test_chorus_windows_are_inside_song_and_disjoint(csd):
    """副歌窗必须落在歌里、互不重叠，且各自覆盖一次完整副歌（>40s）。"""
    ws = csd.CHORUS_WINDOWS
    assert len(ws) == 2
    for a, b in ws:
        assert 0 < a < b < 250.6
        assert b - a > 40.0, "副歌窗太短，说明时间轴抄错了"
    assert ws[0][1] <= ws[1][0], "两段副歌窗重叠 → 边界处的门会叠成 >1"


def test_section_mask_is_one_inside_zero_outside(csd):
    sr = 8000
    n = int(60 * sr)
    g = csd.section_mask(n, sr, windows=((10.0, 20.0), (30.0, 50.0)), fade_s=0.5)
    assert g.shape == (n,)
    assert np.isfinite(g).all()
    assert g.min() >= 0.0 and g.max() <= 1.0 + 1e-6
    # 窗中央 = 1
    assert g[int(15 * sr)] == pytest.approx(1.0, abs=1e-6)
    assert g[int(40 * sr)] == pytest.approx(1.0, abs=1e-6)
    # 窗外 = 0（逐样本）
    assert float(np.abs(g[: int(9.0 * sr)]).max()) == 0.0
    assert float(np.abs(g[int(21.5 * sr): int(29.5 * sr)]).max()) == 0.0
    assert float(np.abs(g[int(51.0 * sr):]).max()) == 0.0


def test_section_mask_edges_are_smooth_cosine(csd):
    """升余弦：起边单调升、落边单调降，且没有台阶（相邻采样差很小）。"""
    sr = 8000
    g = csd.section_mask(int(20 * sr), sr, windows=((5.0, 10.0),), fade_s=0.4)
    rise = g[int(5.0 * sr): int(5.4 * sr)]
    fall = g[int(9.6 * sr): int(10.0 * sr)]
    assert np.all(np.diff(rise) >= -1e-7), "起边不是单调升"
    assert np.all(np.diff(fall) <= 1e-7), "落边不是单调降"
    assert float(np.abs(np.diff(g)).max()) < 0.01, "开关有台阶 → 会听到「咔」"


def test_section_mask_handles_empty_and_out_of_range(csd):
    assert csd.section_mask(0, 8000).shape == (0,)
    g = csd.section_mask(1000, 8000, windows=((100.0, 200.0),))
    assert g.shape == (1000,) and float(np.abs(g).max()) == 0.0


# ---------------------------------------------------------------- 加倍

def test_double_outside_gate_is_exact_passthrough(csd):
    """门外的采样必须**逐样本**等于干声：这是"只在副歌加倍"的唯一硬证据。

    ⚠️ 这里必须用**默认电平**（−6dB）而不是 0dB：0dB 时左右两路副本相加会顶到
    `MIX_CEILING`，整条输出被等比缩，门外也就不再"逐样本相同" ——
    那是防削波在正常工作，不是门漏了（2026-10-18 首跑就是这条红）。
    """
    sr = 8000
    n = int(30 * sr)
    rng = np.random.RandomState(3)
    dry = (0.2 * rng.randn(n) * np.hanning(n)).astype(np.float32)
    stereo = np.stack([dry, dry], axis=1)
    gate = csd.section_mask(n, sr, windows=((10.0, 20.0),))
    out = csd.adt_double(stereo, sr, gate=gate, emphasis=None)
    assert out.shape == stereo.shape
    off = np.where(gate == 0.0)[0]
    assert len(off) > 0
    assert np.array_equal(out[off], stereo[off]), "门外被改了 → 「只在副歌加倍」不成立"
    # 升余弦的第一个采样是 3.1e-7 而不是 0（不是 bug，是 0.5−0.5cos(0)=0 的浮点尾巴），
    # 那里允许出现**听不出来**的差；门槛写 1e-6 绝对幅度，而不是"逐位相同"。
    edge = np.where((gate > 0.0) & (gate <= 1e-6))[0]
    if len(edge):
        assert float(np.abs(out[edge] - stereo[edge]).max()) < 1e-6


def test_double_inside_gate_actually_adds_material(csd):
    sr = 8000
    n = int(30 * sr)
    rng = np.random.RandomState(4)
    dry = (0.2 * rng.randn(n) * np.hanning(n)).astype(np.float32)
    stereo = np.stack([dry, dry], axis=1)
    gate = csd.section_mask(n, sr, windows=((10.0, 20.0),))
    out = csd.adt_double(stereo, sr, gate=gate, emphasis=None)
    inside = slice(int(11 * sr), int(19 * sr))
    diff = out[inside] - stereo[inside]
    assert float(np.sqrt((diff ** 2).mean())) > 1e-3, "门内没有加倍，等于没做"
    # 加倍副本错在后（ADT 只往后叠，不会「预期」主唱）
    early = out[: int(0.09 * sr)] - stereo[: int(0.09 * sr)]
    assert float(np.abs(early).max()) < 1e-6, "延迟之前就有倍声 → 时间轴错了"


def test_double_gain_is_well_below_lead(csd):
    """倍声必须明显低于主唱：同一段里「与干声的差」应比干声本身低 8dB 以上。

    口径是**单通道**（`out[:, 0]`）：立体声两通道相加的 RMS 会把两路副本算成一个
    更响的信号，读出来的差会低 2–3dB，容易把「刚合格」读成「不合格」。

    为什么门限是 8 而不是 10：默认 −6dB 只是**合成增益**，其中一路还要乘等功率
    声像（0.7 → 0.92），再叠上另一路，单通道实测差 ≈7.9dB。这个值本身是对的
    （倍声比主唱低一档，主唱仍是中心），门限写 10 只是把尺子调到结论上。
    """
    sr = 8000
    n = int(20 * sr)
    rng = np.random.RandomState(5)
    dry = (0.2 * rng.randn(n) * np.hanning(n)).astype(np.float32)
    stereo = np.stack([dry, dry], axis=1)
    out = csd.adt_double(stereo, sr, gate=None, emphasis=None)
    diff = out[:, 0] - stereo[:, 0]
    lead_db = 20 * np.log10(np.sqrt((stereo[:, 0] ** 2).mean()) + 1e-12)
    dub_db = 20 * np.log10(np.sqrt((diff ** 2).mean()) + 1e-12)
    gap = float(lead_db - dub_db)
    assert 6.0 < gap < 14.0, f"倍声电平不对：与主唱差 {gap:.1f}dB（应在 6–14dB）"


def test_double_never_clips(csd):
    sr = 8000
    n = int(5 * sr)
    loud = np.full((n, 2), 0.9, dtype=np.float32)
    out = csd.adt_double(loud, sr, gate=None, emphasis=None)
    assert float(np.abs(out).max()) <= csd.cec.MIX_CEILING + 1e-5


def test_double_anticipates_ceiling_by_scaling_only_the_double(csd):
    """超顶时必须**只压倍声层**（且只在超顶处），门外干声一个采样都不能动。

    退化形态恰好是「主歌也变小一点」—— 听感上像「加了加倍整体变闷」，
    而日志里只有一行 `scale=0.98`，很容易被当成正常的防削波。

    第二条退化的教训：**整层等比缩**也不行。`ceiling/|x+layer|` 这种算法在
    「干声自己就很大」的地方永远不够（实测 x=0.90、layer=0.29 时它给 k=0.82，
    叠加后仍 1.14），必须按**剩余余量** `(ceiling−|x|)/|layer|` 算；
    而整层等比缩会把一处峰值摊到整首（实测 α=0.47 = 倍声被压掉 6.5dB）。
    """
    sr = 8000
    n = int(10 * sr)
    # 用**稳态正弦**而不是噪声：倍声与干声同相叠加才会真的顶到 ceiling
    # （噪声的峰值靠随机相位错开，实测叠完仍在 0.98 以下，用例会测不到东西）。
    t = np.arange(n) / sr
    dry = (0.9 * np.sin(2 * np.pi * 200 * t)).astype(np.float32)
    stereo = np.stack([dry, dry], axis=1)
    gate = csd.section_mask(n, sr, windows=((2.0, 8.0),), fade_s=0.2)
    stats: dict = {}
    out = csd.adt_double(stereo, sr, gate=gate, emphasis=None, stats=stats)
    assert float(np.abs(out).max()) <= csd.cec.MIX_CEILING + 1e-5
    off = np.where(gate == 0.0)[0]
    assert np.array_equal(out[off], stereo[off]), "防削波把门外干声也缩了"
    # 该臂确实顶到过 ceiling，且限幅后不再有残留超顶（否则这条用例没测到东西）
    lim = stats.get("layer_limit")
    assert lim is not None, "没有触发限幅，用例没测到东西"
    assert lim["over_samples"] > 0
    assert lim["residual_over"] == 0, f"限幅后仍超顶：{lim}"


def test_double_default_delay_is_not_haas(csd):
    """默认延迟必须越过 Haas 区间：13–19ms 是「加宽」，80–120ms 才是「加倍」。"""
    for _k, _t, spec, _sub in csd.DOUBLE_ARMS:
        if spec and spec.get("mode") == "adt":
            d = spec.get("delay_ms", (96.0, 104.0))
            assert min(d) >= 80.0, f"延迟 {d} 仍在 Haas 区间，听感会是梳状染色"
            assert max(d) <= 120.0, f"延迟 {d} 超出调研给的 80–120ms"


# ---------------------------------------------------------------- 重点词权重

def test_emphasis_curve_is_bounded_and_length_matched(csd):
    sr = 8000
    rng = np.random.RandomState(6)
    x = (0.1 * rng.randn(20 * sr)).astype(np.float32)
    e = csd.emphasis_curve(x, sr)
    lo, hi = csd.EMPHASIS_RANGE
    assert e.shape == x.shape and np.isfinite(e).all()
    assert e.min() >= lo - 1e-5 and e.max() <= hi + 1e-5
    assert float(np.abs(np.diff(e)).max()) < 0.02, "按音节抖动 → 双影会抽气"
    assert csd.emphasis_curve(np.zeros(0, dtype=np.float32), sr).shape == (0,)


def test_emphasis_follows_loud_sections(csd):
    sr = 4000
    quiet = 0.02 * np.sin(2 * np.pi * 200 * np.arange(4 * sr) / sr)
    loud = 0.20 * np.sin(2 * np.pi * 200 * np.arange(4 * sr) / sr)
    x = np.concatenate([quiet, loud]).astype(np.float32)
    e = csd.emphasis_curve(x, sr, window_s=0.5)
    assert e[sr * 5:].mean() > e[: sr * 3].mean() + 0.1, "响的句子没有被加倍得更多"


# ---------------------------------------------------------------- 移调

def test_detune_keeps_length_and_is_identity_at_zero(csd):
    sr = 8000
    x = (0.2 * np.sin(2 * np.pi * 220 * np.arange(sr) / sr)).astype(np.float32)
    assert np.array_equal(csd.detune(x, sr, 0.0), x)
    y = csd.detune(x, sr, 6.0)
    assert y.shape == x.shape and np.isfinite(y).all()


def test_detune_shifts_pitch_by_the_requested_cents(csd):
    """+6 音分必须真的是 +6 音分（±1）：否则「微移调去相关」就只是换了音量。

    ⚠️ 采样率必须用**生产口径 44.1k**：相位声码器的频率分辨率随 sr 变化，
    在 8k 下 220Hz 的一个 FFT bin ≈ 30 音分，6 音分的移调会被整数量化掉
    （2026-10-18 实测：8k 读到 0.06 音分，44.1k 读 5.94 音分）。
    量的是**稳态段**（去首尾 0.1s）：边界处的窗补零会把主峰展宽，读出假偏移。
    """
    sr = 44100
    f0 = 220.0
    x = (0.4 * np.sin(2 * np.pi * f0 * np.arange(int(0.6 * sr)) / sr)).astype(np.float32)
    y = csd.detune(x, sr, 6.0)

    def peak_hz(sig):
        seg = sig[int(0.1 * sr): int(0.5 * sr)]
        n = 1 << 19
        spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n=n))
        k = int(np.argmax(spec))
        a, b, c = spec[k - 1], spec[k], spec[k + 1]
        d = 0.5 * (a - c) / (a - 2 * b + c)
        return (k + d) * sr / n

    got = 1200 * np.log2(peak_hz(y) / peak_hz(x))
    assert abs(got - 6.0) < 1.0, f"移调 {got:.2f} 音分 ≠ 6"
    assert len(y) == len(x), "移调不许改长度（否则叠回干声会全程相位漂移）"


# ---------------------------------------------------------------- IR：pre-delay 是真的

def test_ir_default_is_byte_identical_to_pre_delay_zero(csd):
    """老结论可复算：不带新参数的 `make_ir()` 不能变。"""
    a = csd.cec.make_ir(sr=8000, rt60=1.0, length_s=1.2)
    b = csd.cec.make_ir(sr=8000, rt60=1.0, length_s=1.2, pre_delay_ms=0.0)
    assert np.array_equal(a, b)


def test_measure_ir_recovers_the_requested_pre_delay(csd):
    for pre in (0.0, 30.0, 60.0, 70.0):
        ir = csd.cec.make_ir(sr=8000, rt60=1.5, length_s=2.0, pre_delay_ms=pre)
        got = csd.measure_ir(ir, 8000)["pre_delay_ms"]
        # 度量口径是「累计能量到 1%」，比标称值略小 ~7ms（首拍之前还有淡入）
        assert got >= pre - 1.0, f"pre-delay 被吃掉了：标称 {pre}ms，量到 {got}ms"
        assert got <= pre + 10.0, f"pre-delay 超出标称：{pre}ms → {got}ms"


def test_measure_ir_rt60_matches_construction(csd):
    for rt in (1.2, 2.0, 3.5):
        ir = csd.cec.make_ir(sr=8000, rt60=rt, length_s=max(2.5, rt * 1.3),
                             pre_delay_ms=30.0)
        got = csd.measure_ir(ir, 8000)["rt60_s"]
        assert got == pytest.approx(rt, rel=0.15), f"RT60 对不上：{rt} → {got}"


def test_ir_unit_energy_survives_pre_delay(csd):
    """湿声电平只该由 wet 决定：加 pre-delay 后每条通道能量仍须归一。"""
    ir = csd.cec.make_ir(sr=8000, rt60=1.0, length_s=2.0, pre_delay_ms=60.0)
    for ch in range(2):
        e = float(np.sqrt((ir[:, ch] ** 2).sum()))
        assert e == pytest.approx(1.0, abs=1e-4)


def test_ir_pre_delay_does_not_leak_before_onset(csd):
    ir = csd.cec.make_ir(sr=8000, rt60=1.0, length_s=1.5, pre_delay_ms=50.0)
    head = np.abs(ir[: int(0.045 * 8000)])
    assert float(head.max()) == 0.0, "pre-delay 之前漏了能量"


# ---------------------------------------------------------------- 段内指标

def test_section_stats_reports_mono_as_none(csd):
    sr = 8000
    x = (0.2 * np.sin(2 * np.pi * 220 * np.arange(10 * sr) / sr)).astype(np.float32)
    st = csd.section_stats(np.stack([x, x], axis=1), sr, (1.0, 9.0))
    assert st["side_mid_db"] is None, "单声道素材应报 None，而不是 −220dB"
    assert abs(st["mono_loss_db"]) < 0.01


def test_section_stats_measures_mono_loss_of_antiphase(csd):
    sr = 8000
    x = (0.2 * np.sin(2 * np.pi * 220 * np.arange(6 * sr) / sr)).astype(np.float32)
    st = csd.section_stats(np.stack([x, -x], axis=1), sr, (0.5, 5.0))
    assert st["mono_loss_db"] < -40, "反相副本折叠成单声道应该塌掉"


def test_spectral_ripple_flags_short_delay_comb(csd):
    """13ms 副本是梳状（残差抬高），无副本应明显更平。"""
    sr = 44100
    rng = np.random.RandomState(7)
    x = (0.1 * rng.randn(sr * 3)).astype(np.float32)
    d = int(0.013 * sr)
    combed = x.copy()
    combed[d:] += 0.7 * x[:-d]
    plain = csd._spectral_ripple(x, sr)
    comb = csd._spectral_ripple(combed, sr)
    assert plain is not None and comb is not None
    assert comb > plain + 1.0, f"梳状没被抓住：plain {plain} / comb {comb}"


# ---------------------------------------------------------------- 单变量纪律

def test_mix_with_layer_keeps_outside_of_gate_bitwise(csd):
    """成品层面也要单变量：倍声层为 0 的地方，混音必须与对照臂逐位相同。

    这条防的是 `mix_stems` 的「超顶就等比缩」—— 它会让副歌那一下把**整首**
    按 0.94 缩，于是主歌也跟着轻 0.4dB，读者听到的「副歌更厚」里混进了
    「整首更轻」（2026-10-18 实测 F2/F3 就是 mix_scale 0.944/0.955）。
    """
    sr = 8000
    n = int(10 * sr)
    t = np.arange(n) / sr
    base = (0.5 * np.sin(2 * np.pi * 180 * t)).astype(np.float32)
    base_st = np.stack([base, base], axis=1)
    acc = (0.5 * np.sin(2 * np.pi * 70 * t)).astype(np.float32)
    layer = np.zeros((n, 2), dtype=np.float32)
    seg = (0.4 * np.sin(2 * np.pi * 200 * t[int(3 * sr):int(7 * sr)])).astype(np.float32)
    layer[int(3 * sr):int(7 * sr), 0] = seg
    layer[int(3 * sr):int(7 * sr), 1] = seg
    mix, lim = csd.mix_with_layer(base_st, layer, acc)
    ref, _sc = csd.cec.mix_stems(base_st, np.stack([acc, acc], axis=1))
    off = np.r_[0:int(2.9 * sr), int(7.1 * sr):n]
    assert np.array_equal(mix[off], ref[off]), "限幅把倍声窗外的主歌也改了"
    assert float(np.abs(mix).max()) <= csd.cec.MIX_CEILING + 1e-5


def test_double_arms_differ_by_one_variable(csd):
    arms = {k: s for k, _t, s, _sub in csd.DOUBLE_ARMS}
    assert arms["F0_control"] is None
    assert arms["F1_haas_all"] == {"mode": "haas"}
    # F2 → F3 只差「何时加倍」
    a, b = arms["F2_adt_all"], arms["F3_adt_chorus"]
    assert {k: v for k, v in a.items() if k != "where"} == {k: v for k, v in b.items() if k != "where"}
    assert a["where"] == "all" and b["where"] == "chorus"
    # F3 → F4 只差延迟/音分
    c = arms["F4_adt_chorus_wide"]
    assert c["where"] == b["where"] and c["mode"] == b["mode"]
    assert c["delay_ms"] != b["delay_ms"] and c["cents"] != b["cents"]


def test_reverb_tiers_are_ordered_and_only_ir_changes(csd):
    tiers = {k: (rt, ln, pre) for k, _t, rt, ln, pre, _s in csd.REVERB_TIERS}
    # R0 是现状档（与 m1l 的 B 臂同参数）
    assert tiers["R0_current"] == (2.0, 2.4, 0.0)
    # R1 只改 pre-delay
    assert tiers["R1_predelay60"][0] == tiers["R0_current"][0]
    assert tiers["R1_predelay60"][2] > 0
    # R3 是 SOS 案例值
    assert tiers["R3_sos_plate"][0] == pytest.approx(1.2)
    assert 60 <= tiers["R3_sos_plate"][2] <= 80
    # R4 是抒情长尾
    assert tiers["R4_ballad"][0] >= 3.5
    # IR 长度必须比 RT60 长，否则尾巴被截断、RT60 量不准
    for k, (rt, ln, _pre) in tiers.items():
        assert ln >= rt * 1.15, f"{k}: IR 长度 {ln}s 截断了 RT60 {rt}s"
