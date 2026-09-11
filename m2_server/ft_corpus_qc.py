# -*- coding: utf-8 -*-
"""微调语料体检与一键剔除（A3）：训练前的语料质量闸门。

背景：`clip_qc`（P1-1）已经会给**流水线切片**打分，但微调工坊（/ft）的训练入口
完全没消费这个分数——录音切出来的 D 级垃圾（静音、爆音、底噪、他人声）照样进
训练集，是"调出来鬼叫/不像"的主要来源（袋鼠音色已踩过一次）。

本模块把质检接到微调链路上，提供三件事：

    1. 体检报告 `build_report()`  —— 对 media/ft/<id>/clips/*.wav 逐条打分，
       汇总等级分布、问题原因 Top、可操作建议，落盘 corpus_qc.json（带指纹缓存）。
    2. 一键剔除 `prune()`        —— 把低分切片**移入** clips_rejected/（不删除，
       可恢复），同步从 train_raw.jsonl 摘掉对应样本、重选锚点并刷新所有行的
       ref_audio，最后更新 status.json。
    3. 恢复 `restore()`          —— 把 clips_rejected/ 的切片与 train_rejected.jsonl
       的样本放回训练集，重选锚点后重算体检。

设计约定：
  - **绝不删除音频**：剔除=移动，随时可恢复（数据集比磁盘金贵）。
  - 锚点（anchor）是所有行 ref_audio 的指向，剔除/恢复后必须重选并回写，
    否则训练会指向一个已被移走的参考音频。
  - 打分复用 `clip_qc.score_clip`，其"任何异常都降级为维度缺失、绝不抛"的约定
    在这里同样成立——体检失败不能阻断训练。
  - 声纹维度（with_spk）默认关闭：需加载 CAM++，且中心声纹由"无 spk 打分里最
    好的 3 条"均值而来，避免锚点本身是脏样本时把中心带偏。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import config as cfg

# 训练样本下限（与 finetune._process 的 "有效转写样本 < 8 报错" 保持一致）
MIN_TRAIN_SAMPLES = 8
# 建议的最低有效语音总时长（秒）：低于此值少样本微调很难稳
MIN_TRAIN_SECONDS = 60.0

DEFAULT_KEEP_GRADES = ("A", "B")


# ---------------- 路径 ----------------

def ft_dir(voice_id: str) -> Path:
    return Path(cfg.MEDIA_DIR) / "ft" / voice_id


def clips_dir(voice_id: str) -> Path:
    return ft_dir(voice_id) / "clips"


def rejected_dir(voice_id: str) -> Path:
    return ft_dir(voice_id) / "clips_rejected"


def train_jsonl(voice_id: str) -> Path:
    return ft_dir(voice_id) / "train_raw.jsonl"


def rejected_jsonl(voice_id: str) -> Path:
    return ft_dir(voice_id) / "train_rejected.jsonl"


def report_path(voice_id: str) -> Path:
    return ft_dir(voice_id) / "corpus_qc.json"


def status_path(voice_id: str) -> Path:
    return ft_dir(voice_id) / "status.json"


def clip_paths(voice_id: str) -> list[Path]:
    d = clips_dir(voice_id)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.wav"))


def rejected_paths(voice_id: str) -> list[Path]:
    d = rejected_dir(voice_id)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.wav"))


# ---------------- 状态读写 ----------------

def _read_status(voice_id: str) -> dict:
    p = status_path(voice_id)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"stage": "new", "voice_id": voice_id}


def _write_status(voice_id: str, **kw) -> dict:
    d = ft_dir(voice_id)
    d.mkdir(parents=True, exist_ok=True)
    st = _read_status(voice_id)
    st.update(kw)
    st["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    (d / "status.json").write_text(
        json.dumps(st, ensure_ascii=False, indent=1), "utf-8")
    return st


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    rows = []
    for line in p.read_text("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return rows


def _write_jsonl(p: Path, rows: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")
    tmp.replace(p)


def _stem_of(row: dict) -> str:
    return Path(str(row.get("audio", "")).replace("\\", "/")).stem


# ---------------- 打分 ----------------

def _fingerprint(paths: list[Path]) -> str:
    if not paths:
        return "0:0"
    return f"{len(paths)}:{max(int(p.stat().st_mtime) for p in paths)}"


def _embed(path: Path):
    """切片声纹（CAM++ 192 维，L2 归一化）；失败返回 None。"""
    try:
        import speaker_sep
    except Exception:  # noqa: BLE001
        return None
    try:
        a16 = speaker_sep._read16k(Path(path))
        if a16.size < int(0.4 * 16000):
            return None
        return speaker_sep._sv_embed(a16)
    except Exception:  # noqa: BLE001
        return None


def _spk_center(paths: list[Path]):
    """主说话人中心声纹：先用无 spk 的分数挑最好的 3 条，再取声纹均值。

    不直接用锚点（最长切片）：锚点可能是"长但脏"的样本，会把中心带偏。
    """
    import numpy as np

    scored = []
    for p in paths:
        try:
            import clip_qc
            it = clip_qc.score_clip(p)
        except Exception:  # noqa: BLE001
            continue
        scored.append((it.get("score", 0), it.get("duration_s", 0.0), p))
    scored.sort(key=lambda t: (-t[0], -t[1]))

    embs = []
    for _s, _d, p in scored[:3]:
        e = _embed(p)
        if e is not None:
            embs.append(e)
    if not embs:
        return None
    c = np.mean(np.stack(embs), axis=0)
    n = float(np.linalg.norm(c))
    return c / n if n > 0 else None


def _reason_key(reason: str) -> str:
    """把带数字的原因归一化，便于聚合 Top 问题。

    "信噪比 8dB 偏低（底噪或伴奏残留）" → "信噪比偏低"
    """
    head = str(reason).split("（")[0]
    key = re.sub(r"[-+]?\d+(\.\d+)?\s*(dB|dBFS|s|%)?", "", head)
    return re.sub(r"\s+", "", key).strip("，,。 ") or str(reason)[:12]


def _advice(grades: dict, ok_count: int, total_s: float, rejected: int) -> list[str]:
    out = []
    if grades.get("D", 0):
        out.append(f"{grades['D']} 条切片不合格（D 级），建议先「一键剔除」再训练"
                   f"——脏样本是克隆失败/鬼叫的主要来源")
    if grades.get("C", 0):
        out.append(f"{grades['C']} 条切片勉强可用（C 级），语料充足时可一并剔除换取更干净的训练集")
    if ok_count < MIN_TRAIN_SAMPLES:
        out.append(f"可用切片仅 {ok_count} 条（建议 ≥{MIN_TRAIN_SAMPLES} 条），"
                   f"剔除前请先补录，否则训练会因样本不足失败")
    if 0 < total_s < MIN_TRAIN_SECONDS:
        out.append(f"有效语音 {total_s:.0f}s（建议 ≥{MIN_TRAIN_SECONDS:.0f}s），"
                   f"语料偏少时微调收益有限")
    if rejected:
        out.append(f"已剔除 {rejected} 条（存于 clips_rejected，可随时恢复）")
    if not out:
        out.append("语料质量良好，可直接开始训练")
    return out


def load_report(voice_id: str) -> dict | None:
    p = report_path(voice_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def build_report(voice_id: str, with_spk: bool = False, force: bool = False) -> dict:
    """给微调语料做体检并落盘；指纹未变且维度一致时直接返回缓存。"""
    import clip_qc

    paths = clip_paths(voice_id)
    fp = _fingerprint(paths)
    cached = None if force else load_report(voice_id)
    if cached and cached.get("fingerprint") == fp and bool(cached.get("has_spk")) == bool(with_spk):
        return cached

    center = _spk_center(paths) if (with_spk and paths) else None

    clips: dict[str, dict] = {}
    for p in paths:
        try:
            clips[p.stem] = clip_qc.score_clip(p, center)
        except Exception as e:  # noqa: BLE001
            clips[p.stem] = {"name": p.stem, "score": 0, "grade": "D",
                             "reasons": [f"质检异常（{type(e).__name__}）"],
                             "duration_s": 0.0, "spk_sim": None, "metrics": {}}

    grades = {"A": 0, "B": 0, "C": 0, "D": 0}
    reasons: dict[str, int] = {}
    total_s = 0.0
    score_sum = 0
    for it in clips.values():
        g = it.get("grade", "D")
        grades[g] = grades.get(g, 0) + 1
        total_s += float(it.get("duration_s") or 0.0)
        score_sum += int(it.get("score") or 0)
        for r in it.get("reasons") or []:
            k = _reason_key(r)
            reasons[k] = reasons.get(k, 0) + 1

    n = len(clips)
    top_reasons = [{"reason": k, "count": v}
                   for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])[:5]]

    payload = {
        "voice_id": voice_id,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fingerprint": fp,
        "count": n,
        "has_spk": bool(center is not None),
        "grades": grades,
        "ok_count": grades["A"] + grades["B"],
        "avg_score": round(score_sum / n, 1) if n else 0.0,
        "total_s": round(total_s, 1),
        "rejected_count": len(rejected_paths(voice_id)),
        "top_reasons": top_reasons,
        "advice": _advice(grades, grades["A"] + grades["B"], total_s,
                          len(rejected_paths(voice_id))),
        "clips": clips,
    }
    try:
        report_path(voice_id).parent.mkdir(parents=True, exist_ok=True)
        report_path(voice_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
    except Exception:  # noqa: BLE001
        pass  # 落盘失败不影响本次返回
    return payload


# ---------------- 剔除 / 恢复 ----------------

def _mutable_or_raise(voice_id: str, training: bool = False) -> None:
    """处理中 / 训练中不允许改语料（后台线程正在读写同样的文件）。"""
    st = _read_status(voice_id)
    if st.get("stage") in ("processing",):
        raise RuntimeError("语料正在处理中，请等待处理完成后再调整")
    if training:
        raise RuntimeError("该音色正在训练中，不能调整语料")


def _durations(rows: list[dict]) -> float:
    """统计样本总时长（读 wav，失败按 0 计）。"""
    import soundfile as sf
    total = 0.0
    for r in rows:
        try:
            x, sr = sf.read(str(r.get("audio", "")).replace("\\", "/"))
            total += (len(x) / float(sr)) if sr else 0.0
        except Exception:  # noqa: BLE001
            continue
    return total


def _pick_anchor(rows: list[dict]) -> str | None:
    """锚点 = 最长样本（speaker 嵌入最稳），与 finetune._process 的选法一致。"""
    import soundfile as sf

    best, best_len = None, -1
    for r in rows:
        try:
            x, _ = sf.read(str(r.get("audio", "")).replace("\\", "/"))
        except Exception:  # noqa: BLE001
            continue
        if len(x) > best_len:
            best, best_len = r, len(x)
    return str(best.get("audio", "")).replace("\\", "/") if best else None


def _apply_anchor(rows: list[dict], anchor: str | None) -> list[dict]:
    if not anchor:
        return rows
    for r in rows:
        r["ref_audio"] = anchor
    return rows


def prune(voice_id: str, keep_grades=DEFAULT_KEEP_GRADES, min_score: int | None = None,
          training: bool = False) -> dict:
    """把低分切片移入 clips_rejected，并从训练集中摘掉对应样本。

    keep_grades: 保留的等级（默认 A/B）；min_score: 额外的最低分门槛。
    返回 {kept, moved, moved_names, rows, anchor, warning}，绝不抛业务异常以外的内容。
    """
    _mutable_or_raise(voice_id, training=training)
    paths = clip_paths(voice_id)
    if not paths:
        raise RuntimeError("该音色没有可体检的切片")

    report = build_report(voice_id, force=True)
    keep = {g.upper() for g in (keep_grades or DEFAULT_KEEP_GRADES)}
    rows = _read_jsonl(train_jsonl(voice_id))

    drop_stems: set[str] = set()
    for name, it in (report.get("clips") or {}).items():
        g = str(it.get("grade", "D")).upper()
        s = int(it.get("score") or 0)
        if g not in keep or (min_score is not None and s < int(min_score)):
            drop_stems.add(name)

    if not drop_stems:
        return {"voice_id": voice_id, "kept": len(rows), "moved": 0,
                "moved_names": [], "rows": len(rows), "anchor": None, "warning": ""}

    rej_dir = rejected_dir(voice_id)
    rej_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for stem in sorted(drop_stems):
        src = clips_dir(voice_id) / f"{stem}.wav"
        if not src.exists():
            continue
        try:
            shutil.move(str(src), str(rej_dir / f"{stem}.wav"))
            moved.append(stem)
        except OSError as e:
            raise RuntimeError(f"移动切片失败 {stem}.wav：{e}") from e

    keep_rows = [r for r in rows if _stem_of(r) not in drop_stems]
    drop_rows = [r for r in rows if _stem_of(r) in drop_stems]

    # 被剔除的样本另存一份，恢复时才能拿回转写文本
    if drop_rows:
        old_rej = [r for r in _read_jsonl(rejected_jsonl(voice_id))
                   if _stem_of(r) not in {_stem_of(r2) for r2 in drop_rows}]
        _write_jsonl(rejected_jsonl(voice_id), old_rej + drop_rows)

    anchor = _pick_anchor(keep_rows)
    keep_rows = _apply_anchor(keep_rows, anchor)
    _write_jsonl(train_jsonl(voice_id), keep_rows)

    speech_s = _durations(keep_rows)
    warning = ""
    if len(keep_rows) < MIN_TRAIN_SAMPLES:
        warning = (f"剔除后仅剩 {len(keep_rows)} 条样本（训练需 ≥{MIN_TRAIN_SAMPLES} 条），"
                   f"请补录或点「恢复」撤回本次剔除")

    _write_status(
        voice_id,
        clips=len(keep_rows),
        speech_s=round(speech_s, 1),
        anchor=(Path(anchor).name if anchor else None),
        rejected=len(rejected_paths(voice_id)),
        qc={"grades": report.get("grades"), "avg_score": report.get("avg_score"),
            "updated_at": report.get("updated_at")},
    )
    build_report(voice_id, force=True)

    return {"voice_id": voice_id, "kept": len(keep_rows), "moved": len(moved),
            "moved_names": moved, "rows": len(keep_rows),
            "speech_s": round(speech_s, 1), "anchor": anchor,
            "rejected": len(rejected_paths(voice_id)), "warning": warning}


def restore(voice_id: str, training: bool = False) -> dict:
    """把 clips_rejected 里的切片与样本全部放回训练集。"""
    _mutable_or_raise(voice_id, training=training)
    rej_paths = rejected_paths(voice_id)
    rej_rows = _read_jsonl(rejected_jsonl(voice_id))
    if not rej_paths and not rej_rows:
        return {"voice_id": voice_id, "restored": 0, "rows": 0, "anchor": None}

    cdir = clips_dir(voice_id)
    cdir.mkdir(parents=True, exist_ok=True)
    restored: list[str] = []
    for p in rej_paths:
        dst = cdir / p.name
        if dst.exists():      # 同名已存在（例如重新处理产生过同名切片）→ 保留现役
            continue
        try:
            shutil.move(str(p), str(dst))
            restored.append(p.stem)
        except OSError as e:
            raise RuntimeError(f"恢复切片失败 {p.name}：{e}") from e

    rows = _read_jsonl(train_jsonl(voice_id))
    have = {_stem_of(r) for r in rows}
    add = [r for r in rej_rows if _stem_of(r) not in have]
    rows += add
    # 未恢复的（文件被占用/同名冲突）留在 rejected 清单里
    _write_jsonl(rejected_jsonl(voice_id),
                 [r for r in rej_rows if _stem_of(r) not in {_stem_of(a) for a in add}])

    anchor = _pick_anchor(rows)
    rows = _apply_anchor(rows, anchor)
    _write_jsonl(train_jsonl(voice_id), rows)

    speech_s = _durations(rows)
    _write_status(
        voice_id,
        clips=len(rows),
        speech_s=round(speech_s, 1),
        anchor=(Path(anchor).name if anchor else None),
        rejected=len(rejected_paths(voice_id)),
    )
    build_report(voice_id, force=True)
    report = load_report(voice_id) or {}

    return {"voice_id": voice_id, "restored": len(restored), "rows": len(rows),
            "speech_s": round(speech_s, 1), "anchor": anchor,
            "rejected": len(rejected_paths(voice_id)),
            "grades": report.get("grades", {})}
