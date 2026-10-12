# -*- coding: utf-8 -*-
"""RVC vs Qwen3-TTS 对照评测（.venv：CAM++ 声纹 + faster-whisper 可懂度）。

背景
----
`ab_eval_multi.py` 只能评"同文本、同批次"的多个 adapter；RVC 输出是多条**不同来源**
（真人原音 / Qwen 零样本 TTS）经同一个 RVC 模型转换得到的，评估口径不同，因此单开一支。

本脚本做三件事：
1. **同源对照**（最硬的一条）：Qwen 零样本 TTS 输出 → RVC 转换前后对比。
   两次唯一的差别就是 RVC，所以 CAM++ 的 Δ 直接等于"RVC 对音色的净贡献"。
2. **跨源定位**：真人原音过 RVC（out_real）给出该 RVC 模型的"音色天花板"，
   用来判断 TTS→RVC 与上限差多少（差得多 = 问题在 TTS 源码；差得少 = 已到 RVC 极限）。
3. **客观质量**：peak / 削波率 / RMS / 时长，防止"像了但爆音/静音"这类假高分。

用法（必须在 .venv 下跑，见 AGENTS.md 环境矩阵）：
    .venv\\Scripts\\python.exe tts_trial\\ab_eval_rvc_vs_qwen.py
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, r"D:\变声\m2_server")
sys.path.insert(0, r"D:\变声\tts_trial")
sys.stdout.reconfigure(encoding="utf-8")

from ab_evaluate import cam_emb  # noqa: E402
from asr_cer import accuracy as asr_accuracy  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402

try:
    import soundfile as sf
except ImportError:  # pragma: no cover
    sf = None

DEFAULT_REF = r"D:\变声\media\ft_work\ppt12\ref_24k.wav"
DEFAULT_DIR = r"D:\变声\media\ft_work\ppt12\myvoice_rvc"

# 与 myvoice_rvc/in_tts0{1,2,3}_40k.wav 一一对应（生成时的 3 条文本）
TEXTS = {
    "tts01": "这一节我们讲三个概念，先看第一个。",
    "tts02": "把步骤拆开，一步一步做就不会乱。",
    "tts03": "注意这里的条件，符号写反了结论就完全不同。",
}


def emb(path):
    v = cam_emb(path)
    return None if v is None else np.asarray(v, dtype=np.float32).reshape(-1)


def cos(x, y):
    if x is None or y is None:
        return float("nan")
    d = float(np.linalg.norm(x)) * float(np.linalg.norm(y))
    return float(np.dot(x, y) / d) if d > 0 else float("nan")


def overlap(a, b):
    """可懂度（1 - CER）。见 asr_cer 模块头：字符集 Jaccard 会被标点污染。"""
    return asr_accuracy(b, a)


def audio_stats(path):
    """peak / 削波率 / RMS(dB) / 时长(s)。"""
    if sf is None:
        return dict(peak=float("nan"), clip=float("nan"), rms_db=float("nan"), dur=float("nan"))
    a, sr = sf.read(path, dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    # 削波：连续 |x| >= 0.999 的样本占比（真·数字满幅，不是"接近满幅"）
    clip = float(np.mean(np.abs(a) >= 0.999)) if a.size else 0.0
    rms = float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0
    rms_db = 20.0 * np.log10(rms) if rms > 0 else float("-inf")
    return dict(peak=peak, clip=clip, rms_db=rms_db, dur=len(a) / float(sr))


def asr_text(wm, path):
    segs, _ = wm.transcribe(path, language="zh", vad_filter=False)
    text = "".join(s.text for s in segs)
    # faster-whisper 常在中文里插空格，字符集 Jaccard 前先去掉，避免虚低
    return text.replace(" ", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=DEFAULT_REF)
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--out", default=None, help="结果 json 路径（默认写到 --dir/rvc_vs_qwen.json）")
    args = ap.parse_args()

    out_json = args.out or os.path.join(args.dir, "rvc_vs_qwen.json")

    # 对照组：同一条 TTS 源码，RVC 转换前(in_tts0X_40k) vs 转换后(out_tts0X)
    pairs = [
        ("qwen_zs_1", "in_tts01_40k.wav", "out_tts01.wav", TEXTS["tts01"]),
        ("qwen_zs_2", "in_tts02_40k.wav", "out_tts02.wav", TEXTS["tts02"]),
        ("qwen_zs_3", "in_tts03_40k.wav", "out_tts03.wav", TEXTS["tts03"]),
    ]

    ref = emb(args.ref)
    if ref is None:
        print(f"[FATAL] 参考音 CAM++ 提取失败：{args.ref}")
        return 1
    wm = WhisperModel("small", device="cuda", compute_type="float16")

    rows = []
    for name, src_name, out_name, text in pairs:
        s_path = os.path.join(args.dir, src_name)
        o_path = os.path.join(args.dir, out_name)
        if not (os.path.exists(s_path) and os.path.exists(o_path)):
            print(f"[skip] 缺文件：{src_name} / {out_name}")
            continue
        s_sim, o_sim = cos(emb(s_path), ref), cos(emb(o_path), ref)
        s_st, o_st = audio_stats(s_path), audio_stats(o_path)
        s_ov = overlap(asr_text(wm, s_path), text)
        o_ov = overlap(asr_text(wm, o_path), text)
        rows.append(dict(
            pair=name,
            campp_before=s_sim, campp_after=o_sim, campp_delta=o_sim - s_sim,
            asr_before=s_ov, asr_after=o_ov,
            peak_before=s_st["peak"], peak_after=o_st["peak"],
            clip_after=o_st["clip"], dur_before=s_st["dur"], dur_after=o_st["dur"],
        ))

    # 真人原音过同一个 RVC：该模型的音色上限（无 TTS 误差掺入）
    ceiling = None
    real_in = os.path.join(args.dir, "in_real_40k.wav")
    real_out = os.path.join(args.dir, "out_real.wav")
    if os.path.exists(real_in) and os.path.exists(real_out):
        ceiling = dict(
            campp_before=cos(emb(real_in), ref),
            campp_after=cos(emb(real_out), ref),
            **{f"out_{k}": v for k, v in audio_stats(real_out).items()},
        )

    print(f"\n参考音：{args.ref}")
    print(f"输出目录：{args.dir}\n")
    print(f"{'对照':<11}{'CAM++前':>9}{'CAM++后':>9}{'Δ':>9}"
          f"{'ASR前':>8}{'ASR后':>8}{'peak后':>9}{'削波%':>8}")
    print("-" * 71)
    for r in rows:
        print(f"{r['pair']:<11}{r['campp_before']:>9.4f}{r['campp_after']:>9.4f}"
              f"{r['campp_delta']:>+9.4f}{r['asr_before']:>8.3f}{r['asr_after']:>8.3f}"
              f"{r['peak_after']:>9.3f}{r['clip_after'] * 100:>8.2f}")
    print("-" * 71)

    if rows:
        m = lambda k: float(np.mean([r[k] for r in rows]))  # noqa: E731
        print(f"{'均值':<11}{m('campp_before'):>9.4f}{m('campp_after'):>9.4f}"
              f"{m('campp_delta'):>+9.4f}{m('asr_before'):>8.3f}{m('asr_after'):>8.3f}"
              f"{m('peak_after'):>9.3f}{m('clip_after') * 100:>8.2f}")
        if ceiling is not None:
            gap = ceiling["campp_after"] - m("campp_after")
            print(f"\n音色上限（真人原音→RVC）：CAM++ {ceiling['campp_before']:.4f} → "
                  f"{ceiling['campp_after']:.4f}   peak {ceiling['out_peak']:.3f}")
            print(f"TTS→RVC 距该上限还差 {gap:+.4f}"
                  f"（越小越说明瓶颈在 TTS 源码而非 RVC 模型）")

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(dict(ref=args.ref, dir=args.dir, rows=rows, ceiling=ceiling),
                  f, ensure_ascii=False, indent=2)
    print(f"\n已写入：{out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
