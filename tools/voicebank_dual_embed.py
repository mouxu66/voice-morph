"""双空间声纹最小实验：Qwen 自空间余弦 vs CAM++ 余弦的同人/异人分离度。

为什么有这个脚本（2026-10-11）：
- 评测引擎里的 `emb_sim` 用的是 Qwen3-TTS **自己的说话人编码器**（与克隆条件
  同一空间）。pilotC 上 3s 档就 0.9815、30s 饱和到 0.9854 —— 疑似"自空间抬高"：
  数值很高，但对"像不像本人"的分辨力存疑。
- 仓库里还有第二、且独立的声纹：CAM++（modelscope，192 维，Apache-2.0）。
- 本脚本在同一批合成样本上同时算两把尺子，用两组配对做验证：
    同人 = 样本 vs 自己的那一档参考音
    异人 = 样本 vs 同档位、别的说话人的参考音（异人对照必须同档，否则时长混淆）
  比较三件事：① 分离度（ROC-AUC / EER / d'）② 天花板占用（同人分 >= 0.98 的占比）
  ③ 时长敏感性（两把尺子的同人均值随参考音时长的跨度）。

口径（写死，别漂）：
- 两把尺子任一不可用即**报错退出**——本实验的结论就是两把尺子对比，缺一把没意义。
- 结果只写 `<exp>/dual_embed/`，不碰原始样本；嵌入有缓存（按文件大小失效）。
- 素材边界照 `pilotC-公开语料证据/README.md`：公开许可语料、非被试、不外推。

用法：
    python tools/voicebank_dual_embed.py --exp pilotC            # 增量（缓存命中跳过）
    python tools/voicebank_dual_embed.py --exp pilotC --force    # 全部重算
    python tools/voicebank_dual_embed.py --exp pilotC --root outputs/voicebank
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "tools", ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

WORKER = "http://127.0.0.1:8001"
TOP_FLOOR = 0.98  # "天花板占用"阈值：同人分 >= 0.98 的占比


def _default_root() -> Path:
    """默认落盘根 = `<OUTPUTS_DIR>/voicebank`（延迟 import config，同 voicebank_eval 口径）。"""
    import config as cfg

    return Path(cfg.OUTPUTS_DIR) / "voicebank"


# ---------------- 两把尺子 ----------------

def _worker_health(timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(WORKER + "/health", timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def _qwen_embedding(path: Path, *, retries: int = 3, timeout: int = 300) -> np.ndarray:
    """Qwen 自空间声纹（worker /emb，与克隆同一编码器；服务端已 L2 归一化）。"""
    payload = json.dumps({"path": str(path)}).encode()  # ensure_ascii 转义中文路径
    last = ""
    for _ in range(retries):
        req = urllib.request.Request(
            WORKER + "/emb", data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            continue
        if data.get("emb"):
            return np.asarray(data["emb"], dtype=float)
        last = str(data.get("error") or "空 emb")
    raise RuntimeError(f"Qwen 声纹提取失败（{path.name}）：{last}")


def _cam_embedding(path: Path) -> np.ndarray:
    """CAM++ 独立声纹（modelscope 192 维，L2 归一化；失败抛错不静默）。"""
    m2 = str(ROOT / "m2_server")
    if m2 not in sys.path:
        sys.path.insert(0, m2)
    import speaker_sep  # 重依赖（modelscope/torch），按需导入

    emb = speaker_sep._sv_embed(speaker_sep._read16k(path))
    if emb is None:
        raise RuntimeError(f"CAM++ 声纹提取失败：{path.name}")
    return np.asarray(emb, dtype=float)


# ---------------- 配对 ----------------

def collect_layout(exp_dir: Path) -> dict:
    """扫 refs/<说话人>/<档>.wav 与 samples/<说话人>/<档>/<条件>.wav。"""
    refs: dict[tuple[str, str], Path] = {}
    for p in sorted((exp_dir / "refs").glob("*/*.wav")):
        refs[(p.parent.name, p.stem)] = p
    samples = []
    for p in sorted((exp_dir / "samples").glob("*/*/*.wav")):
        samples.append(
            {"speaker": p.parent.parent.name, "dur": p.parent.name, "cond": p.stem, "path": p}
        )
    return {"refs": refs, "samples": samples}


def build_pairs(layout: dict, base: Path) -> tuple[list[dict], list[dict]]:
    """同人 = 自己那一档参考音；异人 = 同档、其他说话人的参考音。"""
    refs = layout["refs"]
    speakers = sorted({sp for sp, _ in refs})

    def rel(p: Path) -> str:
        return p.relative_to(base).as_posix()

    same: list[dict] = []
    cross: list[dict] = []
    for s in layout["samples"]:
        sid = f"{s['speaker']}/{s['dur']}/{s['cond']}"
        sid_rel = rel(s["path"])
        own = refs.get((s["speaker"], s["dur"]))
        if own is not None:
            same.append({
                "role": "same", "speaker": s["speaker"], "dur": s["dur"], "cond": s["cond"],
                "sample_id": sid, "sample_rel": sid_rel,
                "ref_id": f"{s['speaker']}/{s['dur']}", "ref_rel": rel(own),
            })
        for other in speakers:
            if other == s["speaker"]:
                continue
            ref2 = refs.get((other, s["dur"]))
            if ref2 is not None:
                cross.append({
                    "role": "cross", "speaker": s["speaker"], "dur": s["dur"], "cond": s["cond"],
                    "sample_id": sid, "sample_rel": sid_rel,
                    "ref_id": f"{other}/{s['dur']}", "ref_rel": rel(ref2),
                })
    return same, cross


# ---------------- 统计（纯函数，零依赖只留 numpy） ----------------

def cos(a, b) -> float:
    """余弦相似度；零向量直接报错，不返回 0 糊弄。"""
    va = np.asarray(a, dtype=float)
    vb = np.asarray(b, dtype=float)
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0.0 or nb == 0.0:
        raise ValueError("零向量没有余弦")
    return float(np.dot(va, vb) / (na * nb))


def _rankdata(values: np.ndarray) -> np.ndarray:
    """平均秩（并列取平均），供 AUC 用。"""
    x = np.asarray(values, dtype=float)
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(x.size, dtype=float)
    sx = x[order]
    i = 0
    while i < sx.size:
        j = i
        while j + 1 < sx.size and sx[j + 1] == sx[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def auc(same_scores, cross_scores) -> float | None:
    """P(同人分 > 异人分)，并列记一半。0.5 = 扔硬币，1.0 = 完全分开。"""
    a = np.asarray(same_scores, dtype=float)
    b = np.asarray(cross_scores, dtype=float)
    if a.size == 0 or b.size == 0:
        return None
    allv = np.concatenate([a, b])
    r = _rankdata(allv)
    u = float(r[: a.size].sum()) - a.size * (a.size + 1) / 2.0
    return float(u / (a.size * b.size))


def eer(same_scores, cross_scores) -> float | None:
    """等错误率：扫一遍阈值，取「异人误收」与「同人漏收」最接近的那个点的均值。"""
    a = np.sort(np.asarray(same_scores, dtype=float))
    b = np.sort(np.asarray(cross_scores, dtype=float))
    if a.size == 0 or b.size == 0:
        return None
    cand = np.unique(np.concatenate([a, b]))
    if cand.size > 1:
        cand = np.unique(np.concatenate([cand, (cand[:-1] + cand[1:]) / 2.0]))
    best_gap: float | None = None
    best_err = None
    for t in cand:
        fpr = float(np.mean(b >= t))  # 得分 >= t 判为"同人" → 异人误收
        fnr = float(np.mean(a < t))   # 同人漏收
        gap = abs(fpr - fnr)
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best_err = (fpr + fnr) / 2.0
    return float(best_err) if best_err is not None else None


def dprime(same_scores, cross_scores) -> float | None:
    """d' = 均值差 / 合并标准差。两组都没方差（分母 0）时返回 None，不编数字。"""
    a = np.asarray(same_scores, dtype=float)
    b = np.asarray(cross_scores, dtype=float)
    if a.size < 2 or b.size < 2:
        return None
    denom = math.sqrt((a.var(ddof=1) + b.var(ddof=1)) / 2.0)
    if denom <= 0:
        return None
    return float((a.mean() - b.mean()) / denom)


def brief(values, *, top_floor: float | None = None) -> dict:
    a = np.asarray(values, dtype=float)
    out = {
        "n": int(a.size),
        "mean": round(float(a.mean()), 4) if a.size else None,
        "std": round(float(a.std(ddof=1)), 4) if a.size > 1 else None,
        "min": round(float(a.min()), 4) if a.size else None,
        "max": round(float(a.max()), 4) if a.size else None,
    }
    if top_floor is not None:
        out["top_frac"] = round(float(np.mean(a >= top_floor)), 4) if a.size else None
    return out


def _round_opt(x: float | None) -> float | None:
    return None if x is None else round(float(x), 4)


def summarize(same_scores, cross_scores, *, top_floor: float = TOP_FLOOR) -> dict:
    a = np.asarray(same_scores, dtype=float)
    b = np.asarray(cross_scores, dtype=float)
    return {
        "same": brief(a, top_floor=top_floor),
        "cross": brief(b),
        "auc": _round_opt(auc(a, b)),
        "eer": _round_opt(eer(a, b)),
        "dprime": _round_opt(dprime(a, b)),
    }


# ---------------- 主流程 ----------------

def _dur_key(tag: str) -> int:
    try:
        return int(str(tag).rstrip("sS"))
    except ValueError:
        return 10**9


def run(exp: str, *, root: Path | None = None, force: bool = False) -> dict:
    base = (root or _default_root()) / exp
    if not (base / "samples").exists():
        raise RuntimeError(f"找不到样本目录：{base / 'samples'}（先跑 voicebank_eval plan/synth）")
    layout = collect_layout(base)
    if not layout["refs"] or not layout["samples"]:
        raise RuntimeError(f"{base} 下 refs/samples 收集为空——检查目录结构")
    if not _worker_health():
        raise RuntimeError(
            "worker 8001 未就绪：先启动 Qwen3-TTS worker"
            "（m2_server/qwen3_tts_service.py，见 voicebank_eval 同款启动说明）"
        )

    same_pairs, cross_pairs = build_pairs(layout, base)
    out_dir = base / "dual_embed"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_path = out_dir / "embeddings.json"
    cache: dict = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    files: dict[str, Path] = {}
    for (sp, dur), p in sorted(layout["refs"].items()):
        files[f"refs/{sp}/{dur}.wav"] = p
    for s in layout["samples"]:
        files[f"samples/{s['speaker']}/{s['dur']}/{s['cond']}.wav"] = s["path"]

    computed = 0
    for i, (rel, p) in enumerate(sorted(files.items()), 1):
        size = p.stat().st_size
        for kind, fn in (("qwen", _qwen_embedding), ("cam", _cam_embedding)):
            entry = (cache.get(kind) or {}).get(rel)
            if entry and entry.get("size") == size and not force:
                continue
            vec = fn(p)
            cache.setdefault(kind, {})[rel] = {"size": size, "vec": [float(x) for x in vec]}
            computed += 1
            if computed % 10 == 0:
                cache_path.write_text(json.dumps(cache), encoding="utf-8")
        print(f"[{i}/{len(files)}] {rel}", flush=True)
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    def vec(kind: str, rel: str) -> np.ndarray:
        return np.asarray(cache[kind][rel]["vec"], dtype=float)

    rows: list[dict] = []
    for pr in same_pairs + cross_pairs:
        row = dict(pr)
        row["qwen_cos"] = round(cos(vec("qwen", pr["sample_rel"]), vec("qwen", pr["ref_rel"])), 4)
        row["cam_cos"] = round(cos(vec("cam", pr["sample_rel"]), vec("cam", pr["ref_rel"])), 4)
        rows.append(row)

    stats = {}
    by_duration: dict[str, dict] = {}
    by_condition: dict[str, dict] = {}
    for metric in ("qwen_cos", "cam_cos"):
        s_same = [r[metric] for r in rows if r["role"] == "same"]
        s_cross = [r[metric] for r in rows if r["role"] == "cross"]
        stats[metric] = summarize(s_same, s_cross)
        for dur in sorted({r["dur"] for r in rows}, key=_dur_key):
            by_duration.setdefault(dur, {})[metric] = {
                "same": brief([r[metric] for r in rows if r["role"] == "same" and r["dur"] == dur],
                              top_floor=TOP_FLOOR),
                "cross": brief([r[metric] for r in rows if r["role"] == "cross" and r["dur"] == dur]),
            }
        for cond in sorted({r["cond"] for r in rows}):
            by_condition.setdefault(cond, {})[metric] = brief(
                [r[metric] for r in rows if r["role"] == "same" and r["cond"] == cond]
            )

    duration_spread = {
        m: round(
            max(by_duration[d][m]["same"]["mean"] for d in by_duration)
            - min(by_duration[d][m]["same"]["mean"] for d in by_duration),
            4,
        )
        for m in ("qwen_cos", "cam_cos")
    }

    results = {
        "exp": exp,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "base": str(base),
        "top_floor": TOP_FLOOR,
        "files": len(files),
        "computed": computed,
        "n_same_pairs": len(same_pairs),
        "n_cross_pairs": len(cross_pairs),
        "stats": stats,
        "by_duration": by_duration,
        "by_condition": by_condition,
        "duration_spread": duration_spread,
        "pairs": rows,
    }

    with (out_dir / "pairs.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["role", "speaker", "dur", "cond", "sample_id", "ref_id", "qwen_cos", "cam_cos"])
        for r in rows:
            w.writerow([r["role"], r["speaker"], r["dur"], r["cond"],
                        r["sample_id"], r["ref_id"], r["qwen_cos"], r["cam_cos"]])
    (out_dir / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    write_report(results, out_dir)
    return results


def write_report(results: dict, out_dir: Path) -> Path:
    st = results["stats"]
    bd = results["by_duration"]

    def dist(m: str, bucket: str) -> str:
        b = st[m][bucket]
        if b["mean"] is None:
            return "-"
        return f"{b['mean']}（{b['min']}–{b['max']}）"

    lines = [
        f"# 双空间声纹最小实验 · {results['exp']}",
        "",
        f"- 生成时间：{results['created_at']}　|　样本：{results['n_same_pairs']} 同人配对"
        f" + {results['n_cross_pairs']} 异人配对（同档、别的说话人）",
        "- 两把尺子：**Qwen 自空间**（/emb，与克隆同一编码器）· **CAM++**（独立声纹，192 维）",
        "- 口径：余弦前 L2 归一化；异人对照只取**同档位**（时长是自变量，不能混）",
        "",
        "## 1 总表",
        "",
        "| 尺子 | 同人分 mean（min–max） | 异人分 mean（min–max） | AUC | EER | d′ | 同人 ≥0.98 占比 |",
        "|---|---|---|---|---|---|---|",
        f"| Qwen 自空间 | {dist('qwen_cos', 'same')} | {dist('qwen_cos', 'cross')} | "
        f"{st['qwen_cos']['auc']} | {st['qwen_cos']['eer']} | "
        f"{st['qwen_cos']['dprime']} | {st['qwen_cos']['same']['top_frac']} |",
        f"| CAM++ | {dist('cam_cos', 'same')} | {dist('cam_cos', 'cross')} | "
        f"{st['cam_cos']['auc']} | {st['cam_cos']['eer']} | "
        f"{st['cam_cos']['dprime']} | {st['cam_cos']['same']['top_frac']} |",
        "",
        "## 2 逐档（同人分）",
        "",
        "| 档位 | Qwen mean | Qwen ≥0.98 | CAM++ mean | CAM++ ≥0.98 |",
        "|---|---|---|---|---|",
    ]
    for dur in sorted(bd, key=_dur_key):
        q = bd[dur]["qwen_cos"]["same"]
        c = bd[dur]["cam_cos"]["same"]
        lines.append(
            f"| {dur} | {q['mean']} | {q['top_frac']} | {c['mean']} | {c['top_frac']} |"
        )
    lines += [
        "",
        f"- 同人均值随时长的跨度（max−min）：Qwen **{results['duration_spread']['qwen_cos']}**，"
        f"CAM++ **{results['duration_spread']['cam_cos']}** —— 跨度大 = 这把尺子看得见时长差异。",
        "",
        "## 3 怎么读",
        "",
        "- **AUC** = P(同人分 > 异人分)：0.5 等于扔硬币，1.0 是完全分开。**EER** 越低越好，"
        "**d′** 越大越分得开。",
        "- **≥0.98 占比**看天花板占用：同人分都挤在 0.98 以上时，阈值型门禁几乎没有分辨空间"
        "（全过或全挂）。",
        "- 分离度看“能不能区分你是不是我”；天花板占用看“在同人区间里还能不能分辨好坏”"
        "——自动门禁需要的是后者。",
        "",
        "## 4 边界（照抄 pilotC README，不要藏）",
        "",
        "- 公开许可语料（AISHELL-3，Apache-2.0），**非被试数据**；参考音是同一说话人同一次录制的"
        "多条拼接（保住人/麦/房间，丢掉同一条 take）。",
        "- n = 3 说话人、单机单卡、中文；结论只作本项目观测，不外推总体。",
        "- 复现：`python tools/voicebank_dual_embed.py --exp " + results["exp"] + "`"
        "（worker 8001 需在跑；嵌入缓存见 `dual_embed/embeddings.json`）。",
    ]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv=None) -> int:
    # 重定向到管道时 Python 按 cp936 写 stdout —— 中文会变乱码，这里统一钉成 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="双空间声纹最小实验（Qwen 自空间 vs CAM++）")
    ap.add_argument("--exp", required=True, help="实验名（voicebank root 下的目录，如 pilotC）")
    ap.add_argument("--root", type=Path, default=None, help="实验根（默认 OUTPUTS/voicebank）")
    ap.add_argument("--force", action="store_true", help="忽略嵌入缓存，全部重算")
    args = ap.parse_args(argv)
    try:
        results = run(args.exp, root=args.root, force=args.force)
    except Exception as exc:  # noqa: BLE001
        print(f"[dual-embed] 失败：{exc}", file=sys.stderr)
        return 1
    st = results["stats"]
    q, c = st["qwen_cos"], st["cam_cos"]
    print(f"[dual-embed] 同人 {results['n_same_pairs']} / 异人 {results['n_cross_pairs']}")
    print(f"[dual-embed] Qwen: AUC={q['auc']} EER={q['eer']} d'={q['dprime']}"
          f" 同人≥{results['top_floor']} 占比={q['same']['top_frac']}")
    print(f"[dual-embed] CAM++: AUC={c['auc']} EER={c['eer']} d'={c['dprime']}"
          f" 同人≥{results['top_floor']} 占比={c['same']['top_frac']}")
    print(f"[dual-embed] 结果目录：{Path(results['base']) / 'dual_embed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
