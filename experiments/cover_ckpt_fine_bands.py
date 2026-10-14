# -*- coding: utf-8 -*-
"""细频段解剖：质心差 94 Hz 到底差在哪（e30 vs e40）。

起因（2026-10-14 自查发现的**两个判据打架**）：
    e30 的 4-8k 比 e40 **高 0.7 dB**（更亮），但谱质心却**低 94 Hz**（看着更闷）。
    两个都是"亮度"指标却给出相反方向 —— 在查清之前不该写任何推荐。

质心 = Σf·P(f)/ΣP(f)，而 0-4k 装了 ~99% 的能量 ⇒ 质心的差异必然来自
**0-4k 内部**的形状，而不是 4-8k 那点尾巴。所以这里把 0-4k 切成细段来看。
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

FINE = [(0, 200), (200, 400), (400, 800), (800, 1200), (1200, 2000),
        (2000, 3000), (3000, 4000), (4000, 8000)]

PAIRS = [
    ("media/covers/ckpt/epochs_12min__myvoice_ppt12_full_e30_s2970.wav", "e30"),
    ("media/covers/ckpt/epochs_12min__myvoice_ppt12_full_e40_s3960.wav", "e40"),
    ("media/covers/ckpt_s4321/epochs_12min__myvoice_ppt12_full_e30_s2970.wav", "e30(s4321)"),
    ("media/covers/ckpt_s4321/epochs_12min__myvoice_ppt12_full_e40_s3960.wav", "e40(s4321)"),
]


def spectrum(path: pathlib.Path):
    d, sr = sf.read(str(path), dtype="float64", always_2d=True)
    mono = d.mean(1)
    n, hop = 1024, 512
    win = np.hanning(n)
    frames = [mono[i:i + n] for i in range(0, len(mono) - n, hop)]
    X = np.stack([np.abs(np.fft.rfft(f * win)) ** 2 for f in frames])
    rms = np.sqrt(X.sum(1) / n)
    keep = rms >= rms.max() * (10 ** (-35.0 / 20))
    P = X[keep].mean(0)
    fr = np.fft.rfftfreq(n, 1 / sr)
    return P, fr


def main() -> int:
    print(f"{'band':>12s}" + "".join(f"{t:>12s}" for _, t in PAIRS)
          + f"{'e30-e40':>10s}")
    specs = [(spectrum(ROOT / p), t) for p, t in PAIRS]
    rows = []
    for lo, hi in FINE:
        vals = []
        for (P, fr), _ in specs:
            tot = float(P.sum()) + 1e-15
            frac = float(P[(fr >= lo) & (fr < hi)].sum() / tot)
            vals.append(10 * float(np.log10(frac + 1e-15)))
        diff = vals[0] - vals[1]
        print(f"{f'{lo}-{hi}':>12s}" + "".join(f"{v:>12.1f}" for v in vals)
              + f"{diff:>+10.2f}")
        rows.append({"band": f"{lo}-{hi}", "vals": [round(v, 2) for v in vals],
                     "e30_minus_e40_s1234": round(diff, 2)})
    print("\n（单位 dB，相对整段总能量；最后一列 = seed 1234 下 e30 减 e40）")
    print("★ 若某段两 seed 的差值**同号**，那才是真差异；异号就是噪声。")
    out = ROOT / "media/covers/ckpt/fine_bands.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
