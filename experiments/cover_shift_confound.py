# -*- coding: utf-8 -*-
"""查「谁更像」的模式是不是**位移量的赝象**（2026-10-14 自查）。

怀疑
    `cover_pitch_discrim.py` 读到「−12 档输出更像你，其余 6 档都更像原唱」。
    但这个模式与**两边各自被移调多少**高度同步：
        −12 档：你的参考音只移 +3.2 半音（几乎无损），原唱移 −12（重损）
        +12 档：你的参考音移 +27 半音（极重损），原唱移 +12（中损）
    而 `cover_pitch_ruler.py` 已实测**重移调会严重损伤 CAM++ 相似度**
    （同一个人移 ±12 半音 → 0.04~0.25）。

    若"赢家"确实由位移量决定，那这个判别试验**无效**。

做法（把位移量单独作为变量）
    拿**同一个人**的两条真语音（参考音 X 与另一段 X2），各自做 0…+27 半音的移调，
    量「X_at(p) vs X2_at(p)」。若相似度随 |位移| 单调下降 ⇒ 位移量本身就是混淆因子，
    「谁更像」必须由位移量解释，不能当音色证据。
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))
sys.path.insert(0, str(ROOT / "experiments"))

import importlib.util  # noqa: E402

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "cover_pitch_ruler", ROOT / "experiments" / "cover_pitch_ruler.py")
cpr = importlib.util.module_from_spec(_spec)
sys.modules["cover_pitch_ruler"] = cpr
_spec.loader.exec_module(cpr)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    import librosa

    import cover_metrics

    out = ROOT / "media/covers/shift_confound"
    out.mkdir(parents=True, exist_ok=True)

    # 同一个人（用户）的两条素材：参考音 + 训练切片里挑一条（真正不同内容、同一人）
    y, sr = sf.read(str(ROOT / "media/ft_work/ppt12/ref_24k.wav"),
                    dtype="float32", always_2d=True)
    x1 = librosa.resample(y.mean(1), orig_sr=sr, target_sr=16000)[: 16000 * 6]
    clips = sorted((ROOT / "media/ft_work/ppt12" / "clips24k").glob("*.wav"))
    if not clips:
        print("[!] 找不到第二条同人素材（clips24k）")
        return 2
    y2, sr2 = sf.read(str(clips[0]), dtype="float32", always_2d=True)
    x2 = librosa.resample(y2.mean(1), orig_sr=sr2, target_sr=16000)[: 16000 * 6]

    print(f"素材：ref_24k.wav（{len(x1)/16000:.1f}s）与 {clips[0].name}（{len(x2)/16000:.1f}s）"
          f" —— **同一个人**，不同内容")
    print()
    print(f"{'移调(半音)':>10} {'CAM++ (同人两条)':>18}   ← 位移量本身的影响")
    rows = []
    for st in [0, 3, 6, 12, 18, 27]:
        a = cpr.tape_shift(x1, 16000, st)[0] if st else x1
        b = cpr.tape_shift(x2, 16000, st)[0] if st else x2
        pa, pb = out / f"a{st}.wav", out / f"b{st}.wav"
        sf.write(str(pa), a, 16000)
        sf.write(str(pb), b, 16000)
        sim = cover_metrics.secs(pa, pb)
        rows.append({"shift_semitones": st, "secs_same_speaker": sim})
        print(f"{st:>10} {str(sim):>18}")

    vals = [r["secs_same_speaker"] for r in rows if r["secs_same_speaker"] is not None]
    monotone_down = all(vals[i] >= vals[i + 1] - 0.02 for i in range(len(vals) - 1))
    rep = {"rows": rows, "monotone_decreasing": monotone_down,
           "note": "同一个人、不同内容、同样移调 ⇒ 相似度随位移量下降则位移量是混淆因子"}
    (out / "shift_confound.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"★ 单调下降：{monotone_down}")
    print("  若是 ⇒ `cover_pitch_discrim` 的『谁更像』模式可由**位移量**解释，"
          "不能当音色证据。")
    print(f"→ {out / 'shift_confound.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
