# -*- coding: utf-8 -*-
"""A/B 双标尺评测：CAM++ 独立声纹 + Qwen 自空间 + Praat 音色继承 + ASR 回译。

用主 venv（.venv）跑：modelscope(CAM++) + parselmouth(Praat) 都在那边。
Qwen 自空间需要 worker 8001；不可用时该项记 None（不中断）。

判据方向（见 skill svc-timbre-diagnosis 与 docs 调研-SVC路线与嗓音客观指标）：
  cam_sim     越高越像目标人     （CAM++ 独立声纹，主判）
  qwen_self   越高越像目标人     （Qwen 自空间，保底/对照）
  hnr         越高越干净
  h1_h2       越低越自然（越大越"挤压"）
  spectral_tilt 越负越"挤压"
  jitter/shimmer 越低越稳
  asr_overlap 越高越可懂
"""
import argparse
import json
import os
import sys
import wave
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WORK = ROOT / "media" / "ft_work" / "ppt12"
AB = WORK / "out_ab"
TOOLS = ROOT / "tools"
sys.path.insert(0, str(ROOT / "m2_server"))
sys.path.insert(0, str(TOOLS))

REF = WORK / "ref_24k.wav"
# 干净目标人参考：用训练用的原始 24k 演讲音（长）做声纹基准更稳
TARGET_LONG = WORK / "source_24k.wav"

M2 = ROOT / "m2_server"


def cos(a, b):
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    d = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / d) if d > 0 else None


def cam_emb(path):
    """CAM++ 独立声纹（本项目 speaker_sep 单例）。

    ★ 长音频要分段：实测 CAM++ 对 766s 整条返回 None（输入过长），
      所以超过 30s 就切成 20s 段分别算再平均（L2 归一化后平均，再归一化）。
    """
    import speaker_sep
    a = speaker_sep._read16k(Path(path))
    if a is None or a.size == 0:
        return None
    sr = 16000
    if a.size <= 30 * sr:
        return speaker_sep._sv_embed(a)
    vs = []
    step = 20 * sr
    for s0 in range(0, a.size - sr, step):
        seg = a[s0:s0 + step]
        if seg.size < 2 * sr:      # 太短没意义
            continue
        v = speaker_sep._sv_embed(seg)
        if v is not None:
            vs.append(v)
        if len(vs) >= 12:          # 最多 12 段（4 分钟）够估声纹
            break
    if not vs:
        return None
    m = np.mean(np.stack(vs), axis=0)
    n = float(np.linalg.norm(m))
    return m / n if n > 0 else None


def qwen_emb(path):
    """Qwen 自空间声纹（worker 8001 /emb）。不可用返回 None。"""
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:8001/emb",
            data=json.dumps({"path": str(path)}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
        if d.get("error") or not d.get("emb"):
            return None
        return np.asarray(d["emb"], dtype=np.float64)
    except Exception:
        return None


def wav_dur(path):
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except Exception:
        import soundfile as sf
        info = sf.info(str(path))
        return info.duration


_ASR = {"m": None}


def asr_text(path):
    """ASR 回译（可懂度）。懒加载模型。"""
    try:
        from faster_whisper import WhisperModel
        if _ASR["m"] is None:
            _ASR["m"] = WhisperModel("small", device="cuda", compute_type="float16")
        segs, _ = _ASR["m"].transcribe(str(path), language="zh", vad_filter=False)
        t = "".join(s.text for s in segs)
        try:
            import opencc
            t = opencc.OpenCC("t2s").convert(t)
        except Exception:
            pass
        import re
        return re.sub(r"[^\u4e00-\u9fff0-9%.A-Za-z]", "", t)
    except Exception:
        return None


def overlap(a, b):
    """字符集合 IoU（口径同 tools/voice_qc.py::_char_overlap）。"""
    if not a or not b:
        return None
    A, B = set(a.replace("，", "")), set(b.replace("，", ""))
    return round(len(A & B) / max(len(A | B), 1), 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(AB / "ab_scores.json"))
    ap.add_argument("--no-qwen", action="store_true")
    ap.add_argument("--no-asr", action="store_true")
    args = ap.parse_args()

    import voice_metrics

    # 参考声纹
    print("[eval] 计算参考声纹 ...", flush=True)
    cam_ref = cam_emb(TARGET_LONG)
    cam_ref_short = cam_emb(REF)
    print(f"[eval] CAM++ 目标声纹: 长参考 {np.linalg.norm(cam_ref):.3f}, "
          f"短参考 {np.linalg.norm(cam_ref_short):.3f}")
    # 自一致性基线：目标人自己两段的相似度（上限参考）
    qwen_ref = None if args.no_qwen else qwen_emb(TARGET_LONG)

    # 收集样本（从生成记录读回 prompt 文本，用于 ASR 可懂度比对）
    gen = {}
    gp = AB / "gen_result.jsonl"
    if gp.exists():
        for line in gp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("path"):
                    gen[os.path.basename(r["path"])] = r
    samples = []
    for arm in ("A", "B"):
        d = AB / arm
        if not d.exists():
            continue
        for p in sorted(d.glob("*.wav")):
            g = gen.get(p.name, {})
            samples.append({"arm": arm, "path": str(p), "name": p.name,
                            "text": g.get("text", "")})
    if not samples:
        print("[eval] 没有样本，先跑 ab_generate.py")
        return

    print(f"[eval] {len(samples)} 个样本", flush=True)
    out = []
    for s in samples:
        row = dict(s)
        row["dur"] = round(wav_dur(s["path"]), 2)
        # 1) CAM++ 独立声纹
        e = cam_emb(s["path"])
        row["cam_sim"] = round(cos(e, cam_ref), 4) if e is not None else None
        row["cam_sim_shortref"] = round(cos(e, cam_ref_short), 4) if e is not None else None
        # 2) Qwen 自空间
        if not args.no_qwen:
            q = qwen_emb(s["path"])
            row["qwen_self"] = round(cos(q, qwen_ref), 4) if (q is not None and qwen_ref is not None) else None
        # 3) Praat 音色继承
        m = voice_metrics.extract(s["path"])
        for k in ("hnr", "h1_h2", "spectral_tilt", "jitter_local",
                  "shimmer_local", "f0_median", "voiced_ratio"):
            row[k] = m.get(k)
        row["metrics_error"] = m.get("error")
        # 4) ASR 回译可懂度
        if not args.no_asr:
            back = asr_text(s["path"])
            row["asr_back"] = (back or "")[:200]
            row["asr_overlap"] = overlap(back, s.get("text") or "")
            row["asr_chars"] = len(back) if back else 0
            # 生成失控检测：字符数 / 期望字符数
            exp = len((s.get("text") or "").replace("，", ""))
            row["asr_len_ratio"] = round(row["asr_chars"] / max(exp, 1), 2) if back else None
        out.append(row)
        print(f"  {s['arm']}/{s['name']}  cam={row['cam_sim']}  "
              f"qwen={row.get('qwen_self')}  hnr={row['hnr']}  tilt={row['spectral_tilt']}  "
              f"asr={row.get('asr_overlap')} len×{row.get('asr_len_ratio')}", flush=True)

    # 汇总
    summary = {}
    for arm in ("A", "B"):
        rows = [r for r in out if r["arm"] == arm]
        if not rows:
            continue
        agg = {}
        for k in ("cam_sim", "cam_sim_shortref", "qwen_self", "hnr", "h1_h2",
                  "spectral_tilt", "jitter_local", "shimmer_local", "dur"):
            vals = [r[k] for r in rows if r.get(k) is not None]
            agg[k] = {"n": len(vals),
                      "mean": round(float(np.mean(vals)), 4) if vals else None,
                      "median": round(float(np.median(vals)), 4) if vals else None}
        summary[arm] = agg

    payload = {"ref": str(REF), "target_long": str(TARGET_LONG),
               "samples": out, "summary": summary}
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 74)
    print(f"{'指标':<18}{'A臂(零样本)':>16}{'B臂(微调)':>16}{'Δ(B-A)':>14}")
    print("-" * 74)
    keys = ["cam_sim", "cam_sim_shortref", "qwen_self", "hnr", "h1_h2",
            "spectral_tilt", "jitter_local", "shimmer_local"]
    for k in keys:
        a = summary.get("A", {}).get(k, {}).get("mean")
        b = summary.get("B", {}).get(k, {}).get("mean")
        if a is None and b is None:
            continue
        d = (b - a) if (a is not None and b is not None) else None
        print(f"{k:<18}{a if a is not None else '-':>16}{b if b is not None else '-':>16}"
              f"{round(d,4) if d is not None else '-':>14}")
    print("=" * 74)
    print(f"\n[eval] -> {args.out}")


if __name__ == "__main__":
    main()
