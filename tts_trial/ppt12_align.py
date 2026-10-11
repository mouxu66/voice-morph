# -*- coding: utf-8 -*-
"""用《答辩讲稿-19页.md》给 Whisper 转写纠错，生成干净训练集。

背景（8 月袋鼠失败教训）：Whisper 转写真人普通话仍有约 15-20% 错字
（本次实测：顾客人群→录课人群、用丧→用嗓、生意银行→声音银行、零端客龙→云端克隆），
LoRA 会把这些错字当成"正确的音-字映射"学进去 → 内容崩坏。

对策：本次有本人讲稿（近乎零噪声的金标准），用 difflib 把 ASR 流与讲稿流做
长程对齐，取讲稿原文当标签，ASR 只用来定时间边界。
"""
import difflib
import json
import os
import re
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "media", "ft_work", "ppt12")
SCRIPT_MD = os.path.join(ROOT, "docs", "大创申报-2026-10", "答辩讲稿-19页.md")

log_lines = []


def log(m):
    print(m, flush=True)
    log_lines.append(m)


# ---------------- 1. 解析讲稿 ----------------
with open(SCRIPT_MD, encoding="utf-8") as f:
    md = f.read()

# 只要正文（附 1 之前）
body = md.split("## 附 1")[0]

# 按页切：## P<n> · 标题
pages = re.split(r"^## (P\d+)\s*·\s*(.+)$", body, flags=re.M)
# pages = [前言, 'P1', '封面（约 15 秒）', 正文1, 'P2', ..., 正文N]
page_items = []
for i in range(1, len(pages), 3):
    pid, title, txt = pages[i], pages[i + 1], pages[i + 2]
    txt = re.sub(r"〔[^〕]*〕", "", txt)          # 去掉〔姓名〕占位
    txt = re.sub(r"[*_>`#]", "", txt)             # 去 markdown 标记
    txt = re.sub(r"\s+", "", txt)                 # 去所有空白（中文正文无需空格）
    txt = re.sub(r"[^\u4e00-\u9fff0-9%.A-Za-z]", "", txt)  # 只留有效字符
    if txt:
        page_items.append({"page": pid, "title": title.strip(), "text": txt})
log(f"[script] 解析出 {len(page_items)} 页")

# 拼成一条流 + 映射回 (page, offset)
script_stream = ""
script_map = []   # 每个字符 -> (page_idx, char_in_page)
for pi, it in enumerate(page_items):
    for ci, ch in enumerate(it["text"]):
        script_map.append((pi, ci))
    script_stream += it["text"]
log(f"[script] 正文 {len(script_stream)} 字")

# ---------------- 2. 读 ASR 转写 ----------------
tpath = os.path.join(OUT, "transcript.jsonl")
asr = [json.loads(l) for l in open(tpath, encoding="utf-8") if l.strip()]
log(f"[asr] {len(asr)} 段")


def clean(s):
    return re.sub(r"[^\u4e00-\u9fff0-9%.A-Za-z]", "", s or "")


asr_stream = ""
asr_map = []  # 每字符 -> asr 段 index
for si, seg in enumerate(asr):
    t = clean(seg["text"])
    seg["_clean"] = t
    seg["_start"] = len(asr_stream)
    seg["_end"] = len(asr_stream) + len(t)
    for _ in t:
        asr_map.append(si)
    asr_stream += t
log(f"[asr] 流长 {len(asr_stream)} 字")

# ---------------- 3. 长程对齐 ----------------
sm = difflib.SequenceMatcher(None, asr_stream, script_stream, autojunk=False)
blocks = sm.get_matching_blocks()
# 只保留长度 >=4 的匹配块（噪声块无信息）
blocks = [b for b in blocks if b.size >= 4]
log(f"[align] 匹配块 {len(blocks)} 个，总匹配 {sum(b.size for b in blocks)} 字")
log(f"[align] 讲稿被覆盖 {100*sum(b.size for b in blocks)/len(script_stream):.1f}%  "
    f"ASR 命中 {100*sum(b.size for b in blocks)/len(asr_stream):.1f}%")

# 对每个 ASR 段，找它覆盖的讲稿区间
rows = []
for si, seg in enumerate(asr):
    a0, a1 = seg["_start"], seg["_end"]
    if a1 <= a0:
        continue
    hits = []
    for b in blocks:
        # 块与段有重叠
        lo, hi = max(a0, b.a), min(a1, b.a + b.size)
        if hi > lo:
            off = lo - b.a
            hits.append((b.b + off, b.b + off + (hi - lo)))
    if not hits:
        rows.append({**seg, "script_text": "", "cover": 0.0, "status": "no_match"})
        continue
    hits.sort()
    # 合并相邻/重叠区间
    merged = [list(hits[0])]
    for h in hits[1:]:
        if h[0] <= merged[-1][1] + 2:
            merged[-1][1] = max(merged[-1][1], h[1])
        else:
            merged.append(list(h))
    s0, s1 = merged[0][0], merged[-1][1]
    stext = script_stream[s0:s1]
    # 覆盖率 = 匹配字符数 / ASR 段字符数
    matched = sum(h[1] - h[0] for h in merged)
    cover = matched / max(a1 - a0, 1)
    pages_hit = sorted({page_items[script_map[c][0]]["page"]
                        for c in range(s0, min(s1, len(script_map)))})
    rows.append({**seg, "script_text": stext, "cover": round(cover, 3),
                 "pages": pages_hit,
                 "status": "ok" if cover >= 0.5 else "weak"})

n_ok = sum(1 for r in rows if r["status"] == "ok")
n_weak = sum(1 for r in rows if r["status"] == "weak")
n_no = sum(1 for r in rows if r["status"] == "no_match")
log(f"[align] 段级：ok={n_ok}  weak={n_weak}  no_match={n_no}")

# ---------------- 4. 导出对齐结果 ----------------
with open(os.path.join(OUT, "aligned.jsonl"), "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps({k: v for k, v in r.items() if not k.startswith("_")},
                           ensure_ascii=False) + "\n")

log("\n[样例] 前 12 段对齐结果（ASR -> 讲稿）")
for r in rows[:12]:
    log(f"  {r['start']:7.1f}-{r['end']:7.1f}s  cover={r['cover']:.2f} {r['status']:8s}")
    log(f"      ASR : {r['_clean']}")
    log(f"      讲稿: {r['script_text']}")

# 时长统计
dur_speech = sum(r["end"] - r["start"] for r in rows)
dur_ok = sum(r["end"] - r["start"] for r in rows if r["status"] == "ok")
log(f"\n[stat] 有转写语音总时长 {dur_speech:.1f}s ({dur_speech/60:.2f}min)")
log(f"[stat] 其中对齐良好 {dur_ok:.1f}s ({dur_ok/60:.2f}min)")

with open(os.path.join(OUT, "align_log.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(log_lines))
