# -*- coding: utf-8 -*-
"""验预加重：造出来的数据集**真的**按目标斜率抬了 2kHz 以上吗？

为什么必须先验（2026-10-14）：训练要花 GPU 时间，而"预加重写错了"
（斜率算反 / 抬错了频段 / 被峰值缩放抹平）在训练日志里**完全看不出来** ——
只会得到三个"都不管用"的模型，然后可能错误地总结成"预加重没用"。
同族纪律：#175「能读出来的机制不等于会运行的机制」、#178「先验尺子」。
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

BANDS = [(400, 800), (800, 1600), (1600, 2400), (2400, 3200), (3200, 4800),
         (4800, 6400), (6400, 8000)]

SOURCE = pathlib.Path("D:/RVC/dataset_raw/myvoice_ppt12_full_40k")


def band_db(path: pathlib.Path) -> dict:
    d, sr = sf.read(str(path), dtype="float64", always_2d=True)
    mono = d.mean(1)
    n, hop = 2048, 512
    win = np.hanning(n)
    frames = [mono[i:i + n] for i in range(0, len(mono) - n, hop)]
    X = np.stack([np.abs(np.fft.rfft(f * win)) ** 2 for f in frames])
    rms = np.sqrt(X.sum(1) / n)
    keep = rms >= np.percentile(rms, 60)        # 只统计活跃帧（避免静音拉低高频）
    P = X[keep].mean(0)
    fr = np.fft.rfftfreq(n, 1 / sr)
    ref = float(P[(fr >= 400) & (fr < 800)].sum()) + 1e-15
    return {f"{lo}-{hi}": round(10 * float(np.log10(
        (float(P[(fr >= lo) & (fr < hi)].sum()) + 1e-15) / ref)), 1)
        for lo, hi in BANDS}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    out = ROOT / "media/covers/tilt"
    # 每条取同一批文件（排序后前 5 条）求平均，避免采样偏差
    tags = ["原样", "t03", "t06"]
    dirs = {"原样": SOURCE}
    for t in ("t03", "t06"):
        p = out / f"ds_{t}"
        if p.exists():
            dirs[t] = p
    if len(dirs) < 3:
        print(f"[!] 数据集不全，现有 {list(dirs)}（先跑 cover_tilt_train.py --dry-run）")
        return 2

    rows = {}
    print(f"{'档':<8}" + "".join(f"{b:>11}" for b in [f"{a}-{c}" for a, c in BANDS]))
    for tag, d in dirs.items():
        files = sorted(d.glob("*.wav"))[:5]
        acc = {}
        for f in files:
            for k, v in band_db(f).items():
                acc.setdefault(k, []).append(v)
        rows[tag] = {k: round(float(np.mean(v)), 1) for k, v in acc.items()}
        print(f"{tag:<8}" + "".join(f"{rows[tag][b]:>11.1f}" for b in rows[tag]))

    print()
    print("=== 相对原样的变化（dB）===")
    print(f"{'档':<8}" + "".join(f"{b:>11}" for b in rows["原样"]))
    deltas = {}
    for tag in ("t03", "t06"):
        if tag not in rows:
            continue
        deltas[tag] = {k: round(rows[tag][k] - rows["原样"][k], 1) for k in rows["原样"]}
        print(f"{tag:<8}" + "".join(f"{deltas[tag][b]:>+11.1f}" for b in rows["原样"]))

    print()
    # 判据：4-8k 必须**真的被抬起来**，且抬得随斜率单调
    def hf(tag):
        ks = [k for k in rows[tag] if k.split("-")[0] in ("3200", "4800", "6400")]
        return float(np.mean([rows[tag][k] for k in ks]))

    base_hf = hf("原样")
    ok = True
    for tag in ("t03", "t06"):
        if tag not in rows:
            continue
        d = hf(tag) - base_hf
        print(f"{tag} 的 3.2–8k 平均抬升：{d:+.1f} dB")
        if d <= 0.5:
            print(f"  ✗ {tag} 没抬起来 —— 预加重没生效，别拿它去训练")
            ok = False
    if "t03" in rows and "t06" in rows:
        mono_up = (hf("t06") - base_hf) > (hf("t03") - base_hf)
        print(f"单调性（+6dB 档比 +3dB 档抬得更多）：{mono_up}")
        ok = ok and mono_up

    (out / "tilt_verify.json").write_text(json.dumps(
        {"bands": rows, "deltas": deltas, "ok": ok}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("\n" + ("✅ 预加重生效，可以训练" if ok else "❌ 预加重有问题，先修再训练"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
