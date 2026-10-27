# -*- coding: utf-8 -*-
"""VFI 嗓音疲劳指数 + VAS 视觉模拟量表 计分（大创减负实测层 · 探索性指标，2026-10-09）。

定位（★ 先读这段）
    减负实测协议（`docs/附录-可执行规程.md`）的第二层证据：**主观自评量表**。
    它是**探索性**指标 —— 小样本、自评、前后测，只用来「看趋势」，
    **不作硬结论、不作任何诊断分档**，更不构成疗效证据。

出处（📚 文献转述，立项后核验一手）
    - Vocal Fatigue Index（VFI）：Nanjundeswaran, Jacobson, Gartner-Schmidt,
      2015, Journal of Voice。**19 条**，0（从不）– 4（总是）五级计分，三个因子：
        因子 1「用嗓后疲劳与嗓音恶化」11 条（v01–v11）
        因子 2「与发声相关的躯体不适」5 条（v12–v16）
        因子 3「休息后改善」3 条（v17–v19）
    - VAS（视觉模拟量表）：0–100 单条自评，这里按「每次录课前后各一次」采集。
    - 是否存在经信效度检验的中文版：**❓ 未核验**。立项后核验；核验之前
      只把本工具当**计分器**用，因子结构照上面记，别拿去对人群下结论。

为什么题目原文不在这份代码里
    本仓库要开源，量表条目文本有版权（使用需引用原始文献，部分量表还要求
    授权）。所以这里只做**计分**：题目文本由研究者从一手文献誊录后放进
    自己的纸质/表单问卷，回收时按 `v01..v19` 列名填进 CSV。

计分规则
    - 每个因子 = 组内条目**求和**（0–4 分/条 → 因子 1 满分 44、因子 2 满分 20、
      因子 3 满分 12）。
    - **非法值剔除并上报，绝不夹紧**（与 voicebank_eval.merge-listen 同一口径）：
      `v07=7` 不会夹到 4，而是该条记 invalid，**所在因子**该行不给分
      （求和里混进一个非法值，整个和都是错的）。
    - 因子间独立校验：因子 1 坏了不影响因子 2/3 照常给分 —— 丢整行等于把
      被试的有效判断也删了。
    - VAS 范围 0–100，越界同样只上报不给值。

用法
    # 打印 CSV 表头模板（发给填表人的列名）
    python tools/vfi_score.py --template

    # 计分（可合并多份回收表；CSV 用 Excel 填过也行，自动吃 BOM）
    python tools/vfi_score.py --in 甲.csv 乙.csv --out outputs/voicebank/vfi_scores.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

#: 三个因子的条目号（1 起）。📚 结构转述自 VFI 原始文献（见模块 docstring），
#: 立项后核验一手；如核验结果与此不符，改这里 + 对应单测。
FACTORS: dict[str, list[int]] = {
    "factor1": list(range(1, 12)),   # 用嗓后疲劳与嗓音恶化（11 条）
    "factor2": list(range(12, 17)),  # 与发声相关的躯体不适（5 条）
    "factor3": list(range(17, 20)),  # 休息后改善（3 条）
}
RATING_MIN, RATING_MAX = 0, 4
VAS_MIN, VAS_MAX = 0, 100
ITEM_COLS = [f"v{i:02d}" for i in range(1, 20)]
TEMPLATE_HEADER = ["participant_id", *ITEM_COLS, "vas_pre", "vas_post"]


def _parse_num(raw: str | None) -> float | None:
    """解析单元格；空串/None 返回 None（= 缺填），非数字抛 ValueError。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "":
        return None
    return float(s)


def score_row(row: dict) -> dict:
    """给一行问卷计分。返回因子分（非法/缺填的因子为 None）+ 逐条问题清单。"""
    out: dict = {"participant_id": str(row.get("participant_id", "")).strip()}
    invalid: list[str] = []
    missing: list[str] = []

    for fname, items in FACTORS.items():
        vals: list[float] = []
        ok = True
        for it in items:
            col = f"v{it:02d}"
            if col not in row:
                missing.append(col)
                ok = False
                continue
            try:
                v = _parse_num(row.get(col))
            except ValueError:
                invalid.append(f"{col}={row.get(col)!r}")
                ok = False
                continue
            if v is None:
                missing.append(col)
                ok = False
                continue
            if not (RATING_MIN <= v <= RATING_MAX) or v != int(v):
                invalid.append(f"{col}={row.get(col)!r}")
                ok = False
                continue
            vals.append(v)
        out[fname] = int(sum(vals)) if ok else None

    for vas in ("vas_pre", "vas_post"):
        if vas not in row:
            out[vas] = None
            continue
        try:
            v = _parse_num(row.get(vas))
        except ValueError:
            invalid.append(f"{vas}={row.get(vas)!r}")
            out[vas] = None
            continue
        if v is None or not (VAS_MIN <= v <= VAS_MAX):
            if v is not None:
                invalid.append(f"{vas}={row.get(vas)!r}")
            out[vas] = None  # ★ 越界/缺填一律不给值 —— 101 不能混进均值
            continue
        out[vas] = v
    out["notes"] = "; ".join(invalid + missing)
    return out


def score_csv(rows: list[dict]) -> tuple[list[dict], dict]:
    """整表计分。汇总**先算每人的分，再对人数平均**（只平均给得出分的行）。"""
    scores = [score_row(r) for r in rows]
    summary: dict = {"n": len(scores)}
    for fname in FACTORS:
        vals = [s[fname] for s in scores if s[fname] is not None]
        summary[fname] = {
            "n_valid": len(vals),
            "mean": round(sum(vals) / len(vals), 2) if vals else None,
        }
    for vas in ("vas_pre", "vas_post"):
        vals = [s[vas] for s in scores if s.get(vas) is not None]
        summary[vas] = {
            "n_valid": len(vals),
            "mean": round(sum(vals) / len(vals), 2) if vals else None,
        }
    summary["n_rows_with_notes"] = sum(1 for s in scores if s["notes"])
    return scores, summary


def read_csv(path: Path) -> list[dict]:
    """utf-8-sig 优先（Excel 另存 UTF-8 带 BOM 是常态），退回 utf-8。"""
    data = path.read_bytes()
    text = data.decode("utf-8-sig")
    return list(csv.DictReader(text.splitlines()))


def write_scores(scores: list[dict], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["participant_id", *FACTORS.keys(), "vas_pre", "vas_post", "notes"]
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for s in scores:
            row = {**s,
                   "vas_pre": "" if s.get("vas_pre") is None else s["vas_pre"],
                   "vas_post": "" if s.get("vas_post") is None else s["vas_post"]}
            w.writerow(row)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="VFI/VAS 计分（探索性指标）")
    ap.add_argument("--in", dest="inputs", nargs="+", default=None,
                    help="回收的问卷 CSV（可多份合并）")
    ap.add_argument("--out", default=None, help="计分结果 CSV 落盘路径")
    ap.add_argument("--template", action="store_true", help="打印 CSV 表头模板")
    args = ap.parse_args(argv)

    if args.template:
        print(",".join(TEMPLATE_HEADER))
        return 0
    if not args.inputs:
        ap.error("需要 --in <问卷csv...>（或用 --template 看表头）")

    rows: list[dict] = []
    for p in args.inputs:
        rows.extend(read_csv(Path(p)))
    if not rows:
        print("[vfi_score] 一行数据都没读到，先检查 CSV 是不是空的", file=sys.stderr)
        return 2

    scores, summary = score_csv(rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    bad = [s for s in scores if s["notes"]]
    if bad:
        print(f"⚠ {len(bad)} 行有问题（已按上面口径剔除该字段，未夹紧、未丢弃整行）：")
        for s in bad:
            print(f"  {s['participant_id'] or '(无编号)'}: {s['notes']}")
    if args.out:
        write_scores(scores, Path(args.out))
        print(f"[已落盘] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
