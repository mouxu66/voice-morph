# -*- coding: utf-8 -*-
"""切片质检与自动优选（P1-1）：切片级打分 + 等级 + 不合格原因 + 建库自动优选。

与 `tools/voice_qc.py` 的分工：
    - `tools/voice_qc.py` 打分对象是**训练后的音色**（验证这个模型能不能用）；
    - 本模块打分对象是**建库前的切片**（决定这段素材配不配进参考音频），
      是"脏素材清洗 + 切片质量排序"的落点。

维度与权重见 docs/ROADMAP.md 的 P1-1：

    时长 20 / 响度 15 / 削波 15 / 中段静音 15 / 底噪 SNR 15 / 说话人一致性 20

规则：
    - 任一维度越过硬判废线 → 直接 D 级，并在 reasons 里给一句人话解释；
    - 说话人一致性是最强信号（他人声或 BGM 残留基本都能被它抓住），但依赖
      CAM++ 声纹、成本较高，缺失时不参与加权（分数按剩余维度归一化，仍可比较）；
    - 结果按素材聚合落盘 `outputs/clip_qc/<prefix>.json`，切片数量或最新 mtime
      变化时指纹失效、自动重算；
    - **任何一步异常都降级为"该维度缺失"，绝不抛给调用方**——流水线跑完后的
      自动打分依赖这个约定（照抄 rvc_live._maybe_run_qc 的静默模式）。

命令行自查：
    python m2_server/clip_qc.py --prefix video_xxx     # 只扫某个素材的切片
    python m2_server/clip_qc.py --all                  # 扫全部切片（不含声纹维度）
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

import config as cfg

QC_DIR = cfg.OUTPUTS_DIR / "clip_qc"

# ---------------- 阈值（按真实素材标定后可整体调整）----------------
DUR_OK = (2.5, 10.0)        # 合格区间：沿用 pipeline 的 CLIP_MIN_MS / CLIP_MAX_MS
DUR_FAIL = (1.5, 12.0)      # 硬判废
LOUD_OK = (-20.0, -3.0)     # max_dBFS 合格区间
LOUD_FAIL_FLOOR = -50.0     # 沿用 pipeline_clean 的判废阈值
CLIP_FAIL = 0.005           # |x| > 0.99 的采样占比
# 实测标定（2026-09-01，奶龙合集 39 条）：pipeline 按 >=350ms 静音切片，单条内部
# 天然带句间静音，3~10s 正常切片的中段静音普遍在 0.27~0.35，取 0.40 会误杀，故放宽。
MID_SILENCE_FAIL = 0.60     # 首/末有效语音之间的静音占比
SPEECH_MIN = 0.25           # 有效语音占切片时长的比例下限（低于此≈静音或纯伴奏）
SNR_FAIL = 10.0             # dB
SPK_SIM_FAIL = 0.50         # 与主说话人中心声纹的余弦相似度（低于即一票否决）
SPK_SIM_FULL = 0.75         # 相似度拿满分的线
SNR_FULL = 30.0             # SNR 拿满分的线

WEIGHTS = {
    "duration": 20,
    "loudness": 15,
    "clipping": 15,
    "silence": 15,
    "snr": 15,
    "speaker": 20,
}

GRADE_A = 80
GRADE_B = 60
GRADE_C = 40


# ---------------- 信号分析 ----------------

def _read(path: Path) -> tuple[np.ndarray, int]:
    """读音频为 (float32 单声道, 采样率)。"""
    import soundfile as sf
    x, sr = sf.read(str(path), dtype="float32")
    if x.ndim == 2:
        x = x[:, 0]
    return np.asarray(x, dtype=np.float32), int(sr)


def _rms_frames(x: np.ndarray, sr: int, win_ms: float = 20.0) -> np.ndarray:
    """分帧 RMS（默认 20ms 一帧）。"""
    n = max(1, int(sr * win_ms / 1000.0))
    if x.size < n:
        return np.empty(0)
    m = (x.size // n) * n
    return np.sqrt((x[:m].reshape(-1, n) ** 2).mean(axis=1) + 1e-12)


def analyze_signal(x: np.ndarray, sr: int) -> dict:
    """算一条切片的客观指标（不判分，纯测量）。"""
    dur = x.size / float(sr) if sr else 0.0
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    loud = 20.0 * math.log10(peak + 1e-12)
    clip_ratio = float(np.mean(np.abs(x) > 0.99)) if x.size else 1.0

    rms = _rms_frames(x, sr)
    if rms.size == 0:
        return {"duration_s": dur, "loudness_dbfs": loud, "clip_ratio": clip_ratio,
                "snr_db": 0.0, "mid_silence": 1.0, "head_silence": 1.0,
                "tail_silence": 1.0, "speech_ratio": 0.0}

    # 低分位能量≈底噪，高分位≈语音；阈值取两者折中，避免整段都很响时误判
    low = float(np.percentile(rms, 20))
    high = float(np.percentile(rms, 95))
    thr = max(low * 6.0, high * 0.10, 1e-4)
    speech = rms > thr
    if not speech.any():
        return {"duration_s": dur, "loudness_dbfs": loud, "clip_ratio": clip_ratio,
                "snr_db": 0.0, "mid_silence": 1.0, "head_silence": 1.0,
                "tail_silence": 1.0, "speech_ratio": 0.0}

    idx = np.flatnonzero(speech)
    s0, s1 = int(idx[0]), int(idx[-1])
    mid = speech[s0:s1 + 1]
    snr = 20.0 * math.log10(float(rms[speech].mean()) / (low + 1e-12))
    return {
        "duration_s": dur,
        "loudness_dbfs": loud,
        "clip_ratio": clip_ratio,
        "snr_db": snr,
        "mid_silence": 1.0 - float(mid.mean()),
        "head_silence": s0 / rms.size,
        "tail_silence": (rms.size - 1 - s1) / rms.size,
        "speech_ratio": float(speech.mean()),
    }


# ---------------- 判分 ----------------

def _ramp(v: float, lo: float, hi: float) -> float:
    """v<=lo → 0，v>=hi → 1，中间线性。"""
    if hi == lo:
        return 0.0
    return float(max(0.0, min(1.0, (v - lo) / (hi - lo))))


def _score_duration(d: float) -> float:
    lo_ok, hi_ok = DUR_OK
    if lo_ok <= d <= hi_ok:
        return 1.0
    if d < lo_ok:
        return _ramp(d, DUR_FAIL[0], lo_ok)
    return 1.0 - _ramp(d, hi_ok, DUR_FAIL[1])


def _score_loud(l: float) -> float:
    lo_ok, hi_ok = LOUD_OK
    if lo_ok <= l <= hi_ok:
        return 1.0
    if l < lo_ok:
        return _ramp(l, LOUD_FAIL_FLOOR, lo_ok)
    return 1.0 - _ramp(l, hi_ok, 0.0)


def _speaker_sim(path: Path, center) -> float | None:
    """切片声纹与主说话人中心的余弦相似度；失败返回 None（不参与判分）。"""
    try:
        import speaker_sep
        a16 = speaker_sep._read16k(path)          # 同项目模块，内部已处理重采样
        if a16.size < int(0.4 * 16000):
            return None
        emb = speaker_sep._sv_embed(a16)
        if emb is None:
            return None
        c = np.asarray(center, dtype=np.float64).reshape(-1)
        n = float(np.linalg.norm(c))
        if n <= 0:
            return None
        return float(np.dot(emb, c / n))
    except Exception:  # noqa: BLE001
        return None


def score_clip(path, spk_center=None) -> dict:
    """对单个切片打分，返回 {name, score, grade, reasons, duration_s, metrics...}。"""
    p = Path(path)
    item = {"name": p.stem, "score": 0, "grade": "D", "reasons": [],
            "duration_s": 0.0, "spk_sim": None, "metrics": {}}
    try:
        x, sr = _read(p)
    except Exception as e:  # noqa: BLE001
        item["reasons"].append(f"无法读取音频（{type(e).__name__}）")
        return item

    try:
        m = analyze_signal(x, sr)
    except Exception as e:  # noqa: BLE001
        item["reasons"].append(f"音频分析失败（{type(e).__name__}）")
        return item

    parts = {
        "duration": _score_duration(m["duration_s"]),
        "loudness": _score_loud(m["loudness_dbfs"]),
        "clipping": 1.0 - _ramp(m["clip_ratio"], 0.0, CLIP_FAIL),
        "silence": 1.0 - _ramp(m["mid_silence"], 0.0, MID_SILENCE_FAIL),
        "snr": _ramp(m["snr_db"], SNR_FAIL, SNR_FULL),
    }
    reasons: list[str] = []

    if not (DUR_FAIL[0] <= m["duration_s"] <= DUR_FAIL[1]):
        reasons.append(f"时长 {m['duration_s']:.1f}s 不可用（合格 {DUR_OK[0]}–{DUR_OK[1]}s）")
    if m["loudness_dbfs"] < LOUD_FAIL_FLOOR:
        reasons.append(f"响度 {m['loudness_dbfs']:.0f}dBFS 过低（近乎静音）")
    if m["clip_ratio"] > CLIP_FAIL:
        reasons.append(f"削波 {m['clip_ratio'] * 100:.1f}%（爆音）")
    if m["mid_silence"] > MID_SILENCE_FAIL:
        reasons.append(f"中段静音 {m['mid_silence'] * 100:.0f}%（语音不连续）")
    # 静音/纯伴奏兜底：mid_silence 只看首末语音之间，整段没语音时会被它漏掉
    if m["speech_ratio"] < SPEECH_MIN:
        reasons.append(f"有效语音仅 {m['speech_ratio'] * 100:.0f}%（多为静音或伴奏）")
    if m["snr_db"] < SNR_FAIL:
        reasons.append(f"信噪比 {m['snr_db']:.0f}dB 偏低（底噪或伴奏残留）")

    sim = None
    if spk_center is not None:
        sim = _speaker_sim(p, spk_center)
        if sim is not None:
            parts["speaker"] = _ramp(sim, SPK_SIM_FAIL, SPK_SIM_FULL)
            if sim < SPK_SIM_FAIL:
                reasons.append(f"声纹与主说话人不一致（{sim:.2f}），疑似他人声或伴奏残留")

    # 缺失维度不参与加权，保证不同素材间分数可比较
    num = sum(WEIGHTS[k] * v for k, v in parts.items())
    den = sum(WEIGHTS[k] for k in parts)
    score = int(round(100.0 * num / den)) if den else 0

    if reasons:
        grade = "D"
    elif score >= GRADE_A:
        grade = "A"
    elif score >= GRADE_B:
        grade = "B"
    elif score >= GRADE_C:
        grade = "C"
    else:
        grade = "D"
        reasons.append("综合分过低")

    item.update(
        score=score,
        grade=grade,
        reasons=reasons,
        duration_s=round(m["duration_s"], 2),
        spk_sim=(round(sim, 3) if sim is not None else None),
        metrics={k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()},
    )
    return item


# ---------------- 素材级：落盘与缓存 ----------------

def material_file(prefix: str) -> Path:
    return QC_DIR / f"{prefix}.json"


def _fingerprint(paths: list[Path]) -> str:
    if not paths:
        return "0:0"
    return f"{len(paths)}:{max(int(p.stat().st_mtime) for p in paths)}"


def load_material(prefix: str) -> dict | None:
    f = material_file(prefix)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def load_all() -> dict[str, dict]:
    """全部素材的质检结果，拍平成 {切片名: 质检项}（供 /clips 与自动优选直接查）。"""
    out: dict[str, dict] = {}
    if not QC_DIR.exists():
        return out
    for f in sorted(QC_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for name, item in (d.get("clips") or {}).items():
            out[name] = item
    return out


def score_material(prefix: str, paths, spk_center=None, force: bool = False,
                   main_spk: int | None = None) -> dict:
    """给一个素材的全部切片打分并落盘；指纹未变且维度齐全时直接返回缓存。

    spk_center: 主说话人中心声纹（CAM++ 192 维），传了才计算"说话人一致性"维度。
    """
    paths = sorted(Path(p) for p in paths)
    fp = _fingerprint(paths)
    cached = None if force else load_material(prefix)
    if cached and cached.get("fingerprint") == fp and (
            spk_center is None or cached.get("has_spk")):
        return cached

    clips: dict[str, dict] = {}
    for p in paths:
        try:
            clips[p.stem] = score_clip(p, spk_center)
        except Exception as e:  # noqa: BLE001
            clips[p.stem] = {"name": p.stem, "score": 0, "grade": "D",
                             "reasons": [f"质检异常（{type(e).__name__}）"],
                             "duration_s": 0.0, "spk_sim": None, "metrics": {}}

    grades = {"A": 0, "B": 0, "C": 0, "D": 0}
    for it in clips.values():
        grades[it.get("grade", "D")] = grades.get(it.get("grade", "D"), 0) + 1

    payload = {
        "prefix": prefix,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fingerprint": fp,
        "count": len(clips),
        "has_spk": spk_center is not None,
        "main_spk": main_spk,
        "center": (np.asarray(spk_center, dtype=float).round(6).tolist()
                   if spk_center is not None else None),
        "grades": grades,
        "ok_count": grades["A"] + grades["B"],
        "clips": clips,
    }
    try:
        QC_DIR.mkdir(parents=True, exist_ok=True)
        material_file(prefix).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass  # 落盘失败不影响本次返回结果
    return payload


def score_prefixes(prefixes, clips_dir, force: bool = False) -> dict:
    """批量打分（流水线跑完后自动调用，不含声纹维度，保持轻量）。

    返回汇总 {materials, total, ok, grades:{A,B,C,D}}。
    """
    clips_dir = Path(clips_dir)
    summary = {"materials": 0, "total": 0, "ok": 0,
               "grades": {"A": 0, "B": 0, "C": 0, "D": 0}}
    for pre in prefixes:
        if not pre:
            continue
        try:
            paths = [p for p in clips_dir.glob("*.wav") if p.stem.startswith(pre)]
            if not paths:
                continue
            payload = score_material(pre, paths, force=force)
        except Exception:  # noqa: BLE001
            continue
        summary["materials"] += 1
        summary["total"] += payload.get("count", 0)
        for k, v in (payload.get("grades") or {}).items():
            summary["grades"][k] = summary["grades"].get(k, 0) + v
    summary["ok"] = summary["grades"]["A"] + summary["grades"]["B"]
    return summary


# ---------------- 自动优选 ----------------

def recommend(target_s: float = 30.0, only_grades=("A", "B"), limit: int | None = None):
    """跨素材挑分最高的切片，累计到 target_s 为止。返回 (切片名列表, 总时长)。"""
    items = []
    for name, it in load_all().items():
        if it.get("grade") not in only_grades:
            continue
        items.append((it.get("score", 0), it.get("duration_s", 0.0), name))
    items.sort(key=lambda t: (-t[0], -t[1]))

    picked: list[str] = []
    total = 0.0
    for score, dur, name in items:
        picked.append(name)
        total += dur
        if total >= target_s:
            break
        if limit and len(picked) >= limit:
            break
    return picked, round(total, 1)


# ---------------- 命令行 ----------------

def _main():
    ap = argparse.ArgumentParser(description="切片质检（P1-1）")
    ap.add_argument("--prefix", help="只扫该前缀的切片（素材切片前缀）")
    ap.add_argument("--all", action="store_true", help="扫全部切片")
    ap.add_argument("--force", action="store_true", help="忽略缓存重算")
    args = ap.parse_args()

    clips_dir = cfg.MEDIA_DIR / "clips"
    if args.prefix:
        prefixes = [args.prefix]
    elif args.all:
        prefixes = sorted({f.stem[:12] for f in clips_dir.glob("*.wav")})
    else:
        ap.error("需指定 --prefix 或 --all")
        return

    summary = score_prefixes(prefixes, clips_dir, force=args.force)
    print(f"素材 {summary['materials']} 个 / 切片 {summary['total']} 条")
    print(f"等级分布 A={summary['grades']['A']} B={summary['grades']['B']} "
          f"C={summary['grades']['C']} D={summary['grades']['D']}")
    print(f"可用（A+B）{summary['ok']} 条")
    picked, total = recommend()
    print(f"自动优选示例（目标 30s）：{len(picked)} 条 / {total}s")
    print(f"结果目录：{QC_DIR}")


if __name__ == "__main__":
    _main()
