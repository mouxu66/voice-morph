# -*- coding: utf-8 -*-
"""★ 尺子检验：CAM++ 的相似度会不会**光因为音高变了**就下降？

为什么必须做（2026-10-14，研究员警告 + 本项目既有结论）
    `cover_pitch_scan.py` 的结果是「变调越高，CAM++ 对你越低」（0.624 → 0.229）。
    但这个数有两种解释，而它们在报告里长得一模一样：
      ① **音色真的丢了**（模型在高音上撑不住你的音色）—— 那是个真结论；
      ② **尺子本身对音高有偏置**（同一个人的声音，只是音高不同，CAM++ 就判低了）
         —— 那我的结论就是测量伪影。
    本项目已有同族教训：技能库记着「**LTAS 奖励音高、不奖励音色**，纯机械降 12 半音
    就能把 LTAS 推高 0.078」，与 shift 0→−12 的全部增益同量级。所以「变调改变了 X 指标」
    这类结论一律要先证明尺子对音高不敏感。

做法（已知同一说话人，只改音高）
    拿**用户自己的参考音**（`ref_24k.wav`，说话、中位 ~105Hz），用**磁带式变调**
    （resample，F0 与共振峰一起按比例缩放）造出 −12 … +12 各档，再各自与**原始未变调
    的参考音**打 CAM++。

    ★ 为什么用磁带式而不是保共振峰式：RVC 离线路径**没有 formant 参数**（研究员已
    源码级确认），它的变调同样把共振峰一起带走。所以"磁带式"才是与 RVC 同构的对照。

判读
    · 若各档 CAM++ 基本不变（波动 ≤ 0.02）⇒ 尺子对音高不敏感 ⇒ 音域扫描的单调下降
      **是音色真的丢了**；
    · 若随音高差单调下降 ⇒ 尺子有音高偏置 ⇒ 音域扫描的数字要按这个偏置打折解读，
      并在报告里写明"含多少测量伪影"。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))
sys.path.insert(0, str(ROOT / "experiments"))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402


def tape_shift(y: np.ndarray, sr: int, semitones: float) -> tuple[np.ndarray, int]:
    """磁带式变调：**只做一次**重采样 ⇒ F0 与共振峰**一起**按比例缩放。

    ★ 实现要点（2026-10-14 踩过）：**不能"压时间轴再拉回来"** ——
    那两次 `resample_poly` 互为逆运算，音高会原样变回去（实测：±12 档读出来
    仍是 109.2 Hz，与未变调一模一样，而整条检验看上去跑得很正常）。
    正确做法是**只做一次**：把 N 个样本按 `N/ratio` 输出，等于"用 ratio 倍的
    速度播放"⇒ 音高 ×ratio、时长 ÷ratio。这正是"磁带加速"的语义。

    与 RVC 的同构性：离线 RVC **没有 formant 参数**（研究员源码级确认），
    它的变调同样把共振峰一起带走 ⇒ 磁带式才是同构对照。
    """
    if semitones == 0:
        return y, sr
    from scipy.signal import resample_poly

    ratio = 2 ** (semitones / 12.0)
    new_len = max(int(round(len(y) / ratio)), 1)
    return resample_poly(y, new_len, len(y)).astype(np.float32), sr


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="CAM++ 的音高偏置检验（已知同一说话人）")
    ap.add_argument("--ref", required=True, type=Path, help="用户参考音（说话）")
    ap.add_argument("--out", type=Path, default=ROOT / "media/covers/pitchruler")
    ap.add_argument("--semitones", default="-12,-8,-5,0,4,8,12")
    args = ap.parse_args()

    ref = args.ref if args.ref.is_absolute() else ROOT / args.ref
    out = args.out if args.out.is_absolute() else ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)

    import cover_metrics

    y, sr = sf.read(str(ref), dtype="float32", always_2d=True)
    y = y.mean(1)
    import librosa

    # 统一到 16k（CAM++ 的口径），并裁到 6s 以内（够算嵌入，跑得快）
    y16 = librosa.resample(y, orig_sr=sr, target_sr=16000)[: 16000 * 6]

    # ★ 基准文件**先**写（未变调那一份），否则第一档没有对照可打
    sf.write(str(out / "_ref_orig.wav"), y16, 16000)

    rows = []
    for st in [int(x) for x in args.semitones.split(",") if x.strip()]:
        shifted, _ = tape_shift(y16, 16000, st)
        p = out / f"ref{st:+03d}.wav"
        sf.write(str(p), shifted, 16000)
        # ★ 先证明**这个对照真的改了音高** —— 否则整条检验是空转。
        #   fmin/fmax 给宽（±12 档会到 ~430Hz，且高次谐波更远）。
        f0 = float(np.median(librosa.yin(shifted, fmin=60, fmax=1000, sr=16000)))
        rows.append({"semitones": st, "wav": str(p), "median_f0_hz": round(f0, 1),
                     "secs_vs_untouched": None})

    print(f"{'档位':>6} {'中位F0':>10} {'CAM++ vs 未变调参考':>20}")
    for r in rows:
        r["secs_vs_untouched"] = cover_metrics.secs(
            Path(r["wav"]), out / "_ref_orig.wav")
        print(f"{r['semitones']:>+6} {r['median_f0_hz']:>10} "
              f"{str(r['secs_vs_untouched']):>20}")

    # ★ 先自查"变调真的生效了"：各档中位 F0 必须随档位单调、且大致按 2^(st/12) 走。
    #   没生效时（我第一次的 bug）所有档会读出同一个 F0，而相似度却"有差异" ——
    #   那种表会把人带向完全错误的结论。
    by_st = {r["semitones"]: r["median_f0_hz"] for r in rows}
    base = by_st.get(0) or (min(by_st.values()) if by_st else 0)
    f0_ok = True
    for st, f0 in by_st.items():
        if base and abs(f0 / base - 2 ** (st / 12.0)) > 0.15 * 2 ** (abs(st) / 12.0):
            f0_ok = False
    if not f0_ok:
        print("\n❌ **对照没生效**：各档中位 F0 与 2^(st/12) 对不上 —— "
              "这条检验无效，别用它解读任何结论。")
        return 2

    vals = [r["secs_vs_untouched"] for r in rows if r["secs_vs_untouched"] is not None]
    spread = round(max(vals) - min(vals), 4) if vals else None
    verdict = ("尺子对音高基本不敏感（波动 %.4f ≤ 0.02）⇒ 音域扫描的单调下降是**音色真的丢了**"
               % spread if spread is not None and spread <= 0.02 else
               ("尺子**有音高偏置**（波动 %.4f）⇒ 音域扫描的数字含测量伪影，必须按此打折解读"
                % spread if spread is not None else "量不出"))
    report = {"ref": str(ref), "rows": rows, "spread": spread,
              "f0_scaling_ok": f0_ok, "verdict": verdict}
    (out / "pitch_ruler.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"★ {verdict}")
    print(f"→ {out / 'pitch_ruler.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
