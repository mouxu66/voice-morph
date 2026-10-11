# -*- coding: utf-8 -*-
"""构建 Qwen3-TTS 微调训练集（最终版）。

设计要点（每条都对应一个已踩过的坑）：
  1. 文本一律取**讲稿原文**，且只取"确实匹配上"的连续片段
     —— 不用整跨度，避免把没念的下一页内容写进标签（8 月致命伤的反面）。
  2. 切点从 ASR 段边界吸附到能量最低帧 —— 避免在字中间切断。
  3. 把关用**确定性指标**（字数/秒、RMS、时长），不拿 Whisper 回译当裁判
     —— 实测 Whisper 在 3~11s 孤立切片上会复读幻觉（"前端37000多航"×5），
        用它做门禁会误杀好样本。
  4. Whisper 回译仅作**报告**，不作过滤依据。
"""
import difflib
import json
import os
import re
import sys
import wave

import numpy as np
import zhconv

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "media", "ft_work", "ppt12")
SRC24 = os.path.join(OUT, "source_24k.wav")
SCRIPT_MD = os.path.join(ROOT, "docs", "大创申报-2026-10", "答辩讲稿-19页.md")
CLIPS = os.path.join(OUT, "clips24k")
os.makedirs(CLIPS, exist_ok=True)

LOG = []


def log(m):
    print(m, flush=True)
    LOG.append(m)


def clean(s):
    return re.sub(r"[^\u4e00-\u9fff0-9%.A-Za-z]", "", zhconv.convert(s or "", "zh-cn"))


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def write_wav(path, a, sr):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(a * 32767, -32768, 32767).astype("<i2").tobytes())


# ---------------- 讲稿 ----------------
md = open(SCRIPT_MD, encoding="utf-8").read().split("## 附 1")[0]
pages = re.split(r"^## (P\d+)\s*·\s*(.+)$", md, flags=re.M)
script_stream, script_page = "", []
for i in range(1, len(pages), 3):
    t = clean(re.sub(r"〔[^〕]*〕", "", pages[i + 2]))
    script_stream += t
    script_page += [pages[i]] * len(t)
log(f"[script] {len(script_stream)} 字 / {len(set(script_page))} 页")

# ---------------- 全片 ASR ----------------
asr = [json.loads(l) for l in open(os.path.join(OUT, "transcript.jsonl"), encoding="utf-8") if l.strip()]
asr_stream, spans = "", []
for seg in asr:
    t = clean(seg["text"])
    spans.append({"i0": len(asr_stream), "i1": len(asr_stream) + len(t),
                  "start": seg["start"], "end": seg["end"]})
    asr_stream += t
log(f"[asr] {len(asr_stream)} 字 / {len(asr)} 段")

# ---------------- 对齐：ASR 位置 -> 讲稿位置 ----------------
sm = difflib.SequenceMatcher(None, asr_stream, script_stream, autojunk=False)
opcodes = sm.get_opcodes()
pos = [0] * (len(asr_stream) + 1)
equal_blocks = []          # (asr_i0, asr_i1, script_j0) 仅 equal，用于取标签
for tag, i1, i2, j1, j2 in opcodes:
    if tag == "equal":
        for k in range(i2 - i1):
            pos[i1 + k] = j1 + k
        pos[i2] = j2
        equal_blocks.append((i1, i2, j1))
    elif tag == "replace":
        n = i2 - i1
        for k in range(n):
            pos[i1 + k] = j1 + round((j2 - j1) * k / max(n, 1))
        pos[i2] = j2
    elif tag == "insert":
        for k in range(i2 - i1):
            pos[i1 + k] = j1
        pos[i2] = j1
    else:
        pos[i2] = j2
for i in range(1, len(pos)):
    pos[i] = max(pos[i], pos[i - 1])
log(f"[map] 讲稿覆盖 {pos[-1]}/{len(script_stream)} = {100*pos[-1]/len(script_stream):.1f}%")
log(f"[map] equal 块 {len(equal_blocks)} 个，匹配 {sum(b[1]-b[0] for b in equal_blocks)} 字 "
    f"({100*sum(b[1]-b[0] for b in equal_blocks)/len(script_stream):.1f}% 讲稿)")

# ---------------- 能量包络（用于吸附切点） ----------------
audio, sr = read_wav(SRC24)
fl, hl = int(sr * 0.025), int(sr * 0.010)
nf = 1 + (len(audio) - fl) // hl
db = np.empty(nf, dtype=np.float32)
for i in range(nf):
    seg = audio[i * hl:i * hl + fl]
    db[i] = 10 * np.log10(np.mean(seg ** 2) + 1e-12)
floor = float(np.percentile(db, 5))


def snap(t, search=0.30):
    i0 = max(0, int((t - search) * sr / hl))
    i1 = min(nf, int((t + search) * sr / hl) + 1)
    if i1 <= i0:
        return t
    return (i0 + int(np.argmin(db[i0:i1]))) * hl / sr


# ---------------- 合并 ASR 段 -> 训练片段 ----------------
MIN_D, MAX_D, GAP = 3.0, 11.0, 0.7
groups, cur = [], None
for sp in spans:
    if sp["i1"] <= sp["i0"]:
        continue
    if cur is None:
        cur = dict(sp)
        continue
    if sp["start"] - cur["end"] <= GAP and sp["end"] - cur["start"] <= MAX_D:
        cur["i1"] = sp["i1"]
        cur["end"] = sp["end"]
    else:
        groups.append(cur)
        cur = dict(sp)
if cur:
    groups.append(cur)


def label_for(i0, i1):
    """取该 ASR 区间内**匹配上**的讲稿片段（合并相邻），不取整跨度。"""
    rngs = []
    for (bi0, bi1, bj0) in equal_blocks:
        lo, hi = max(i0, bi0), min(i1, bi1)
        if hi > lo:
            off = lo - bi0
            rngs.append([bj0 + off, bj0 + off + (hi - lo)])
    if not rngs:
        return "", 0
    rngs.sort()
    merged = [rngs[0]]
    for r in rngs[1:]:
        if r[0] <= merged[-1][1] + 3:      # 间隔 <=3 字算连续
            merged[-1][1] = max(merged[-1][1], r[1])
        else:
            merged.append(r)
    txt = "".join(script_stream[a:b] for a, b in merged)
    matched = sum(b - a for a, b in merged)
    return txt, matched


items = []
for g in groups:
    d = g["end"] - g["start"]
    if d < 1.5:
        continue
    txt, matched = label_for(g["i0"], g["i1"])
    if len(txt) < 6:
        continue
    n_asr = g["i1"] - g["i0"]
    items.append({
        "start": g["start"], "end": g["end"], "text": txt,
        "matched": matched, "hit": matched / max(n_asr, 1),
        "n_asr": n_asr,
        "_pages": sorted({script_page[c] for c in range(g["i0"], min(g["i1"], len(script_page)))}),
    })
log(f"[group] 合并后候选 {len(items)} 条")

# ---------------- 时长合理性（确定性把关） ----------------
final = []
for it in items:
    d = it["end"] - it["start"]
    cps = len(it["text"]) / max(d, 0.01)          # 字/秒
    if not (2.2 <= cps <= 9.0):
        log(f"[drop] 字速 {cps:.1f}/s 异常  {d:.1f}s  {it['text'][:30]}")
        continue
    if it["hit"] < 0.45:
        log(f"[drop] 命中率 {it['hit']:.2f} 低  {d:.1f}s  {it['text'][:30]}")
        continue
    final.append(it)
log(f"[filter] 保留 {len(final)} 条")

# ---------------- 导出 ----------------
results = []
for i, it in enumerate(final, 1):
    t0 = snap(it["start"])
    t1 = snap(it["end"])
    if t1 - t0 < 1.5:
        t1 = t0 + max(1.5, t1 - t0)
    s0, s1 = int(t0 * sr), min(int(t1 * sr), len(audio))
    if s1 - s0 < int(1.4 * sr):
        continue
    chunk = audio[s0:s1].copy()
    f = int(0.005 * sr)
    if len(chunk) > 2 * f:
        chunk[:f] *= np.linspace(0, 1, f, dtype=np.float32)
        chunk[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
    rms = float(np.sqrt(np.mean(chunk ** 2) + 1e-12))
    if 20 * np.log10(rms) < -45:      # 太安静，多半是切错了
        log(f"[drop] RMS {20*np.log10(rms):.1f}dB 过低")
        continue
    p = os.path.join(CLIPS, f"ppt12_{i:04d}.wav")
    write_wav(p, chunk, sr)
    results.append({"idx": i, "path": p, "dur": round((s1 - s0) / sr, 2),
                    "text": it["text"], "hit": round(it["hit"], 3),
                    "rms_db": round(20 * np.log10(rms), 1), "pages": it["_pages"]})

# 清掉不在最终列表里的切片
keep = {os.path.basename(r["path"]) for r in results}
for f in os.listdir(CLIPS):
    if f.endswith(".wav") and f not in keep:
        os.remove(os.path.join(CLIPS, f))

durs = np.array([r["dur"] for r in results])
tot = durs.sum()
log(f"\n[export] {len(results)} 条  总时长 {tot:.1f}s ({tot/60:.2f}min)")
log(f"[export] 时长 min={durs.min():.1f} 中位={np.median(durs):.1f} 均={durs.mean():.1f} max={durs.max():.1f}")
log(f"[export] 命中率 均={np.mean([r['hit'] for r in results]):.2f}")
log(f"[export] 字速 均={np.mean([len(r['text'])/r['dur'] for r in results]):.1f} 字/秒")

# ---------------- 参考音 ----------------
cand = [r for r in results if 6 <= r["dur"] <= 11]
ref = max(cand, key=lambda x: x["hit"]) if cand else max(results, key=lambda x: x["hit"])
ref_path = os.path.join(OUT, "ref_24k.wav")
ra, _ = read_wav(ref["path"])
write_wav(ref_path, ra, sr)
log(f"\n[ref] {os.path.basename(ref['path'])}  {ref['dur']}s  hit={ref['hit']}")
log(f"[ref] 文本: {ref['text']}")

# ---------------- 回译报告（不作门禁） ----------------
try:
    from faster_whisper import WhisperModel
    wm = WhisperModel("small", device="cuda", compute_type="float16")
    log("\n[back] 回译抽样报告（仅供人工参考，不作过滤）...")
    for r in results[:8]:
        ts, _ = wm.transcribe(r["path"], language="zh", vad_filter=False)
        r["back"] = clean("".join(t.text for t in ts))
        r["back_len_ratio"] = round(len(r["back"]) / max(len(clean(r["text"])), 1), 2)
        log(f"  #{r['idx']:3d} {r['dur']:4.1f}s lr={r['back_len_ratio']:.2f}")
        log(f"      标签: {r['text']}")
        log(f"      回译: {r['back']}")
except Exception as e:
    log(f"[back] 跳过: {e}")

# ---------------- 落盘 ----------------
out_jsonl = os.path.join(OUT, "train_final.jsonl")
with open(out_jsonl, "w", encoding="utf-8") as f:
    for r in results:
        f.write(json.dumps({"audio": r["path"].replace("\\", "/"), "text": r["text"],
                            "ref_audio": ref_path.replace("\\", "/")}, ensure_ascii=False) + "\n")
log(f"\n[done] 训练集 {len(results)} 条 -> {out_jsonl}")

with open(os.path.join(OUT, "build_log.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(LOG))
with open(os.path.join(OUT, "dataset_report.json"), "w", encoding="utf-8") as f:
    json.dump({"clips": len(results), "total_s": round(tot, 1),
               "ref": {"path": ref_path, "dur": ref["dur"], "text": ref["text"]},
               "items": [{k: v for k, v in r.items() if k != "back"} for r in results]},
              f, ensure_ascii=False, indent=2)
