# -*- coding: utf-8 -*-
"""帧级核验：成品相对原唱的**逐帧**变调量（比"中位数之商"稳得多）。

为什么换判据（2026-10-14 自查）
    第一版用"成品中位 F0 ÷ 原唱中位 F0"判 2^(p/12) 是否落地 —— 对 pitch 0 给出
    +0.20 半音（对），对 pitch −12 却给出 **−9.70**（差 2.3 半音）。
    但同一份产物的 **p90 是 210.0 Hz，而 415.1/2 = 207.6** —— p90 精确匹配 −12。

    ⇒ 两个统计量打架时**先解剖，别挑一个信**（同族 #177）。这里的差别在于：
    "中位数之商"会把**两边的浊音/清音分布差异**混进来（RVC 输出在低音区
    可能有更多帧判为清音或被 pyin 判到别处），而逐帧比对（只在**两边都有声**
    的帧上算差）才是"同一个音高位置移了多少"的直接度量。

可测性：判据本身是纯函数 `framewise_row`（只要 numpy），librosa 只在 `f0_curve`
里惰性导入 —— 守卫测试不必背 pyin/librosa 的重量。
"""
import json
import pathlib
import sys

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):
    pass

BASE = ROOT / "media/covers/song2"

#: 共同有声帧少于这个数就不下结论（清浊分布差异会把中位数之商带歪，但帧太少连逐帧也不可信）
MIN_BOTH_VOICED = 100


def f0_curve(path: pathlib.Path, sr: int = 16000):
    """读音频 → pyin 基频曲线，无声帧记 0.0（惰性导入 librosa，纯逻辑测试不背它）。"""
    import librosa  # 惰性：守卫只测 framewise_row，不需要 pyin

    y, _ = librosa.load(str(path), sr=sr, mono=True)
    f0, _v, _p = librosa.pyin(y, fmin=65, fmax=1400, sr=sr, hop_length=160)
    return np.nan_to_num(np.asarray(f0, dtype=np.float64), nan=0.0)


def framewise_row(src_f0, other_f0, pitch_tag: str) -> dict | None:
    """两条 f0 曲线逐帧比对，返回统计行；共同有声帧 < `MIN_BOTH_VOICED` 返回 None。

    只在**两边都有声**（f0>0）的帧上算 `12·log2(b/a)` —— 这是"同一音高位置被移了
    多少"的直接度量，剔除了"中位数之商"混进来的清浊分布差异（见模块 docstring）。
    """
    n = min(len(src_f0), len(other_f0))
    a = np.asarray(src_f0, dtype=np.float64)[:n]
    b = np.asarray(other_f0, dtype=np.float64)[:n]
    both = (a > 0) & (b > 0)
    if int(both.sum()) < MIN_BOTH_VOICED:
        return None
    shift = 12.0 * np.log2(b[both] / a[both])
    p10 = float(np.percentile(shift, 10))
    p90 = float(np.percentile(shift, 90))
    # p90 之商：只看"高音那一端"移了多少（不受清浊分布差异影响）
    q = 12.0 * float(np.log2(np.percentile(b[both], 90) / np.percentile(a[both], 90)))
    return {"pitch_tag": pitch_tag, "both_voiced": int(both.sum()),
            "frame_shift_median": round(float(np.median(shift)), 2),
            "p10": round(p10, 2), "p90": round(p90, 2),
            "spread": round(p90 - p10, 2),
            "p90_ratio_semitones": round(q, 2)}


def main() -> int:
    src = f0_curve(BASE / "01_vocals.wav")
    rows = []
    print(f"{'档位':<10} {'两边都有声帧':>12} {'逐帧中位':>10} {'p10':>8} {'p90':>8} "
          f"{'跨度':>7} {'p90之商':>9}")
    for tag, rel in (("-12 人声", "mypitch/vocal_p-12.wav"),
                     ("+0 人声", "mypitch/vocal_p+00.wav")):
        p = BASE / rel
        if not p.exists():
            print(f"{tag:<10} （缺）")
            continue
        r = framewise_row(src, f0_curve(p), tag)
        if r is None:
            print(f"{tag:<10} 共同有声帧太少（<{MIN_BOTH_VOICED}）")
            continue
        rows.append(r)
        print(f"{tag:<10} {r['both_voiced']:>12} {r['frame_shift_median']:>10.2f} "
              f"{r['p10']:>8.2f} {r['p90']:>8.2f} {r['spread']:>7.2f} "
              f"{r['p90_ratio_semitones']:>9.2f}")

    print()
    expect = {"-12 人声": -12.0, "+0 人声": 0.0}
    ok_all = True
    for r in rows:
        e = expect.get(r["pitch_tag"])
        if e is None:
            continue
        d = abs(r["frame_shift_median"] - e)
        ok = d < 1.5
        ok_all = ok_all and ok
        print(f"{r['pitch_tag']:<10} 逐帧中位 {r['frame_shift_median']:+.2f} 半音"
              f"（期望 {e:+.0f}）{'✓' if ok else '✗'}")

    out = BASE / "mypitch/f0_framewise.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
