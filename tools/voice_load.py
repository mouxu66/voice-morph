# -*- coding: utf-8 -*-
"""发声负荷测量 —— 把「用嗓减负」从口号变成可测指标（大创减负实测层，2026-10-09）。

一句话
    同一节课，本人全程口播（A） vs 讲稿转旁白流程（B：平台用本人音色合成，
    本人只做少量开口补录），量「发声总时长 / 连续发声时长 / 发声占空比」
    下降多少 → 「减负 X%」是一个链路内就能测出的客观、可复现数字。
    协议：`docs/减负实测协议.md`（设计、口径、红线都在那边，本文件只管测量）。

用法
    # 单文件测量（也可多条一起）
    python tools/voice_load.py measure lesson_self.wav [--json out.json]

    # A/B 对比出节省率：A = 本人全程口播；B = 本人在 B 流程里所有开口录音（可多条）
    python tools/voice_load.py compare --a lesson_self.wav \
        --b retake_01.wav retake_02.wav [--synth narration.wav] [--json out.json]

口径与诚实边界（★ 报结论时要连同口径一起说，别只报一个百分数）
    1. 这里的「发声」是**能量法**：用 ffmpeg `silencedetect` 的自适应门限找静音段
       （直接复用 `voicebank_eval.detect_silences`，与切档用的是**同一套实现** ——
       项目铁律：不写第二份会漂移的能量包络），发声段 = 全程 − 静音段。
       所以它量的是**发声活动时长（speech activity time）**，不是严格生理意义的
       phonation time：呼吸声、桌面噪声可能被计入，< min_silence 的换气间隙也算发声。
    2. 职业嗓音文献里的对应概念是「发声总时长 / 时间剂量（time dose）」与
       「占空比（duty cycle / percent phonation time）」（Titze 的 vocal dose 框架）
       📚 文献转述 —— 能量法是可接受的近似，但**别把两者混为一谈**。
    3. 本工具测的是**发声负荷**，不是嗓音**疲劳**，更不是任何疗效。
    4. 「找不到 ffmpeg」绝不静默退化成"全程都在发声"（duty=1.0 是**错的数**，
       不是一个**没有的数**）—— 检测没跑起来就报错，与 voicebank_eval.split
       的降级口径不同（那边退化成硬切还有 aligned=false 可追溯，这边没有兜底口径）。
    5. 参数（门限 / min_silence）全部落进 JSON —— 换参数就是换口径，必须可复现。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np  # noqa: E402  依赖同 voicebank_eval（本机已装）

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "tools", ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from voicebank_eval import detect_silences  # noqa: E402  同一套自适应静音检测

#: 发声统计用的最小静音时长（秒）：短于它的间隙视为换气，仍算「连续发声」。
#: 与切档默认 0.15s **不同**（那边要找句末切点，这里要量"人开口的时间"），
#: 换成 0.3s 更接近职业嗓音文献对 "voicing episode" 的口径 📚。可 CLI 覆盖，落 JSON。
PHONATION_MIN_SILENCE_S = 0.3


def _read_duration_peak(path: Path) -> tuple[float, float]:
    """返回 (时长秒, 峰值 dBFS)。读不出就报错 —— 录音协议只认 WAV，不猜格式。"""
    try:
        import soundfile as sf

        d, sr = sf.read(str(path), dtype="float32")
    except Exception as exc:  # noqa: BLE001  统一成一句能看懂的话
        raise ValueError(
            f"读不了 {path}（{exc}）。录音协议要求 WAV（44.1k/48k 单声道），"
            f"别的格式请先转码 —— 别拿读不出的文件算减负。"
        ) from exc
    if d.ndim > 1:
        d = d.mean(axis=1)
    if d.size == 0:
        raise ValueError(f"{path} 是空文件，测不出任何东西")
    dur = d.size / sr
    peak = float(np.abs(d).max())
    peak_db = float(20.0 * np.log10(peak)) if peak > 0 else -120.0
    return dur, peak_db


def _complement_segments(silences: list[tuple[float, float]],
                         total_s: float) -> list[tuple[float, float]]:
    """静音段的补集 = 发声段。相邻静音间、以及首尾露出的部分都算在说话。"""
    segs: list[tuple[float, float]] = []
    cursor = 0.0
    for s, e in sorted(silences):
        s = max(0.0, min(s, total_s))
        e = max(0.0, min(e, total_s))
        if s > cursor:
            segs.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < total_s:
        segs.append((cursor, total_s))
    # 过滤零长段（浮点边界）
    return [(s, e) for s, e in segs if e - s > 1e-6]


def phonation_stats(path: Path, *, noise_db: float | None = None,
                    min_silence_s: float = PHONATION_MIN_SILENCE_S) -> dict:
    """单文件发声统计。口径见模块 docstring —— 报结论请连同口径一起报。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} 不存在")
    total_s, peak_db = _read_duration_peak(path)

    trials: list = []
    try:
        silences, used_db, trials = detect_silences(
            path, noise_db=noise_db, min_silence_s=min_silence_s, allow_relax=True)
    except Exception as exc:  # noqa: BLE001  检测没跑起来 ≠ 全程在发声
        raise RuntimeError(
            f"静音检测没跑起来（{exc}）。没有门限就没有口径，宁可报错也不给假数。"
        ) from exc
    if not trials:
        # detect_silences 找不到 ffmpeg 时返回 ([], 门限, []) —— 那是"没测"，
        # 按 duty=1.0 报出去就是"减负 0%"的假结论，比报错恶劣得多。
        raise RuntimeError(
            "静音检测没产生任何尝试记录（多半是找不到 ffmpeg）。"
            "装 ffmpeg（winget install Gyan.FFmpeg）或设 FFMPEG_PATH 后重跑。"
        )

    segs = _complement_segments(silences, total_s)
    phonation_s = sum(e - s for s, e in segs)
    longest = max((e - s for s, e in segs), default=0.0)
    return {
        "file": str(path),
        "total_s": round(total_s, 3),
        "peak_dbfs": round(peak_db, 1),
        "noise_db": round(float(used_db), 1),
        "min_silence_s": min_silence_s,
        "n_silences": len(silences),
        "n_segments": len(segs),
        "phonation_s": round(phonation_s, 3),
        "duty_cycle": round(phonation_s / total_s, 4) if total_s > 0 else None,
        "longest_continuous_s": round(longest, 3),
        "segments": [[round(s, 3), round(e, 3)] for s, e in segs],
    }


def compare_load(a: dict, b_stats: list[dict], synth: dict | None = None) -> dict:
    """A/B 节省率。a、b 均为 phonation_stats 的返回值。

    口径：B 侧只汇总「本人开口」的录音（补录/改稿重录），审听与播放不算
    （没有口腔发声，也没有录音可测）—— 这正是用**录音文件**计量而不是
    人工估时的原因。合成旁白（--synth）只是"替代了多长的本人口播"的
    覆盖率证据，它本身不耗嗓子、不参与节省率。
    """
    if not b_stats:
        raise ValueError("B 侧至少要给一条本人开口的录音（哪怕很短）；空集算不出对比")
    a_phonation = a["phonation_s"] or 0.0
    b_phonation = sum(s["phonation_s"] or 0.0 for s in b_stats)
    b_total = sum(s["total_s"] or 0.0 for s in b_stats)
    saving_s = a_phonation - b_phonation
    # A 侧发声为 0 时节省率没有定义 —— 返回 None（"没测出"），
    # 绝不返回 0.0（会被读成"测过了，没减负"），也不编 100%。
    saving_rate = (saving_s / a_phonation) if a_phonation > 0 else None
    out = {
        "a": a,
        "b": b_stats,
        "saving": {
            "a_phonation_s": round(a_phonation, 3),
            "b_phonation_s": round(b_phonation, 3),
            "saving_s": round(saving_s, 3),
            "saving_rate": round(saving_rate, 4) if saving_rate is not None else None,
            "a_longest_continuous_s": a["longest_continuous_s"],
            "b_longest_continuous_s": max(
                (s["longest_continuous_s"] for s in b_stats), default=0.0),
            "b_total_s": round(b_total, 3),
        },
    }
    if synth is not None:
        out["synth"] = synth
    return out


def synth_duration(path: Path) -> dict:
    """合成旁白成品只记时长（它是机器干的活，不占发声负荷），供覆盖率参考。"""
    dur, _peak = _read_duration_peak(Path(path))
    return {"file": str(path), "total_s": round(dur, 3)}


# ---------------- 输出 ----------------

def _fmt_dur(s: float) -> str:
    m, sec = divmod(int(round(s)), 60)
    return f"{m}分{sec:02d}秒" if m else f"{sec}秒"


def _pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "—"


def print_stats(tag: str, st: dict) -> None:
    print(f"  {tag}  {Path(st['file']).name}")
    print(f"    全程 {_fmt_dur(st['total_s'])} | 发声 {_fmt_dur(st['phonation_s'])}"
          f" | 占空比 {_pct(st['duty_cycle'])} | 最长连续 {_fmt_dur(st['longest_continuous_s'])}"
          f" | 发声 {st['n_segments']} 段 | 门限 {st['noise_db']}dB"
          f" | 峰值 {st['peak_dbfs']}dBFS")


def print_compare(res: dict) -> None:
    print_stats("A 本人全程口播", res["a"])
    for i, st in enumerate(res["b"], 1):
        print_stats(f"B 本人开口录音 {i}", st)
    if res.get("synth"):
        print(f"  合成旁白（机器产出，不计发声）  {Path(res['synth']['file']).name}"
              f"  时长 {_fmt_dur(res['synth']['total_s'])}")
    sv = res["saving"]
    print("减负对比（口径见 docs/减负实测协议.md）：")
    print(f"  发声总时长  {_fmt_dur(sv['a_phonation_s'])} → {_fmt_dur(sv['b_phonation_s'])}"
          f"  节省 {_fmt_dur(max(sv['saving_s'], 0.0))}"
          f"（{_pct(sv['saving_rate'])}）")
    print(f"  最长连续发声 {_fmt_dur(sv['a_longest_continuous_s'])} → "
          f"{_fmt_dur(sv['b_longest_continuous_s'])}")


def _write_json(payload: dict, json_path: str | None) -> None:
    if json_path:
        out = Path(json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"  [JSON 已落盘] {out}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发声负荷测量（减负实测层）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_m = sub.add_parser("measure", help="单文件发声统计")
    p_m.add_argument("wavs", nargs="+")
    _add_common(p_m)

    p_c = sub.add_parser("compare", help="A/B 对比出节省率")
    p_c.add_argument("--a", required=True, help="A 侧：本人全程口播的课")
    p_c.add_argument("--b", required=True, nargs="+",
                     help="B 侧：本人在旁白流程里的开口录音（补录/重录，可多条）")
    p_c.add_argument("--synth", default=None, help="合成旁白成品（可选，只记时长）")
    _add_common(p_c)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "measure":
            stats = [phonation_stats(Path(w), noise_db=args.noise_db,
                                     min_silence_s=args.min_silence) for w in args.wavs]
            for st in stats:
                print_stats("测量", st)
            _write_json({"tool": "voice_load", "params": _params(args),
                         "files": stats}, args.json)
        else:
            a = phonation_stats(Path(args.a), noise_db=args.noise_db,
                                min_silence_s=args.min_silence)
            b = [phonation_stats(Path(w), noise_db=args.noise_db,
                                 min_silence_s=args.min_silence) for w in args.b]
            syn = synth_duration(Path(args.synth)) if args.synth else None
            res = compare_load(a, b, syn)
            print_compare(res)
            _write_json({"tool": "voice_load", "params": _params(args),
                         **res}, args.json)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"[voice_load] {exc}", file=sys.stderr)
        return 2
    return 0


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--noise-db", type=float, default=None,
                   help="固定门限 dBFS（默认自适应；显式给则不放宽，保证可复现）")
    p.add_argument("--min-silence", type=float, default=PHONATION_MIN_SILENCE_S,
                   help=f"最小静音时长秒（默认 {PHONATION_MIN_SILENCE_S}）")
    p.add_argument("--json", default=None, help="把完整结果（含参数）落成 JSON")


def _params(args: argparse.Namespace) -> dict:
    return {"noise_db": args.noise_db, "min_silence_s": args.min_silence}


if __name__ == "__main__":
    raise SystemExit(main())
