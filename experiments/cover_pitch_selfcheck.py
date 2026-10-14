# -*- coding: utf-8 -*-
"""★ 对照自检：『同档参考』那一路的相似度低，是**因为它真的不像**，
还是因为我的变调对照把时长/内容弄坏了（假对照）？

为什么必须查（2026-10-14）
    `cover_pitch_matched.py` 读出 `vs 同档参考` 甚至为负（−0.08 / −0.19）。
    但"负"有两种来源：
      ① 高音段输出**确实**不像变调后的我（真结论）；
      ② **我的对照坏了** —— 磁带式变调会**缩短时长**（±12 档只剩 1/2 长度），
         而那可能是 CAM++ 读到的差异来源，与"音色像不像"无关。
    （同族：#178「别把假对照读成结论」、`m1i` 的"各自分位"混淆。）

做法：造一组**已知答案**的对照
    · **自比**：X vs X ⇒ 必须 ≈ 1.0（尺子上界）
    · **同变调自比**：shift(X, p) vs shift(X, p) ⇒ 必须 ≈ 1.0（尺子对"同样变调过"的两份仍认同一人）
    · **同内容不同时长裁剪**：X vs X[:half] ⇒ 若明显 < 1.0，说明时长是混淆因子
    · **未变调 vs 变调**：X vs shift(X, p) ⇒ 就是 `pitch_ruler` 量的那个（预期低）
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

    out = ROOT / "media/covers/pitchruler_selfcheck"
    out.mkdir(parents=True, exist_ok=True)
    y, sr = sf.read(str(ROOT / "media/ft_work/ppt12/ref_24k.wav"),
                    dtype="float32", always_2d=True)
    x = librosa.resample(y.mean(1), orig_sr=sr, target_sr=16000)[: 16000 * 6]

    def w(name, arr):
        p = out / name
        sf.write(str(p), arr, 16000)
        return p

    xp = w("x.wav", x)
    cases = [
        ("自比 X vs X", xp, xp),
        ("同变调自比 shift+12 vs 同一条", w("s12.wav", cpr.tape_shift(x, 16000, 12)[0]),
         out / "s12.wav"),
        ("X vs X 的前半段（时长混淆）", xp, w("xhalf.wav", x[: len(x) // 2])),
        ("X vs shift+12（未变调 vs 变调）", xp, out / "s12.wav"),
        ("X vs shift-12", xp, w("sm12.wav", cpr.tape_shift(x, 16000, -12)[0])),
    ]

    print(f"{'对照':<40} {'CAM++':>8}  期望")
    rows = []
    for label, a, b in cases:
        sim = cover_metrics.secs(a, b)
        rows.append({"case": label, "secs": sim})
        print(f"{label:<40} {str(sim):>8}")

    rep = {"rows": rows, "note": "已知答案的对照：判断『同档参考』那一路能不能用"}
    (out / "selfcheck.json").write_text(
        json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out / 'selfcheck.json'}")
    print("\n★ 若『自比』与『同变调自比』都是 1.0，而『前置段』也≈1.0 ⇒ "
          "尺子与对照都没坏，负分是真结论。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
