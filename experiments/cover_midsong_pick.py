"""选曲：把「这首歌我的音色能不能唱」变成可复算的四个数（2026-10-18）。

## 为什么要做成模块

10-18 这轮先按印象挑了 6 首候选，实测后发现**印象全错**：
《消愁》被当成"低音歌"中位却 195.9Hz、《夜空中最亮的星》中位 345Hz，
而真正决定能不能唱的不是中位，是**在崩坏区停留多久**。
这些口径如果只活在一次性的 `.tmp_*.py` 里，下次换歌又会重写一遍、又会漏掉某一项。

## 四个数（缺一不可）

| 指标 | 回答什么 | 为什么不能用别的 |
|---|---|---|
| `median_hz` | 这首歌"坐在"多高 | 不能用整轨 min/max（间奏残留会把音域读低一个八度，犯错指南 #183） |
| `span_semi` | 跨度多大 | 用 0.5s 窗中位的 p05–p95，不是极值 |
| `ge_collapse_pct` | **在崩坏区停留多久** | 只看中位会把「整段坐在 345Hz」和「一句长音顶到 345Hz」当成同一件事 |
| `run_collapse_s` | 崩坏区最长连续多长 | 同上，且决定"能不能靠跳过某一句规避" |

## ★ 关于"崩坏区阈值"（2026-10-18 更正）

旧文档写「模型硬天花板 ~272Hz」，据此把 ≥300Hz 当成不可唱。**这条已作废**：
它来自 `pitch=-12`（降八度）的产物，不是原调直唱。同一输入 `pitch=0` 实测
输出中位 351Hz、周期度 0.942（详见 `m1o` 文档与 `ceiling_test/`）。
所以本模块**不写死天花板**，阈值一律由调用方传 —— 它是一个**待测参数**，
不是常量。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: 崩坏区阈值（Hz）。默认 300 是历史口径，便于与旧结论横比；
#: ★ 它**不是**模型天花板（见模块 docstring），要不要用由调用方决定。
DEFAULT_COLLAPSE_HZ = 300.0

#: 音域统计窗长（秒）。0.5s 是本项目统一口径（`犯错指南` #183）。
WINDOW_S = 0.5


@dataclass(frozen=True)
class RangeSummary:
    """一首歌的音域摘要。字段名与 `candidates/range_survey.json` 保持一致。"""

    n_win: int
    p05_hz: float
    p50_hz: float
    p95_hz: float
    span_semi: float

    def notes(self) -> dict[str, str]:
        """把三个分位点换成音名（ASCII，避免 Windows 控制台 GBK 编码炸掉）。"""
        return {
            "lo": hz_to_note(self.p05_hz),
            "mid": hz_to_note(self.p50_hz),
            "hi": hz_to_note(self.p95_hz),
        }


def hz_to_note(hz: float) -> str:
    """频率 → 音名（A4=440，十二平均律）。用 `F#2` 而不是 `F♯2`。

    ★ 为什么强制 ASCII：Windows 控制台默认 GBK，librosa 的 `hz_to_note` 默认返回
    升号 `♯`（U+266F），直接 print 会 `UnicodeEncodeError` —— 本轮实测踩过一次，
    整个 6 首普查在最后一行汇总时崩掉，前面的下载与分离全白跑。
    """
    if not np.isfinite(hz) or hz <= 0:
        return "?"
    names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    midi = int(round(69 + 12 * np.log2(float(hz) / 440.0)))
    return f"{names[midi % 12]}{midi // 12 - 1}"


def semitones_between(hz_a: float, hz_b: float) -> float:
    """两个频率差几个半音（`hz_b` 相对 `hz_a`）。用于「这首比那首高多少」。"""
    if hz_a <= 0 or hz_b <= 0:
        return float("nan")
    return float(12.0 * np.log2(float(hz_b) / float(hz_a)))


def gate_threshold(rms: np.ndarray, *, percentile: float = 20.0, mult: float = 2.0) -> float:
    """静音门 = 全曲 RMS 的第 `percentile` 百分位 × `mult`。

    这是 `犯错指南` #183 的修正：只看 min/max 会把间奏里 demucs 残留的低频
    （60–72Hz 占 18.3%）读成"唱到了 C2"。
    """
    rms = np.asarray(rms, dtype=np.float64)
    if rms.size == 0:
        return 0.0
    return float(np.percentile(rms, percentile)) * float(mult)


def window_medians(
    f0: np.ndarray,
    rms: np.ndarray,
    *,
    hop_s: float,
    window_s: float = WINDOW_S,
    gate: float | None = None,
    min_voiced_frac: float = 0.2,
) -> np.ndarray:
    """按 `window_s` 滑窗取有声帧的 F0 中位，返回一串窗中位。

    `min_voiced_frac` 是"这个窗里至少要有多少比例的有声帧"才采信 —— 太少时
    中位数没有代表性（一个窗里 2 个有声帧也能算出数字，但那是噪声）。
    """
    f0 = np.asarray(f0, dtype=np.float64)
    rms = np.asarray(rms, dtype=np.float64)
    n = min(f0.size, rms.size)
    f0, rms = f0[:n], rms[:n]
    win = max(1, int(round(window_s / hop_s)))
    out: list[float] = []
    for i0 in range(0, n - win + 1, win):
        sl = f0[i0:i0 + win]
        fin = sl[np.isfinite(sl)]
        if fin.size < max(1, win * min_voiced_frac):
            continue
        if gate is not None and float(np.mean(rms[i0:i0 + win])) < gate:
            continue
        out.append(float(np.median(fin)))
    return np.asarray(out, dtype=np.float64)


def summarize(win_medians: np.ndarray, *, lo_pct: float = 5.0, hi_pct: float = 95.0) -> RangeSummary:
    """窗中位 → 音域摘要。空输入返回全 NaN（调用方按"量不到"处理，不要抛）。"""
    arr = np.asarray(win_medians, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return RangeSummary(0, float("nan"), float("nan"), float("nan"), float("nan"))
    lo = float(np.percentile(arr, lo_pct))
    mid = float(np.percentile(arr, 50.0))
    hi = float(np.percentile(arr, hi_pct))
    return RangeSummary(
        n_win=int(arr.size),
        p05_hz=round(lo, 1),
        p50_hz=round(mid, 1),
        p95_hz=round(hi, 1),
        span_semi=round(float(12.0 * np.log2(hi / lo)), 1) if lo > 0 else float("nan"),
    )


def longest_run(mask: np.ndarray, hop_s: float) -> tuple[float, float]:
    """布尔序列里最长连续 True 的 (秒数, 起点秒)。全 False 返回 (0.0, 0.0)。

    为什么要它：`ge_collapse_pct` 高有可能是"整段坐在高音区"，也有可能是
    "无数短促尖峰"。前者必须换歌/降调，后者常常可以靠跳过某一句规避 ——
    同一个百分比对应两个完全不同的决策，所以必须分开量。
    """
    m = np.asarray(mask, dtype=bool)
    if m.size == 0:
        return 0.0, 0.0
    best = 0.0
    best_at = 0.0
    i = 0
    n = m.size
    while i < n:
        if not m[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and m[j + 1]:
            j += 1
        dur = (j - i + 1) * hop_s
        if dur > best:
            best, best_at = dur, i * hop_s
        i = j + 1
    return float(best), float(best_at)


def collapse_stats(
    f0: np.ndarray,
    ok: np.ndarray,
    *,
    hop_s: float,
    collapse_hz: float = DEFAULT_COLLAPSE_HZ,
) -> dict[str, float | int]:
    """崩坏区占比 + 最长连续。`ok` = 有声且过静音门的帧掩码。"""
    f0 = np.asarray(f0, dtype=np.float64)
    ok = np.asarray(ok, dtype=bool)
    n = min(f0.size, ok.size)
    f0, ok = f0[:n], ok[:n]
    valid = ok & np.isfinite(f0)
    if not bool(valid.any()):
        return {"n": 0, "ge_pct": float("nan"), "run_s": 0.0, "run_at_s": 0.0}
    hit = valid & (f0 >= collapse_hz)
    run_s, run_at = longest_run(hit, hop_s)
    return {
        "n": int(valid.sum()),
        "ge_pct": round(100.0 * float(hit.sum()) / float(valid.sum()), 2),
        "run_s": round(run_s, 2),
        "run_at_s": round(run_at, 1),
    }


def high_run_stats(
    f0: np.ndarray, ok: np.ndarray, *, hop_s: float, hz: float
) -> tuple[float, float]:
    """`hz` 以上的最长连续与起点（`collapse_stats` 的窄用途版本）。"""
    f0 = np.asarray(f0, dtype=np.float64)
    ok = np.asarray(ok, dtype=bool)
    n = min(f0.size, ok.size)
    hit = ok[:n] & np.isfinite(f0[:n]) & (f0[:n] >= hz)
    return longest_run(hit, hop_s)
