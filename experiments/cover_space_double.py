# -*- coding: utf-8 -*-
"""《好久不见》supp2026 成品 · 空间档位 + 「副歌限定 ADT 加倍」两组单变量对照（2026-10-18）。

### 为什么做这一页（承接 `m1l` / `m1m`）

`m1l` 的 F 臂是**全曲 Haas 双轨**（左右 13/19ms、−18dB）。`m1m` 调研把两处对上了号：

1. **空间档位可能选错**：我们现在的 IR 是 RT60 2.0s、早期反射第一拍在 11ms，
   更像「中厅」；SOS 的流行人声 plate 案例是 **1.2s + 60–80ms pre-delay**，
   抒情长尾是 3.5–5s。**pre-delay 明显偏短**。
2. **宽化方式与社区不一致**：社区做的是**只在副歌/重点词加倍**，
   ADT 延迟 80–120ms、微移调 ±4~8 cents；13/19ms 那个区间属于
   「Haas 加宽」（相位梳状 → 合唱感），不是「双轨」。

所以本页两组臂，**分开渲染、分开试听**（不混在一个 A/B 里，否则归因不清）：

| 组 | 臂 | 只改什么 |
|---|---|---|
| 空间 | R0 现状 2.0s/11ms · R1 只加 pre-delay 2.0s/60ms · R2 plate 1.5s/30ms · R3 SOS 1.2s/70ms · R4 抒情 3.5s/60ms | **只换 IR**，其余（干声、气口、混音增益）完全不动 |
| 加倍 | F0 对照（= E 密度臂）· F1 旧全曲 Haas 13/19ms · F2 全曲 ADT 96/104ms · F3 副歌 ADT 96/104ms · F4 副歌 ADT 80/120ms | **只在 E 臂上多加「一份加倍」**；F2/F3 只差「何时加倍」，F3/F4 只差延迟/音分档 |

`F1` 与 `R0` 都同时是**复现自检**：F1 必须逐样本等于 `m1l` 的 `F_wide_mix.wav`
（同一链、同一增益），R0 的指标必须与 `emotion/metrics.json` 的 `B_space` 对上 ——
否则说明我改了别的东西，整页不可信。

### 纪律

- **只写** `media/covers/lowsong_20261018/space_double/`（gitignore），不动产品链路、不重训；
- 混响/加倍的**读数只判「发生了什么」**（宽度、单声道损失、梳状深度、频段漂移），
  **不排序**；「副歌是不是真的厚了」由耳朵裁决（`listen_空间.html` / `listen_加倍.html`）；
- 加倍副本按旧 F 臂口径**叠在混响之后**（社区做法是共用一条 send，会让双影更融进空间，
  列为下一步，不在本页混进来）。

用法：
    python experiments/cover_space_double.py reverb   # 空间 5 臂 + 指标 + 页
    python experiments/cover_space_double.py double   # 加倍 5 臂 + 指标 + 页
    python experiments/cover_space_double.py all
    python experiments/cover_space_double.py check    # 只跑两组复现自检（不写大文件）
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import math
import subprocess
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cover_emotion_chain as cec  # noqa: E402  （复用同一条链，避免两套实现漂移）

SR = cec.SR
BASE = cec.BASE
OUT = BASE / "space_double"

#: 副歌窗（秒）。**一手依据**：LRC 时间轴（网易云 / 1ting 两处一致）与本机实测
#: onset 对账 —— 副歌首句「你会不会忽然的出现」LRC 68.05 / 166.40，
#: 实测（`supp_post_breath` 有声门 −17.6dB）68.17 / 166.52，偏差 +0.12s；
#: 全曲 20 句的中位偏差 +0.25s、IQR [−0.01, +0.57]。
#: 这里取 LRC 值并在两端各留 ≈0.3s；只当**加倍开关**用，零点几秒不影响段落判定。
CHORUS_WINDOWS = ((67.8, 126.2), (166.1, 225.8))

#: 开关淡入淡出：350ms。太快会在副歌第一拍听到「加倍突然进来」的台阶。
GATE_FADE_S = 0.35

#: 「重点词」权重：按 2s 平滑响度映射到 0.6–1.0（响的句子加倍更多）。
#: 2s 是**乐句**尺度，不是音节尺度 —— 300ms 级别的调制会让双影本身抽气。
EMPHASIS_RANGE = (0.60, 1.00)
EMPHASIS_WINDOW_S = 2.0

#: 加倍档位。延迟/音分/电平都取 `m1m` §4.3 给的范围，不自己发明：
#: ADT 80–120ms、±4~8 cents；电平 −6dB 是「双轨比主唱低一档」的常见做法
#: （SOS：双轨常比主轨低；Antares：doubles 略低于主唱，主唱必须仍是中心）。
ADT_GAIN_DB = -6.0
#: 声像：0.7 = 等功率下左 0.92 / 右 0.39。**不硬摇到 ±1**：
#: SOS 明确说对拍硬摇会让单声道掉约 3dB，慢歌的主唱不该付这个代价。
ADT_PAN = 0.7

#: 空间档位：(key, 标题, RT60, IR 长度, pre-delay, 说明)
REVERB_TIERS = [
    ("R0_current", "① 现状 · RT60 2.0s / pre-delay ≈11ms", 2.0, 2.4, 0.0,
     "m1l 的 B 臂原样：早期反射第一拍在 11ms，没有独立 pre-delay"),
    ("R1_predelay60", "② 只加 pre-delay · 2.0s / 60ms", 2.0, 2.4, 60.0,
     "单变量：RT60 不变，只把直达与空间拉开 60ms"),
    ("R2_plate", "③ plate · 1.5s / 30ms", 1.5, 1.9, 30.0,
     "m1l §7 写的那一档：更短、更靠前"),
    ("R3_sos_plate", "④ SOS 流行 plate · 1.2s / 70ms", 1.2, 1.6, 70.0,
     "SOS 案例值（1.2s + 60–80ms）"),
    ("R4_ballad", "⑤ 抒情长尾 · 3.5s / 60ms", 3.5, 4.2, 60.0,
     "SOS/Waves 的抒情大空间档，应明显更远"),
]

#: 加倍档位：mode=haas 走旧 F 臂；mode=adt 走社区做法。
#: `where` 决定开关：chorus = 只在副歌窗 + 重点句加权；all = 全曲常开。
#: ADT 臂的 `delay_ms` / `cents` 一律**显式写出**（不靠函数默认值）：
#: 单测要能直接读「哪两臂只差一个变量」，默认值藏在签格里会让这条纪律失守。
DOUBLE_ARMS = [
    ("F0_control", "① 对照 · E 臂（无加倍）", None,
     "D 空间 + C 推拉 + 密度，没有第二份人声"),
    ("F1_haas_all", "② 旧 F 臂 · 全曲 Haas 13/19ms", {"mode": "haas"},
     "m1l 的 F 臂原样（复现自检）"),
    ("F2_adt_all", "③ 全曲 ADT · 96/104ms ±6音分",
     {"mode": "adt", "where": "all", "delay_ms": (96.0, 104.0), "cents": (6.0, -6.0)},
     "只把延迟换成 ADT，仍然整曲铺满 → 单独看「何时加倍」这一维"),
    ("F3_adt_chorus", "④ 副歌 ADT · 96/104ms ±6音分",
     {"mode": "adt", "where": "chorus", "delay_ms": (96.0, 104.0), "cents": (6.0, -6.0)},
     "社区做法：只在副歌/重点词上加倍"),
    ("F4_adt_chorus_wide", "⑤ 副歌 ADT · 80/120ms ±8音分",
     {"mode": "adt", "where": "chorus", "delay_ms": (80.0, 120.0), "cents": (8.0, -8.0)},
     "把调研给的延迟/音分区间拉满，看边界在哪"),
]


# ---------------------------------------------------------------- 纯函数（可单测）

def section_mask(
    n_samples: int,
    sr: int = SR,
    windows=CHORUS_WINDOWS,
    fade_s: float = GATE_FADE_S,
    *,
    pad_s: float = 0.0,
) -> np.ndarray:
    """副歌窗 → 采样级开关（窗内 1、窗外 0，边缘升余弦渐变）。

    为什么用**掩码**而不是「渲染两遍再剪贴」：剪贴会在句中断开气口与混响尾巴，
    而且副歌首尾与主歌共享同一句尾音；掩码只改「叠不叠那一份倍声」，
    干声、气口、混响完全连续。

    `pad_s`：两端各延长一点（默认 0）。渲染时用负值把门**提前关上**
    （= `pad_s < 0`），保证门外的样本与对照臂逐位相同 —— 倍声本身有
    80–120ms 延迟，门若只在窗内开，延迟会把倍声尾巴推出窗外。
    """
    n = max(0, int(n_samples))
    g = np.zeros(n, dtype=np.float32)
    if n == 0:
        return g
    f = max(1, int(round(fade_s * sr)))
    pad = float(pad_s)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, math.pi, f, dtype=np.float64))
    for a, b in windows:
        a2, b2 = a - pad, b + pad
        if b2 <= a2:
            continue
        i0, i1 = int(round(a2 * sr)), int(round(b2 * sr))
        i0, i1 = max(0, i0), min(n, i1)
        if i1 <= i0:
            continue
        g[i0:i1] = 1.0
        # 两端各用一个半窗升余弦（取 min，避免两窗重叠处 >1）
        g[i0:min(i1, i0 + f)] = np.minimum(g[i0:min(i1, i0 + f)], ramp[: min(f, i1 - i0)])
        tail = g[max(i0, i1 - f):i1]
        g[max(i0, i1 - f):i1] = np.minimum(tail, ramp[::-1][: len(tail)])
    return np.clip(g, 0.0, 1.0)


def emphasis_curve(x_mono: np.ndarray, sr: int = SR,
                   window_s: float = EMPHASIS_WINDOW_S,
                   limits: tuple[float, float] = EMPHASIS_RANGE) -> np.ndarray:
    """按**乐句尺度**响度生成「重点词」权重（`limits[0]`–`limits[1]`，中位句≈1.0）。

    社区原话是「只在副歌/重点词上加倍」。重点到什么程度没有客观定义，
    这里取一个**可复算**的代理：2s 平滑后的响度（dB）相对全曲中位的 ±3dB
    线性映射到区间两端 —— 响的句子加倍多、轻的句子少，且不做音节级抖动。
    """
    x = np.asarray(x_mono, dtype=np.float64)
    lo, hi = float(limits[0]), float(limits[1])
    if x.size == 0:
        return np.zeros(0, dtype=np.float32)
    from scipy.ndimage import uniform_filter1d

    win = max(1, int(round(window_s * sr)))
    env = uniform_filter1d(x ** 2, size=min(win, x.size), mode="nearest")
    db = 10.0 * np.log10(env + 1e-12)
    active = env > np.percentile(env, 20)
    ref = float(np.median(db[active])) if active.any() else float(np.median(db))
    t = np.clip((db - ref + 3.0) / 6.0, 0.0, 1.0)
    return (lo + (hi - lo) * t).astype(np.float32)


def detune(x_mono: np.ndarray, sr: int = SR, cents: float = 0.0) -> np.ndarray:
    """整段移调 `cents` 音分（长度不变）。

    为什么不用纯重采样：重采样会**同时改速度**（250s 变 249s），叠回干声就是
    全程相位漂移，正是我们要避免的「合唱感」。这里用 librosa 的相位声码器，
    实测 44.1k 下 +4/+6/+8/−4/−8 音分的误差 ≤0.06 音分（`_tmp_pitchacc.py`），
    长度逐样本保持。

    ⚠️ `import librosa` 本身会触发 `importlib.resources.path` 的 DeprecationWarning，
    而项目的 pytest 配置把 DeprecationWarning 升级成 error（pyproject
    `filterwarnings`）。librosa 是**可选依赖**、这条警告是第三方的事，
    所以在**首次导入**时局部屏蔽它 —— 不给整个测试套放开 DeprecationWarning
    （那会放过本项目自己的废弃用法），也不要求测试用假 librosa 顶替。
    """
    y = np.asarray(x_mono, dtype=np.float32)
    if y.size == 0 or abs(cents) < 1e-6:
        return y.copy()
    return _pitch_shift(y, sr, cents)


def _pitch_shift(y: np.ndarray, sr: int, cents: float) -> np.ndarray:
    """`librosa.effects.pitch_shift`，把**导入与调用**都包在同一个警告屏蔽里。

    ⚠️ 两次踩：librosa 是 lazy_loader，`import librosa` 只是注册，
    真正导入 `librosa.effects` → `librosa.core.intervals` 发生在**第一次属性访问**；
    那一刻 `importlib.resources.path` 会发 DeprecationWarning，而本项目把
    DeprecationWarning 升级成 error（pyproject `filterwarnings`）。
    所以屏蔽范围必须覆盖 `librosa.effects.pitch_shift(...)` 这一次调用，
    只包住 `import librosa` 不够（2026-10-18 实测：4 条用例仍红）。
    """
    import warnings

    with warnings.catch_warnings():
        # 只压第三方（librosa/lazy_loader/importlib）的废弃警告；本项目自己的
        # 废弃用法仍应照常报错 —— 所以不用全局 ignore。
        warnings.simplefilter("ignore", DeprecationWarning)
        import librosa

        # ★ `n_steps` 的单位是 `1/bins_per_octave` 个八度：这里取 1200，
        #   于是 **n_steps 直接就是音分数**。传 `cents/100` 只会移 1/100 音分
        #   （2026-10-18 自测抓到：+6 音分被写成 0.06 音分，听感上「没去相关」）。
        out = librosa.effects.pitch_shift(y, sr=sr, n_steps=cents,
                                          bins_per_octave=1200)
    return np.asarray(out, dtype=np.float32)


def adt_double(
    dry_stereo: np.ndarray,
    sr: int = SR,
    *,
    delay_ms: tuple[float, float] = (96.0, 104.0),
    cents: tuple[float, float] = (6.0, -6.0),
    gain_db: float = ADT_GAIN_DB,
    pan: float = ADT_PAN,
    gate: np.ndarray | None = None,
    emphasis: np.ndarray | None = None,
    stats: dict | None = None,
) -> np.ndarray:
    """把一份「自动双轨」叠到干声上：左 +cents/延迟 A，右 −cents/延迟 B。

    与 Haas 加宽的关键区别在**延迟长度**：13–19ms 的副本在 5ms 尺度上与干声
    相干，听感是「变宽 + 梳状染色」；80–120ms 的副本已经越过人耳的回声/融合
    边界，听感是「另一个人在同时唱同一句」。这也是社区 ADT 的做法。

    `gate` / `emphasis` 都乘在**叠的那一份**上，干声一根采样都不动 ——
    所以关掉的地方与对照臂逐样本相同（单测 `test_gate_closed_is_exact_passthrough`）。
    """
    x = np.asarray(dry_stereo, dtype=np.float32)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    if x.size == 0:
        return x.copy()
    n = len(x)
    g = np.ones(n, dtype=np.float32)
    if gate is not None:
        g *= np.asarray(gate, dtype=np.float32)[:n]
    if emphasis is not None:
        g *= np.asarray(emphasis, dtype=np.float32)[:n]
    g = np.clip(g, 0.0, 1.0)
    gain = 10.0 ** (gain_db / 20.0)

    mono = x.mean(axis=1)
    # 等功率声像：pan=0 居中、1 全偏。0.7 → 左 0.92 / 右 0.39。
    gl = math.sqrt(max(0.0, (1.0 + pan) / 2.0))
    gr = math.sqrt(max(0.0, (1.0 - pan) / 2.0))

    out = x.astype(np.float64).copy()
    for ch_gain, ms, c in ((gl, delay_ms[0], cents[0]), (gr, delay_ms[1], cents[1])):
        d = int(round(ms * sr / 1000.0))
        if d >= n:
            continue
        copy = detune(mono, sr, c) * gain * ch_gain
        # ★ 门要按**输出时刻**来关：`out[t] += copy[t-d]`，所以 `copy[i]` 要乘
        #   `g[i+d]`（把门**提前** d 个采样），而不是 `g[i]`。差这一处，副歌结束
        #   后会多出 100ms 的「另一个人还在唱」；若反过来把 `g` 往后推，门外的
        #   采样就会被改（2026-10-18 两条断言各抓到一次，方向写反了两次）。
        copy = copy * _gate_advanced(g, d)
        out[d:, 0] += copy[: n - d]
        out[d:, 1] += copy[: n - d]
    peak = float(np.abs(out).max()) if out.size else 0.0
    # ★ 超顶时**只压倍声层，而且只压超顶的那几个采样**：
    #   以前写的是 `out *= ceiling/peak`，那会把门外的干声一起按 0.96 缩掉
    #   （实测 F2/F3 的 mix_scale 0.976–0.988，主歌与对照的差里有一半其实是缩放）。
    #   改成"整层等比缩"也不行：一处峰值就会把整首的倍声压掉 6dB
    #   （2026-10-18 实测 F2/F3 的 α=0.47），那样「加倍够不够厚」这一维就被尺度吃了。
    #   所以只在**超顶的采样附近**做局部增益衰减（5ms 起落），倍声在其余时间保持原电平。
    layer = out - x.astype(np.float64)
    if peak > cec.MIX_CEILING:
        x_peak = float(np.abs(x).max())
        if x_peak > cec.MIX_CEILING:
            # 干声自己就超顶：这是**输入**问题，压倍声救不了（压到 0 也还是超）。
            # 交给 `write_wav` 的整体防削波，并把原因记下来，不假装解决了。
            if stats is not None:
                stats["layer_limit"] = {"reason": "干声本身已超 ceiling", "scale": 1.0}
        else:
            layer, lim = _limit_layer(x, layer, ceiling=cec.MIX_CEILING, sr=sr)
            out = x.astype(np.float64) + layer
            if stats is not None:
                stats["layer_limit"] = lim
    # 门外之所以能**逐位**保持：那些位置上倍声恰好乘了 0.0，
    # float32 → float64 累加 0.0 → 再转回 float32 是精确往返。
    return out.astype(np.float32)


def _limit_layer(x: np.ndarray, layer: np.ndarray, *, ceiling: float,
                 sr: int) -> tuple[np.ndarray, dict]:
    """只对**超顶采样附近**的倍声做增益衰减，其余位置原样保留。

    做法：每个采样上解 `|x + k·layer| ≤ ceiling` 的上界 `k ≤ (ceiling−|x|)/|layer|`，
    再用 5ms 窗的**最小值滤波**把衰减扩到邻近采样（避免只削单点导致波形毛刺）。
    最小值滤波只会让衰减更狠，不会把需求抬回去，所以上界始终成立。
    """
    from scipy.ndimage import minimum_filter1d

    s = x.astype(np.float64) + layer
    mag = np.abs(s)
    over = mag > ceiling
    if not over.any():
        return layer, {"scale": 1.0, "limited_samples": 0, "over_samples": 0}
    # ★ 单点需求的正确解法：`|x + k·layer| ≤ |x| + k·|layer| ≤ ceiling`
    #   ⇒ `k ≤ (ceiling − |x|) / |layer|`。
    #   之前写的是 `ceiling/|x+layer|`，那在「干声自己就很大」的地方永远不够
    #   （实测 x=0.90、layer=0.29 时它给 k=0.82，叠加后仍是 1.14 —— 干声占了大头，
    #   只压倍声根本压不下去），必须按**剩余余量**算。
    x_abs = np.abs(x.astype(np.float64))
    l_abs = np.abs(layer)
    need = np.ones_like(mag)
    room = np.clip(ceiling - x_abs, 0.0, None)
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = np.where(l_abs > 1e-12, room / np.maximum(l_abs, 1e-12), 1.0)
    need = np.minimum(np.ones_like(raw), raw)
    need = np.minimum(need, np.where(over, need, 1.0))
    k = max(1, int(round(0.005 * sr)))  # 5ms
    # ★ 两通道各自沿**时间轴**（axis=0）滤波：`mag` 是 (n, 2)，
    #   直接 `need[:, None]` 会变成 (n,1,2) 再和 (n,2) 广播成 (n,n,2)（95GB，实测 OOM）。
    #   最小值滤波只能让衰减更狠，不会破坏上面的上界。
    need = minimum_filter1d(need, size=k, mode="nearest", axis=0)
    limited = layer * need
    s2 = x.astype(np.float64) + limited
    left = int(np.sum(np.abs(s2) > ceiling))
    scale = 1.0
    if left:
        # 平滑之后仍有零星超顶（相邻采样需求差太大）。这一步是**兜底**，
        # 全局等比缩倍声层；记下来，避免「看着有数据、其实整层被压了」。
        scale = float(ceiling / max(np.abs(s2).max(), 1e-12))
        limited *= scale
        s2 = x.astype(np.float64) + limited
        left = int(np.sum(np.abs(s2) > ceiling))
    return limited, {
        "scale": round(scale, 4),
        "over_samples": int(over.sum()),
        "limited_samples": int(np.sum(need < 0.999)),
        "residual_over": left,
        "max_gain_reduction_db": round(float(20 * math.log10(float(need.min()) + 1e-12)), 2),
    }


def _gate_advanced(g: np.ndarray, d: int) -> np.ndarray:
    """把开关 `g` **提前** `d` 个采样（`out[i] = g[i+d]`，尾部补零）。

    用途：`out[t] = copy[t-d]`，而 copy 里乘的是提前后的门 ——
    这样倍声只在**输出时刻**落在窗内时存在。
    """
    g = np.asarray(g, dtype=np.float32)
    if d <= 0:
        return g
    out = np.zeros_like(g)
    if d < len(g):
        out[: len(g) - d] = g[d:]
    return out


# ---------------------------------------------------------------- IR 测量

def measure_ir(ir: np.ndarray, sr: int = SR) -> dict:
    """量一条 IR：pre-delay（首达时间）、RT60（Schroeder 积分）、前 80ms 能量占比。

    为什么要量而不是照抄参数：`make_ir` 的 pre-delay 是**把整条 IR 往后推**，
    早反射时间、单位能量归一化都在之后做 —— 只有从 IR 本身量出来，
    才能证明「这一档真的是 60ms，不是 11ms 又被归一化吃了」。

    RT60：把能量曲线积分成 dB，取 −5…−35dB 区间做最小二乘斜率，×3 得 RT60。
    `early80_db`：首达之后 80ms 内能量占全 IR 的百分比（dB）——
    这是**去掉 pre-delay 影响**后的「前重后轻」程度：plate 前重、大厅后重。
    """
    x = np.asarray(ir, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    if x.size == 0:
        return {"pre_delay_ms": None, "rt60_s": None, "early80_db": None}
    env = (x ** 2).sum(axis=1)
    total = float(env.sum())
    if total <= 1e-20:
        return {"pre_delay_ms": None, "rt60_s": None, "early80_db": None}
    # 首达：累计能量到 1% 的时刻。用 `argmax` 会读到**最早那根反射**（11ms），
    # 而听感上的「直达→空间」间隔是**能量开始成规模**的时刻（2026-10-18 实测：
    # pre=0 时 argmax 给 23ms、累计 1% 给 7.7ms；pre=60 时分别 85ms / 67ms）。
    cum = np.cumsum(env)
    onset = int(np.argmax(cum >= total * 0.01))
    pre_ms = round(onset * 1000.0 / sr, 1)
    # Schroeder 反向积分（从尾到头累加），用 dB 拟合斜率
    edc = np.cumsum(env[::-1])[::-1]
    edc_db = 10.0 * np.log10(edc / (edc[0] + 1e-30) + 1e-30)
    idx = np.where((edc_db <= -5.0) & (edc_db >= -35.0))[0]
    rt60 = None
    if len(idx) > 8:
        tt = idx / sr
        a, b = np.polyfit(tt, edc_db[idx], 1)
        if a < -1e-6:
            rt60 = round(float(-60.0 / a), 2)
    head = float(env[onset: onset + int(0.08 * sr)].sum())
    total = float(env[onset:].sum())
    early = round(10.0 * math.log10((head + 1e-30) / (total + 1e-30)) + 10.0, 1) if total > 0 else None
    return {"pre_delay_ms": pre_ms, "rt60_s": rt60, "early80_db": early}


# ---------------------------------------------------------------- 段落/宽度指标

def _frame_rms_db(y: np.ndarray, sr: int, win_s: float = 0.10, hop_s: float = 0.05):
    win, hop = max(1, int(win_s * sr)), max(1, int(hop_s * sr))
    if len(y) < win:
        return np.array([10 * math.log10(float(np.mean(y ** 2)) + 1e-12)]), hop / sr
    frames = np.lib.stride_tricks.sliding_window_view(y, win)[::hop]
    return 10 * np.log10((frames ** 2).mean(axis=1) + 1e-12), hop / sr


def section_stats(stereo: np.ndarray, sr: int, window: tuple[float, float]) -> dict:
    """一段内的电平、声场宽度、单声道折叠损失、谱纹波。

    `side_mid_db` = 侧/中能量比（dB）。单声道素材是 None（不是 −220dB），
    与 `m1l` 页面同口径：数值正确但读起来像 bug 的东西不印给用户。
    `mono_loss_db` = 20log10(rms((L+R)/2) / rms(立体声)) —— 折叠成单声道掉多少。
    对逐样本相同的 L/R 恒为 0；对反相副本会掉到底。**这是「加宽的代价」**。
    `ripple_db` = 200–5000Hz 长时谱去掉 1/3 倍频程趋势后的残差 std ——
    梳状滤波（13/19ms 的双轨）会把它抬高，间距够宽的 ADT 不会。
    """
    x = np.asarray(stereo, dtype=np.float32)
    if x.ndim == 1:
        x = np.stack([x, x], axis=1)
    a, b = window
    i0, i1 = max(0, int(a * sr)), min(len(x), int(b * sr))
    seg = x[i0:i1]
    if seg.size == 0:
        return {"rms_db": None, "side_mid_db": None, "mono_loss_db": None, "ripple_db": None}
    mono = seg.mean(axis=1)
    rms = float(np.sqrt(np.mean(mono ** 2) + 1e-30))
    rms_st = float(np.sqrt(np.mean((seg[:, 0] ** 2 + seg[:, 1] ** 2) / 2.0) + 1e-30))
    mid = (seg[:, 0] + seg[:, 1]) / 2.0
    side = (seg[:, 0] - seg[:, 1]) / 2.0
    side_db = 20 * math.log10((np.sqrt(np.mean(side ** 2)) + 1e-12)
                              / (np.sqrt(np.mean(mid ** 2)) + 1e-12))
    return {
        "rms_db": round(20 * math.log10(rms + 1e-12), 1),
        "side_mid_db": None if side_db < -60 else round(float(side_db), 1),
        "mono_loss_db": round(float(20 * math.log10((rms + 1e-12) / (rms_st + 1e-12))), 2),
        "ripple_db": _spectral_ripple(mono, sr),
    }


def _spectral_ripple(x: np.ndarray, sr: int = SR) -> float | None:
    """长时幅度谱在 200–5000Hz 上的「梳状残差」（dB）。

    做法：只取有声帧（避免间奏/静音把谱拉平），Hann 窗 4096；把谱插值到
    每倍频程 24 点的对数频率轴，减去 1/3 倍频程滑动均值，取残差 std。
    这是**相对**读数：同一段在不同臂之间比较才有意义。
    """
    x = np.asarray(x, dtype=np.float64)
    win = 4096
    if x.size < win * 4:
        return None
    hop = win // 2
    frames = np.lib.stride_tricks.sliding_window_view(x, win)[::hop]
    if frames.size == 0:
        return None
    energy = (frames ** 2).mean(axis=1)
    keep = frames[energy > np.percentile(energy, 40)]
    if len(keep) < 4:
        return None
    spec = np.abs(np.fft.rfft(keep * np.hanning(win), axis=1)).mean(axis=0)
    freqs = np.fft.rfftfreq(win, 1.0 / sr)
    lo, hi = 200.0, 5000.0
    grid = lo * 2.0 ** (np.arange(0.0, 24.0 * math.log2(hi / lo) + 1.0) / 24.0)
    vals = np.interp(grid, freqs, 20 * np.log10(spec + 1e-12))
    k = 7  # 7 × (1/24 倍频程) ≈ 1/3 倍频程
    if len(vals) <= k + 4:
        return None
    trend = np.convolve(vals, np.ones(k) / k, mode="valid")
    resid = vals[k // 2: k // 2 + len(trend)] - trend
    return round(float(resid.std()), 2)


# ---------------------------------------------------------------- 渲染

def _load_inputs():
    vocal = cec.read_audio(cec.VOCAL, SR, stereo=False)
    acc = cec.read_audio(cec.ACC, SR, stereo=True)
    src = cec.read_audio(BASE / "原曲_人声.wav", SR, stereo=False)
    n = min(len(vocal), len(acc), len(src))
    return vocal[:n], acc[:n], src[:n]


def render_reverb(vocal, acc, src, tier) -> tuple[np.ndarray, np.ndarray, float | None, dict]:
    _key, _title, rt60, length, pre_ms, _sub = tier
    ir = cec.make_ir(sr=SR, rt60=rt60, length_s=length, pre_delay_ms=pre_ms)
    v_st, mix, wet_db = cec.run_arm(vocal, acc, ir, src, {"reverb": True})
    return v_st, mix, wet_db, measure_ir(ir, SR)


def double_evidence(reference: np.ndarray, arm: np.ndarray, sr: int = SR,
                    window: tuple[float, float] | None = None,
                    search_ms: tuple[int, int] = (5, 140)) -> dict:
    """把「加倍」从成品里**量出来**：差值包络 vs 干声包络的延迟扫描。

    为什么不能用波形相关：±6 音分的移调会让副本相对干声持续相移，50s 窗的
    波形相关平均到 0 —— 那是移调在正常工作，不是没有加倍
    （2026-10-18 实测：96ms 处波形相关 −0.035，看着像「什么都没加」）。
    包络不受相位影响，峰应落在合成时用的延迟上（实测 91–109ms 一带出峰，
    因为 ADT 的两个通道 96/104ms 相邻、包络又是 1.5ms 分辨率）。
    """
    ref = np.asarray(reference, dtype=np.float64)
    got = np.asarray(arm, dtype=np.float64)
    n = min(len(ref), len(got))
    diff = got[:n] - ref[:n]
    a, b = window if window else (0.0, n / sr)
    i0, i1 = max(0, int(a * sr)), min(n, int(b * sr))
    if i1 - i0 < sr:
        return {"ok": False, "reason": "段太短"}
    win = 64  # ≈1.5ms @44.1k
    from scipy.ndimage import uniform_filter1d

    eb = np.sqrt(uniform_filter1d(ref[:n] ** 2, win, mode="nearest") + 1e-18)
    ed = np.sqrt(uniform_filter1d(diff ** 2, win, mode="nearest") + 1e-18)
    seg = ed[i0:i1]
    if float(np.sqrt((diff[i0:i1] ** 2).mean())) < 1e-5:
        return {"ok": False, "reason": "这段没有加倍（差值≈0）",
                "diff_rms_db": round(float(20 * math.log10(
                    float(np.sqrt((diff[i0:i1] ** 2).mean())) + 1e-12)), 1)}
    # ★ 用点积相关系数，不要 `np.corrcoef(x, y)`：后者对两个 1-D 长向量会按
    #   (n×n) 协方差矩阵算，50s 段直接向系统要 193 TB（2026-10-18 实测 OOM）。
    seg_c = seg - seg.mean()
    seg_n = float(np.sqrt((seg_c ** 2).sum()) + 1e-18)
    best: list[tuple[float, int]] = []
    for d_ms in range(int(search_ms[0]), int(search_ms[1]) + 1):
        d = int(round(d_ms * sr / 1000.0))
        if i0 - d < 0 or i1 - d > n:
            continue
        ref_seg = eb[i0 - d:i1 - d]
        ref_c = ref_seg - ref_seg.mean()
        denom = seg_n * float(np.sqrt((ref_c ** 2).sum()) + 1e-18)
        if denom <= 0:
            continue
        c = float((ref_c * seg_c).sum() / denom)
        if math.isfinite(c):
            best.append((c, d_ms))
    best.sort(reverse=True)
    lead_db = 20 * math.log10(float(np.sqrt((ref[i0:i1] ** 2).mean())) + 1e-12)
    diff_db = 20 * math.log10(float(np.sqrt((diff[i0:i1] ** 2).mean())) + 1e-12)
    return {
        "ok": True,
        "peak_delay_ms": best[0][1] if best else None,
        "peak_corr": round(best[0][0], 3) if best else None,
        "top": [{"delay_ms": d, "corr": round(c, 3)} for c, d in best[:4]],
        "diff_rms_db": round(float(diff_db), 1),
        "lead_rms_db": round(float(lead_db), 1),
        "diff_vs_lead_db": round(float(diff_db - lead_db), 1),
    }


def render_double(vocal, acc, src, spec) -> tuple[np.ndarray, np.ndarray, dict]:
    """在 E 臂（空间+推拉+密度）之上加「一份加倍」；spec=None 时就是 E 臂本身。"""
    ir = cec.make_ir(sr=SR)  # 与 m1l 完全同一条 IR（现状档），单变量
    v_st, _mix, _wet = cec.run_arm(vocal, acc, ir, src,
                                   {"reverb": True, "automation": True, "density": True})
    info: dict = {"mode": None}
    if spec is None:
        out = v_st
    elif spec["mode"] == "haas":
        out = cec.widen(v_st, SR)  # 13/19ms、0.12：与 m1l 的 F 臂逐样本相同
        info = {"mode": "haas", "delay_ms": list(cec.WIDE_DELAY_MS),
                "cents": [0.0, 0.0], "gain_db": None, "where": "all"}
    else:
        delay = tuple(spec.get("delay_ms", (96.0, 104.0)))
        cents = tuple(spec.get("cents", (6.0, -6.0)))
        where = spec.get("where", "chorus")
        n = len(v_st)
        # ★ 门在**渲染时**两端各收 0.15s（`pad_s=-0.15`）：倍声本身带着
        #   80–120ms 延迟，门若画在窗内，延迟会把倍声尾巴推出窗外 →
        #   主歌就不再与对照臂逐样本相同。窗外必须干净，否则「只在副歌加倍」
        #   在听感上会变成「主歌也隐约有第二个人」。
        gate = section_mask(n, SR, pad_s=-0.15) if where == "chorus" else None
        emph = emphasis_curve(v_st.mean(axis=1), SR) if where == "chorus" else None
        dstats: dict = {}
        out = adt_double(v_st, SR, delay_ms=delay, cents=cents,
                         gain_db=ADT_GAIN_DB, pan=ADT_PAN, gate=gate, emphasis=emph,
                         stats=dstats)
        info = {"mode": "adt", "delay_ms": [round(float(d), 1) for d in delay],
                "cents": [round(float(c), 1) for c in cents], "gain_db": ADT_GAIN_DB,
                "where": where, "pan": ADT_PAN,
                "gate_coverage": round(float((section_mask(n, SR, pad_s=-0.15) > 0.5).mean()), 3)}
        if "layer_limit" in dstats:
            info["layer_limit"] = dstats["layer_limit"]
        if gate is not None:
            info["emphasis_mean"] = round(float(emph.mean()), 3)
            info["gate_span_s"] = [[round(a, 1), round(b, 1)] for a, b in CHORUS_WINDOWS]
    mix, scale = cec.mix_stems(out, acc)
    info["mix_scale"] = round(float(scale), 4)
    # ★ 加倍组的混音**不许整体缩**：`mix_stems` 一超顶就等比缩全部（实测 F2/F3
    #   的 mix_scale 0.944/0.955），主歌跟着变轻 0.4dB —— 读者会把「副歌更厚」
    #   和「整首更轻」混在一起。这里改成：基线混音原样，只把**倍声那一层**
    #   按剩余余量局部限幅，于是主歌的成品与对照臂**逐位**相同。
    if spec is not None and spec.get("mode") == "adt":
        layer = out - v_st
        mix2, lim = mix_with_layer(v_st, layer, acc)
        info["mix_layer_limit"] = lim
        # 成品层面没有整体缩放（=1.0）；把「如果没做层限幅会缩多少」留给
        # `mix_scale` 这个字段，便于和空间组/旧页对照。
        info["mix_scale_naive"] = info["mix_scale"]
        info["mix_scale"] = 1.0
        return out, mix2, info
    return out, mix, info


def mix_with_layer(
    base_vocal: np.ndarray,
    layer: np.ndarray,
    acc: np.ndarray,
    *,
    vocal_gain: float = cec.VOCAL_GAIN,
    acc_gain: float = cec.ACC_GAIN,
    ceiling: float = cec.MIX_CEILING,
) -> tuple[np.ndarray, dict]:
    """成品 = vocal_gain·(base_vocal + layer) + acc_gain·acc，但**只对 layer 限幅**。

    这样 `layer=0` 的位置（主歌/间奏）与对照臂的成品逐位相同 —— 单变量纪律在
    成品层面也成立，读者听到的差别只来自副歌那 46.6% 的时间。
    """
    n = min(len(base_vocal), len(acc))
    v0 = np.asarray(base_vocal[:n], dtype=np.float64)
    ly = np.asarray(layer[:n], dtype=np.float64)
    a = np.asarray(acc[:n], dtype=np.float64)
    # 伴奏可能是单声道（本曲 `acc.wav` 就是）：与 `mix_stems` 同口径复制成双声道，
    # 否则 (n,2) 的人声和 (n,) 的伴奏在广播上直接报错（2026-10-18 单测抓到）。
    if v0.ndim > 1 and a.ndim == 1:
        a = np.repeat(a[:, None], v0.shape[1], axis=1)
    elif v0.ndim > 1 and a.ndim > 1 and a.shape[1] == 1 and v0.shape[1] > 1:
        a = np.repeat(a, v0.shape[1], axis=1)
    base = vocal_gain * v0 + acc_gain * a
    scaled_layer = vocal_gain * ly
    out = base + scaled_layer
    peak = float(np.abs(out).max()) if out.size else 0.0
    if peak <= ceiling:
        return out.astype(np.float32), {"scale": 1.0, "over_samples": 0}
    limited, lim = _limit_layer(base, scaled_layer, ceiling=ceiling, sr=SR)
    return (base + limited).astype(np.float32), lim


def _arm_metrics(v_st, mix, acc, src, wet_db, extra) -> dict:
    m = cec.analyze(v_st, mix, acc, src, wet_db)
    m.update(extra)
    return m


def run_group(group: str) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    vocal, acc, src = _load_inputs()
    print(f"[输入] 干声 {len(vocal)/SR:.1f}s · 伴奏 {len(acc)/SR:.1f}s · 源 {len(src)/SR:.1f}s")
    arms: dict[str, str] = {}
    metrics: dict[str, dict] = {}
    ir_metrics: dict[str, dict] = {}

    if group == "reverb":
        tiers = REVERB_TIERS
    else:
        tiers = []
    for tier in tiers:
        key, title, *_ = tier
        v_st, mix, wet_db, ir_m = render_reverb(vocal, acc, src, tier)
        write_arm(key, v_st, mix)
        arms[key] = str(OUT / f"{key}_mix.wav")
        ir_metrics[key] = ir_m
        metrics[key] = _arm_metrics(v_st, mix, acc, src, wet_db, {
            "ir": ir_m,
            "chorus": section_stats(v_st, SR, CHORUS_WINDOWS[0]),
            "verse": section_stats(v_st, SR, (14.8, 66.0)),
        })
        print(f"[{key}] {title} · IR {ir_m} · " + json.dumps(metrics[key], ensure_ascii=False))

    doubles = DOUBLE_ARMS if group == "double" else []
    stems: dict[str, np.ndarray] = {}
    for key, title, spec, _sub in doubles:
        v_st, mix, info = render_double(vocal, acc, src, spec)
        write_arm(key, v_st, mix)
        stems[key] = v_st
        arms[key] = str(OUT / f"{key}_mix.wav")
        chorus = section_stats(v_st, SR, CHORUS_WINDOWS[0])
        verse = section_stats(v_st, SR, (14.8, 66.0))
        metrics[key] = _arm_metrics(v_st, mix, acc, src, None, {
            "double": info,
            "chorus": chorus,
            "verse": verse,
        })
        print(f"[{key}] {title} · " + json.dumps(metrics[key], ensure_ascii=False))

    # 加倍组：把「加倍到底有没有加进去 / 加在哪」量出来（差值包络的延迟扫描）
    if group == "double" and "F0_control" in stems:
        ref = stems["F0_control"]
        for key in list(metrics):
            if key == "F0_control":
                continue
            m = metrics[key]
            m["double"]["evidence_chorus"] = double_evidence(ref, stems[key], SR,
                                                             window=CHORUS_WINDOWS[0])
            m["double"]["evidence_verse"] = double_evidence(ref, stems[key], SR,
                                                            window=(14.8, 66.0))
            print(f"[证据·{key}] " + json.dumps({
                "副歌": m["double"]["evidence_chorus"], "主歌": m["double"]["evidence_verse"],
            }, ensure_ascii=False))

    # 加倍组补「相对对照」的读数（加了多少、哪里加）
    if group == "double" and "F0_control" in metrics:
        base = metrics["F0_control"]
        for key in list(metrics):
            m = metrics[key]
            for sec in ("chorus", "verse"):
                a, b = m[sec].get("rms_db"), base[sec].get("rms_db")
                m[sec]["lift_vs_F0_db"] = None if a is None or b is None else round(a - b, 2)
    # 空间组补「相对 R0」的读数
    if group == "reverb" and "R0_current" in metrics:
        base = metrics["R0_current"]
        for key in list(metrics):
            m = metrics[key]
            for sec in ("chorus", "verse"):
                a, b = m[sec].get("side_mid_db"), base[sec].get("side_mid_db")
                if a is not None and b is not None:
                    m[sec]["side_vs_R0_db"] = round(a - b, 2)
                a2, b2 = m[sec].get("rms_db"), base[sec].get("rms_db")
                m[sec]["wet_gain_vs_R0_db"] = None if a2 is None or b2 is None else round(a2 - b2, 2)

    (OUT / f"arms_{group}.json").write_text(json.dumps(arms, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / f"metrics_{group}.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
    if ir_metrics:
        (OUT / "ir_metrics.json").write_text(json.dumps(ir_metrics, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    return arms


def write_arm(key: str, v_st: np.ndarray, mix: np.ndarray) -> None:
    cec.write_wav(OUT / f"{key}_vocal.wav", v_st)
    cec.write_wav(OUT / f"{key}_mix.wav", mix)


# ---------------------------------------------------------------- 复现自检

def self_check() -> dict:
    """两组各一条「必须与原页逐样本对得上」的自检。

    1. R0 的指标 == `emotion/metrics.json` 的 B_space（±0.1）；
    2. F1 的混音 == `emotion/F_wide_mix.wav`（相关系数 >0.9999，max|diff|<1e-3）。

    第 2 条能抓住「加倍顺序/增益/IR 被顺手改了」这类静默错误 ——
    这类错只会让听感「好像不太一样」，日志里全绿。
    """
    out: dict = {}
    emo = BASE / "emotion"
    old_metrics = json.loads((emo / "metrics.json").read_text(encoding="utf-8"))
    vocal, acc, src = _load_inputs()
    v_st, mix, wet_db, ir_m = render_reverb(vocal, acc, src, REVERB_TIERS[0])
    fresh = _arm_metrics(v_st, mix, acc, src, wet_db, {})
    keys = ("lra", "dyn_std", "band_8k", "sfm", "centroid", "clip")
    diffs = {k: round(float(fresh[k] - old_metrics["B_space"][k]), 4) for k in keys}
    out["R0_vs_B_space"] = {"diff": diffs,
                            "ok": all(abs(v) <= 0.1 for v in diffs.values())}
    out["R0_ir"] = ir_m

    v_st2, mix2, info = render_double(vocal, acc, src, {"mode": "haas"})
    old_wav = cec.read_audio(emo / "F_wide_mix.wav", SR, stereo=True)
    n = min(len(mix2), len(old_wav))
    a = np.asarray(mix2[:n], dtype=np.float64).ravel()
    b = np.asarray(old_wav[:n], dtype=np.float64).ravel()
    corr = float(np.corrcoef(a, b)[0, 1])
    maxdiff = float(np.abs(a - b).max())
    out["F1_vs_F_wide"] = {"corr": round(corr, 6), "max_abs_diff": round(maxdiff, 6),
                           "ok": corr > 0.9999 and maxdiff < 1e-3}
    out["F1_info"] = info
    return out


# ---------------------------------------------------------------- 页面

def _ffmpeg() -> str:
    from common import find_ffmpeg

    return find_ffmpeg()


def _audio_tag(path: Path, key: str, bitrate: str = "128k") -> str:
    tmp = OUT / f".{key}.page.mp3"
    subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-i", str(path),
                    "-b:a", bitrate, str(tmp)], check=True, timeout=300)
    b64 = "data:audio/mpeg;base64," + base64.b64encode(tmp.read_bytes()).decode()
    tmp.unlink(missing_ok=True)
    return f'<audio controls preload="metadata" src="{b64}"></audio>'


def _fmt(v, unit="", nd=1):
    return "—" if v is None else f"{v:.{nd}f}{unit}"


def build_page(group: str, arms: dict[str, str], metrics: dict[str, dict]) -> Path:
    is_rev = group == "reverb"
    rows = ([(k, t, sub) for k, t, _rt, _ln, _pd, sub in REVERB_TIERS] if is_rev
            else [(k, t, sub) for k, t, _sp, sub in DOUBLE_ARMS])
    cards: list[str] = []
    for key, title, sub in rows:
        p = Path(arms.get(key, ""))
        m = metrics.get(key, {})
        if not p.exists():
            cards.append(f'<div class="card"><h2>{title}</h2><p class="meta">{sub}</p>'
                         f'<span class="bad">（缺文件）</span></div>')
            continue
        audio = _audio_tag(p, key)
        ir = m.get("ir") or {}
        if is_rev:
            head = (f'IR 实测：pre-delay {_fmt(ir.get("pre_delay_ms"), "ms")} · '
                    f'RT60 {_fmt(ir.get("rt60_s"), "s", 2)} · 前80ms 能量 {_fmt(ir.get("early80_db"), "dB")} · '
                    f'混响发送 {_fmt(m.get("wet"), "dB")}')
            body = (f'全曲 LRA {_fmt(m.get("lra"), "dB")} · 动态std {_fmt(m.get("dyn_std"), "dB")} · '
                    f'与源动态相关 {_fmt(m.get("corr_src"), "", 3)} · 8–12k {_fmt(m.get("band_8k"), "dB")} · '
                    f'SFM {_fmt(m.get("sfm"), "", 4)}<br>'
                    f'副歌 声场 {_fmt(m["chorus"].get("side_mid_db"), "dB")} · '
                    f'单声道损失 {_fmt(m["chorus"].get("mono_loss_db"), "dB", 2)} · '
                    f'梳状残差 {_fmt(m["chorus"].get("ripple_db"), "dB", 2)} · '
                    f'副歌电平相对 R0 {_fmt(m["chorus"].get("wet_gain_vs_R0_db"), "dB", 2)}')
        else:
            d = m.get("double") or {}
            head = (f'加倍：{d.get("mode") or "无"} · 延迟 {d.get("delay_ms")} ms · '
                    f'移调 {d.get("cents")} 音分 · 电平 {d.get("gain_db")} dB · '
                    f'范围 {"副歌+重点句" if d.get("where") == "chorus" else "全曲"}')

            def _ev(tag: str, e: dict | None) -> str:
                if not e:
                    return f'{tag} 未测'
                if not e.get("ok"):
                    return f'{tag} {e.get("reason") or "无"}'
                return (f'{tag} 加倍≈干声延迟 {e.get("peak_delay_ms")}ms '
                        f'(包络相关 {e.get("peak_corr")}) · 差值 {e.get("diff_rms_db")}dB')

            body = (f'{_ev("副歌", d.get("evidence_chorus"))}<br>'
                    f'{_ev("主歌", d.get("evidence_verse"))}<br>'
                    f'副歌 相对对照 {_fmt(m["chorus"].get("lift_vs_F0_db"), "dB", 2)} · '
                    f'声场 {_fmt(m["chorus"].get("side_mid_db"), "dB")} · '
                    f'单声道损失 {_fmt(m["chorus"].get("mono_loss_db"), "dB", 2)} · '
                    f'梳状残差 {_fmt(m["chorus"].get("ripple_db"), "dB", 2)}<br>'
                    f'主歌 相对对照 {_fmt(m["verse"].get("lift_vs_F0_db"), "dB", 2)} · '
                    f'声场 {_fmt(m["verse"].get("side_mid_db"), "dB")} · '
                    f'全曲 LRA {_fmt(m.get("lra"), "dB")} · 削波 {_fmt(m.get("clip"), "%", 3)}')
        cards.append(f'<div class="card"><h2>{title}</h2><p class="meta">{sub}<br>{head}<br>{body}</p>{audio}</div>')

    title = "《好久不见》· 空间档位对照" if is_rev else "《好久不见》· 加倍方式对照（全曲 Haas vs 副歌 ADT）"
    intro = (
        "同一条干声（supp2026 +6dB@7k + 气口）、同一组混音增益（0.9545/0.7071），"
        "**只换混响 IR**。听的时候问：哪一版是「靠前，但身后有房间」？"
        if is_rev else
        "同一条链（E 臂 = 空间 2.0s + 段落推拉 ±2dB + 密度），**只加一份加倍**。"
        "②是旧的全曲 Haas，③换延迟但仍整曲铺满，④只在你熟悉的副歌窗里加倍，⑤把延迟/音分拉到区间上限。"
        "听的时候问：副歌有没有「真的多一个人在唱」，而不是「整首歌加了合唱效果」。"
    )
    warn = ("⚠️ 空间更长/更湿会降低「像你」与清晰度（CAM++ 在本项目已被加工骗过三次），"
            "所以本页不排序、只报漂移；这一页的裁决标准是**空间是否自然**，不是「像不像你」。"
            if is_rev else
            "⚠️ 加倍只在副歌生效的地方，主歌应逐样本等于对照；若你听到主歌也变宽/变糊，"
            "说明门没关住（页面「主歌 相对对照」应≈0.00dB、声场≈「—」）。"
            "硬摇对拍会掉单声道约 3dB，故声像取 0.7 不取 1.0。")
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#0e1014;--card:#161a21;--line:#242a35;--fg:#e8ecf2;--dim:#98a2b3;--acc:#6aa9ff;--warn:#ffb454}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.65 system-ui,"Microsoft YaHei",sans-serif}
.wrap{max-width:940px;margin:0 auto;padding:34px 20px 72px}
h1{font-size:24px;margin:0 0 6px}p.sub{color:var(--dim);margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:14px 0}
.card h2{font-size:16px;margin:0 0 3px;font-weight:600}
.meta{color:var(--dim);font-size:13px;margin:0 0 10px}
audio{width:100%;height:40px}
.note{color:var(--dim);font-size:13px;margin:10px 0 0}
.bad{color:#ff7b72;font-size:13px}.warn{color:var(--warn)}
</style></head><body><div class="wrap">
<h1>__TITLE__</h1>
<p class="sub">__INTRO__</p>
<p class="sub warn">__WARN__</p>
__BODY__
<footer class="note">2026-10-18 · experiments/cover_space_double.py · 全曲 250.5s · 全部成品（人声+伴奏）</footer>
</div></body></html>"""
    dst = OUT / ("listen_空间.html" if is_rev else "listen_加倍.html")
    dst.write_text(html.replace("__TITLE__", title).replace("__INTRO__", intro)
                   .replace("__WARN__", warn).replace("__BODY__", "\n".join(cards)),
                   encoding="utf-8")
    print(f"[页] {dst}（{dst.stat().st_size / 1e6:.1f} MB）")
    return dst


def _double_rows():
    for key, title, spec, sub in DOUBLE_ARMS:
        yield key, title, spec, sub, {}


# ---------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["reverb", "double", "all", "check"], nargs="?", default="all")
    a = ap.parse_args()
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if a.step == "check":
        print(json.dumps(self_check(), ensure_ascii=False, indent=1))
        return 0

    for group in (["reverb", "double"] if a.step == "all" else [a.step]):
        arms = run_group(group)
        metrics = json.loads((OUT / f"metrics_{group}.json").read_text(encoding="utf-8"))
        build_page(group, arms, metrics)

    print("[自检] " + json.dumps(self_check(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
