"""reply_advisor 端到端质量评测（真机，需本地模型 + 6G 显存）。

**为什么放 tools/ 而不是 m2_server/tests/**：
跑一轮 8 场景要 60~90 秒、要真实模型和显存，塞进 pytest 会让 `check.py --fast`
从 260s 涨到 350s+，而且在不具备条件的机器（CI、别人的笔记本）上会整片红。
单元测试守规则，这个守「模型换成新版本 / prompt 改过之后，用户实际拿到的东西有没有变差」。

用法：
    .venv/Scripts/python.exe tools/eval_reply_quality.py                  # 默认 3 轮
    .venv/Scripts/python.exe tools/eval_reply_quality.py --rounds 1       # 快速冒烟
    .venv/Scripts/python.exe tools/eval_reply_quality.py --json r.json    # 存报告
    .venv/Scripts/python.exe tools/eval_reply_quality.py --engine D:\\llama-b10488-win\\llama-server.exe
    .venv/Scripts/python.exe tools/eval_reply_quality.py --only 问时间,让带东西

退出码：0 = 全部达标；1 = 有场景未达标；2 = 模型不可用（先修环境，不算质量回归）。

**场景怎么定的**：每个场景都对应一条「这个产品必须做对的事」，不是凑数。
`must_any` 只要求命中一个词，故意放宽 —— 目的是抓"整体跑偏"（比如问时间只回了个梗），
不是把某一次措辞钉死。用 `--rounds` 多跑几轮抵消 temperature=0.4 的随机性。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "m2_server"))

#: 评测图：内容完全已知，读错任何一条都能看出来。
#: 跟代码一起版本化（`tools/make_eval_image.py` 可重新生成），评测才能复现。
#: ⚠️ 别放 D:\\tmp —— 2026-10-26 实测那里的文件会被系统清掉（图和脚本一起没）。
BENCH_IMG = ROOT / "m2_server" / "tests" / "fixtures" / "reply_eval_activity.png"

PUNCT = re.compile(r"[，。！？、；：,.!?]")

#: 每条场景：name / messages / image / must_any / must_not / advice_must / min_replies
CASES: list[dict] = [
    {
        "name": "问时间",
        "messages": [
            {"side": "left", "kind": "text", "text": "今晚几点集合啊"},
            {"side": "right", "kind": "text", "text": "我看看"},
            {"side": "left", "kind": "text", "text": "八点还是九点，你定"},
        ],
        "must_any": ["点", "今晚", "时间", "几点"],
        "why": "必须落到具体时间或明确问一句，不能只接梗",
    },
    {
        "name": "问地点",
        "messages": [
            {"side": "left", "kind": "text", "text": "你们现在在哪呢"},
            {"side": "left", "kind": "text", "text": "我到楼下了"},
        ],
        "must_any": ["楼下", "到了", "在", "哪", "门口", "马上"],
        "why": "要给出位置或说清不知道",
    },
    {
        "name": "被夸起哄",
        "messages": [
            {"side": "left", "kind": "text", "text": "哇大佬"},
            {"side": "left", "kind": "text", "text": "高手高手"},
            {"side": "left", "kind": "text", "text": "佩服佩服"},
        ],
        "must_any": [],  # 可以不回，也可以接梗
        "must_not": ["谢谢", "过奖", "哪里哪里", "不客气", "客气了", "承让"],
        "why": "被夸不能道谢/自谦（2026-10-25 实测 3 次里 2 次犯）",
    },
    {
        "name": "让带东西",
        "messages": [
            {"side": "left", "kind": "text", "text": "你回来帮我带一份呗"},
            {"side": "left", "kind": "text", "text": "就楼下那家"},
        ],
        "must_any": ["啥", "什么", "哪家", "哪个", "几份", "看看", "不一定"],
        "must_not": ["^好$", "^行$", "^可以$", "没问题", "包在我身上"],
        "why": "不能替用户答应；要么问清楚要么说不确定",
    },
    {
        "name": "发图问活动",
        "image": str(BENCH_IMG),
        "messages": [
            {"side": "left", "kind": "text", "text": "这周活动排了一堆，我只想拿那个限定皮肤"},
            {"side": "right", "kind": "text", "text": "哪个活动给皮肤啊"},
            {"side": "left", "kind": "text", "text": "你看下表告诉我做哪几个"},
        ],
        "must_any": ["29", "军备"],
        "advice_must": [("29", "军备"), ("三阶段", "12")],
        "why": "必须认出 29 号军备竞演 + 三阶段，且不得编造图上没有的活动",
    },
    {
        "name": "纯闲聊",
        "messages": [
            {"side": "left", "kind": "text", "text": "今天好热啊"},
            {"side": "left", "kind": "text", "text": "空调都不敢开"},
        ],
        "must_any": [],
        "max_len": 20,
        "why": "可接梗，别写小作文",
    },
    {
        "name": "只有一条语音",
        "messages": [{"side": "left", "kind": "voice", "seconds": 8, "text": ""}],
        # 不判内容：这条链路本来就拿不到内容，候选只能是「在」「听到了」这类应答词。
        # 要守的是**别让用户以为模型听懂了** —— note 必须把「读不到」说出来。
        "expect_note": "读不到",
        "why": "语音只有 [语音 8 秒] 占位符，内容拿不到，但用户本人在微信里听得到",
    },
    {
        "name": "表情包连发",
        "messages": [
            {"side": "left", "kind": "media", "text": "动画表情"},
            {"side": "left", "kind": "media", "text": "动画表情"},
        ],
        "must_any": [],
        "max_replies": 3,
        "why": "表情包连发，最多接个梗",
    },
    {
        "name": "语音+问听得到",
        "messages": [
            {"side": "left", "kind": "voice", "seconds": 8, "text": ""},
            {"side": "left", "kind": "text", "text": "听得到吗"},
        ],
        # 「你说」「啥事」「咋了」也是合格回答 —— 用户听到「听得到吗」直接让对方说，
        # 字面上没有「听」字，但语义上就是「我听得见，你说」（2026-10-26 r2 实测）。
        "must_any": ["听", "能", "嗯", "在", "收到", "你说", "啥事", "咋了", "什么"],
        "must_not": ["语音里说的", "你刚才说", "你说的那个"],
        "why": "读不到语音内容时不许编内容",
    },
]


def _setup_env(engine: str | None) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001 —— 没有 .env 也能跑（走 _find_exe 兜底）
        pass
    os.environ["VM_REPLY_IDLE_S"] = "600"  # 评测期间别让空闲自杀插手
    if engine:
        os.environ["VM_REPLY_LLAMA_SERVER"] = engine


def _run_case(ra, case: dict) -> dict:
    t0 = time.perf_counter()
    if case.get("image"):
        out = ra.suggest_from_digest(case["messages"], {"items": []}, chat="3864",
                                     image=case["image"])
    else:
        out = ra.suggest(case["messages"], chat="3864")
    dt = time.perf_counter() - t0

    reps = out.get("replies") or []
    adv = out.get("advice") or []
    joined = " ".join(reps)

    fails: list[str] = []
    if out.get("engine") != "local":
        fails.append(f"engine={out.get('engine')}（降级了：{out.get('note') or '无 note'}）")
    if len(reps) < case.get("min_replies", 1) and case.get("must_any"):
        fails.append(f"replies 只有 {len(reps)} 条")
    if case.get("must_any") and not any(k in joined for k in case["must_any"]):
        fails.append(f"没命中 {case['must_any']} → {reps}")
    for bad in case.get("must_not") or []:
        # 逐条匹配而不是拼起来 —— `^好$` 这种锚点对拼接串是失效的（2026-10-26 踩过）
        hit = [r for r in reps if re.search(bad, r)]
        if hit:
            fails.append(f"出现禁止词 {bad!r} → {hit}")
    if case.get("max_len") and any(len(r) > case["max_len"] for r in reps):
        fails.append(f"有回复超过 {case['max_len']} 字 → {reps}")
    if case.get("max_replies") and len(reps) > case["max_replies"]:
        fails.append(f"replies {len(reps)} 条 > {case['max_replies']}")
    # 标点**不算失败**、只计数：`_SYSTEM` 第 144 行本来就要求「几乎不打标点」，
    # 而 2026-10-26 实测纯文本链路 4/58 条带标点（7%）、比用户本人的 12% 还低，
    # 说明这条约束模型是照做的；把它当 FAIL 只会把风格抖动伪装成红线问题。
    # 图链路那 3/9 条是 `3~10 字` 与「要说清活动名+门槛」在打架，不是标点问题。
    for group in case.get("advice_must") or []:
        blob = " ".join(adv)
        if not any(k in blob for k in group):
            fails.append(f"advice 里没有 {group} 任一词 → {adv}")

    dropped = out.get("dropped") or []
    note = out.get("note") or ""
    # 有些场景的正确行为不是「说什么」，而是「说清楚自己不知道」——
    # 比如对方只发了一条语音（语音在 prompt 里只有 `[语音 8 秒]` 占位符）。
    if case.get("expect_note") and case["expect_note"] not in note:
        fails.append(f"note 里缺 {case['expect_note']!r} → {note!r}")
    return {
        "case": case["name"],
        "ok": not fails,
        "fails": fails,
        "dt": round(dt, 1),
        "engine": out.get("engine"),
        "replies": reps,
        "advice": adv,
        "note": note,
        # 直接从 `dropped` 读，不再靠 note 文案猜 —— 那句话 2026-10-26 起会写真实
        # 命中的规则（可能是「复述聊天记录里的话」而不是「替你答应」），猜就会漏报。
        "dropped": dropped,
        "punished": bool(dropped),
        # 从「答应 + 问细节」里拆出来的半句。有它就说明模型又在写「行，几点去拿？」，
        # 但这次拆完还能用，没有变成「全删光退回模板」。
        "salvaged": out.get("salvaged") or [],
        # 解析彻底失败时模型到底写了什么 —— 以前这条信息直接丢掉，失败无从追查。
        "raw": out.get("raw") or "",
        "punct": sum(1 for r in reps if PUNCT.search(r)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--engine", default=None, help="llama-server.exe 路径（默认按 .env）")
    ap.add_argument("--only", default=None, help="只跑这些场景，逗号分隔")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    _setup_env(args.engine)
    from m2_server import reply_advisor as ra  # noqa: E402

    ok, why = ra.available()
    if not ok:
        print(f"模型不可用：{why}")
        return 2
    print(f"engine : {ra._find_exe()}")
    print(f"model  : {ra._find_model()}")
    print(f"rounds : {args.rounds}\n")

    cases = CASES
    if args.only:
        want = {s.strip() for s in args.only.split(",")}
        cases = [c for c in CASES if c["name"] in want]
        if not cases:
            print(f"没有匹配的场景：{want}")
            return 2

    all_rows: list[dict] = []
    try:
        for rnd in range(1, args.rounds + 1):
            for case in cases:
                row = _run_case(ra, case)
                row["round"] = rnd
                all_rows.append(row)
                mark = "PASS" if row["ok"] else "FAIL"
                extra = "" if row["ok"] else "  ← " + " | ".join(row["fails"])
                print(f"[{mark}] r{rnd} {row['case']:<10} {row['dt']:5.1f}s "
                      f"{len(row['replies'])}条 {row['replies']}{extra}")
    finally:
        ra.shutdown()

    # ---- 汇总 ----
    total = len(all_rows)
    passed = sum(1 for r in all_rows if r["ok"])
    local = sum(1 for r in all_rows if r["engine"] == "local")
    punct = sum(r["punct"] for r in all_rows)
    punished = sum(1 for r in all_rows if r["punished"])
    salvaged = sum(1 for r in all_rows if r["salvaged"])
    failed_parse = sum(1 for r in all_rows if r["raw"])
    empty = sum(1 for r in all_rows if not r["replies"])

    print("\n" + "=" * 66)
    by_case: dict[str, list[dict]] = {}
    for r in all_rows:
        by_case.setdefault(r["case"], []).append(r)
    for name, rows in by_case.items():
        p = sum(1 for r in rows if r["ok"])
        avg_n = sum(len(r["replies"]) for r in rows) / len(rows)
        filtered = sum(1 for r in rows if r["punished"])
        flag = "" if p == len(rows) else "  ← 有失败"
        print(f"  {name:<12} 通过 {p}/{len(rows)}  均 {avg_n:.1f} 条"
              f"{f'  被过滤 {filtered} 次' if filtered else ''}{flag}")
    print("-" * 66)
    print(f"  总计 {passed}/{total} 通过 | local {local}/{total} | 标点 {punct} 条 "
          f"| 候选被过滤 {punished} 轮 | 空回复 {empty} 次")
    print(f"  答应词拆分 {salvaged} 轮 | 解析彻底失败 {failed_parse} 轮")
    avg_dt = sum(r["dt"] for r in all_rows) / total
    print(f"  均耗时 {avg_dt:.1f}s")

    # 解析彻底失败的原始输出：这条以前是黑色地带 —— 评测只报「没通过校验」，
    # 模型写了什么完全不知道，也就没法判断该补修复逻辑还是改 prompt。
    for r in all_rows:
        if r["raw"]:
            print(f"\n  [解析失败] {r['case']} r{r.get('round', '?')} 模型原文：")
            print(f"    {r['raw']!r}")

    # 被拆开的候选：「行，几点去拿？」→「几点去拿？」。看拆得对不对比看删除数更有用。
    salvaged_all = [(r["case"], d) for r in all_rows for d in r.get("salvaged") or []]
    if salvaged_all:
        print("\n  被拆开复用的候选：")
        for case_name, d in salvaged_all[:10]:
            print(f"    [{case_name}] {d['from']!r} → {d['to']!r}")
        if len(salvaged_all) > 10:
            print(f"    …… 另有 {len(salvaged_all) - 10} 条")

    # 被删掉的候选原文：没有这段，「候选被过滤 N 轮」只是个数字，
    # 没法判断该改 prompt（模型总在编）还是该补规则（模型没错、判据太严）。
    dropped_all = [(r["case"], d) for r in all_rows for d in r.get("dropped") or []]
    if dropped_all:
        print("\n  被删掉的候选（模型原本要发的话）：")
        for case_name, d in dropped_all[:10]:
            print(f"    [{case_name}] {d['text']!r}  ← {d['reason']}")
        if len(dropped_all) > 10:
            print(f"    …… 另有 {len(dropped_all) - 10} 条")
        by_reason: dict[str, int] = {}
        for _c, d in dropped_all:
            by_reason[d["reason"]] = by_reason.get(d["reason"], 0) + 1
        print("    按原因：" + "；".join(f"{k} {v} 条" for k, v in by_reason.items()))

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"summary": {"total": total, "passed": passed, "local": local,
                                    "punct": punct, "punished": punished, "empty": empty,
                                    "salvaged": salvaged, "failed_parse": failed_parse,
                                    "engine": ra._find_exe()},
                        "rows": all_rows}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"  报告已写入 {args.json_out}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
