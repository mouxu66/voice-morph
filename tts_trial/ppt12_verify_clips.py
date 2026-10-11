# -*- coding: utf-8 -*-
"""重建标签（补齐对齐空洞）+ 逐条回译验证标签质量。

方法：
  1. 用 difflib opcodes 建"ASR 字符 -> 讲稿字符"的单调位置函数 pos[]，
     对 replace/delete 按比例分配 —— 这样段跨度内被对齐丢掉的讲稿字会被补回来。
  2. 逐条把导出的切片重新 ASR，与标签算字符重合度（口径同 tools/voice_qc.py）。
  3. 重合度低于阈值的丢掉，不写进训练集。
"""
import difflib
import json
import os
import re
import sys
import wave

import numpy as np
import zhconv   # Whisper 常吐繁体，必须转简再比，否则重合度全错

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "media", "ft_work", "ppt12")
SRC24 = os.path.join(OUT, "source_24k.wav")
SCRIPT_MD = os.path.join(ROOT, "docs", "大创申报-2026-10", "答辩讲稿-19页.md")
CLIPS = os.path.join(OUT, "clips24k")

log_lines = []


def log(m):
    print(m, flush=True)
    log_lines.append(m)


def clean(s):
    """统一转简体再抽有效字符 —— Whisper 输出常是繁体，不转会虚低。"""
    s = zhconv.convert(s or "", "zh-cn")
    return re.sub(r"[^\u4e00-\u9fff0-9%.A-Za-z]", "", s)


def cer(ref, hyp):
    """字错率 = 编辑距离 / 参考长度（0 最好）。"""
    ref, hyp = clean(ref), clean(hyp)
    if not ref:
        return 1.0
    d = np.zeros((len(ref) + 1, len(hyp) + 1), dtype=np.int32)
    d[:, 0] = np.arange(len(ref) + 1)
    d[0, :] = np.arange(len(hyp) + 1)
    for i in range(1, len(ref) + 1):
        for j in range(1, len(hyp) + 1):
            d[i, j] = min(d[i - 1, j] + 1, d[i, j - 1] + 1,
                          d[i - 1, j - 1] + (ref[i - 1] != hyp[j - 1]))
    return float(d[len(ref), len(hyp)]) / len(ref)


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


# ---------- 讲稿 ----------
md = open(SCRIPT_MD, encoding="utf-8").read().split("## 附 1")[0]
pages = re.split(r"^## (P\d+)\s*·\s*(.+)$", md, flags=re.M)
script_stream = ""
script_page = []
for i in range(1, len(pages), 3):
    t = re.sub(r"〔[^〕]*〕", "", pages[i + 2])
    t = clean(t)
    script_stream += t
    script_page += [pages[i]] * len(t)
log(f"[script] {len(script_stream)} 字, {len(set(script_page))} 页")

# ---------- ASR ----------
asr = [json.loads(l) for l in open(os.path.join(OUT, "transcript.jsonl"), encoding="utf-8") if l.strip()]
asr_stream = ""
bounds = []
for seg in asr:
    t = clean(seg["text"])
    bounds.append((len(asr_stream), len(asr_stream) + len(t)))
    asr_stream += t
log(f"[asr] {len(asr_stream)} 字, {len(asr)} 段")

# ---------- opcode -> 单调位置映射 ----------
sm = difflib.SequenceMatcher(None, asr_stream, script_stream, autojunk=False)
pos = [0] * (len(asr_stream) + 1)
for tag, i1, i2, j1, j2 in sm.get_opcodes():
    if tag == "equal":
        for k in range(i2 - i1):
            pos[i1 + k] = j1 + k
        pos[i2] = j2
    elif tag == "replace":
        n = i2 - i1
        for k in range(n):
            pos[i1 + k] = j1 + round((j2 - j1) * k / max(n, 1))
        pos[i2] = j2
    elif tag == "insert":       # ASR 多出来的字（多半是识别错误）
        for k in range(i2 - i1):
            pos[i1 + k] = j1
        pos[i2] = j1
    else:                        # delete: 讲稿有、ASR 没有 -> 挂到边界
        pos[i2] = j2
# 保证单调
for i in range(1, len(pos)):
    if pos[i] < pos[i - 1]:
        pos[i] = pos[i - 1]
log(f"[map] 讲稿覆盖 {pos[-1]}/{len(script_stream)} = {100*pos[-1]/len(script_stream):.1f}%")

# 段级标签（用 pos 补洞）
segs = []
for i, seg in enumerate(asr):
    a0, a1 = bounds[i]
    if a1 <= a0:
        continue
    s0, s1 = pos[a0], pos[a1]
    txt = script_stream[s0:s1]
    if not txt:
        continue
    segs.append({"start": seg["start"], "end": seg["end"], "text": txt,
                 "s0": s0, "s1": s1, "asr": clean(seg["text"])})
log(f"[segs] 有效段 {len(segs)}")

# 段级自检：标签与 ASR 的重合度
def overlap(a, b):
    A, B = set(a.replace("，", "")), set(b.replace("，", ""))
    return len(A & B) / max(len(A | B), 1)


ov = [overlap(s["asr"], s["text"]) for s in segs]
log(f"[segs] 段级标签vs ASR 重合度: 均={np.mean(ov):.3f} 中位={np.median(ov):.3f} "
    f"<0.5 的段={sum(1 for x in ov if x<0.5)}")

# ---------- 合并成 3~11s 片段 ----------
MIN_D, MAX_D, GAP = 3.0, 11.0, 0.8
groups, cur = [], None
for s in segs:
    if cur is None:
        cur = dict(s)
        continue
    if s["start"] - cur["end"] <= GAP and s["end"] - cur["start"] <= MAX_D:
        cur["end"] = s["end"]
        cur["text"] = script_stream[cur["s0"]:s["s1"]]   # 连续跨度，天然补齐
        cur["s1"] = s["s1"]
        cur["asr"] += s["asr"]
    else:
        groups.append(cur)
        cur = dict(s)
if cur:
    groups.append(cur)

final = []
for g in groups:
    d = g["end"] - g["start"]
    if d >= MIN_D and len(g["text"]) >= 8:
        final.append(g)
    elif 1.6 <= d < MIN_D and len(g["text"]) >= 6:
        final.append(g)
# 超长的再切
split = []
for g in final:
    d = g["end"] - g["start"]
    if d <= MAX_D:
        split.append(g)
        continue
    k = int(np.ceil(d / MAX_D))
    for m in range(k):
        t0, t1 = g["start"] + m * d / k, g["start"] + (m + 1) * d / k
        a, b = g["s0"] + int((g["s1"] - g["s0"]) * m / k), g["s0"] + int((g["s1"] - g["s0"]) * (m + 1) / k)
        split.append({"start": t0, "end": t1, "text": script_stream[a:b],
                      "s0": a, "s1": b, "asr": g["asr"]})
final = [g for g in split if len(g["text"]) >= 6 and g["end"] - g["start"] >= 1.5]
log(f"[final] 片段 {len(final)} 条")

# ---------- 写出切片 + 回译验证 ----------
audio, sr = read_wav(SRC24)
os.makedirs(CLIPS, exist_ok=True)
for f in os.listdir(CLIPS):
    if f.endswith(".wav"):
        os.remove(os.path.join(CLIPS, f))

from faster_whisper import WhisperModel
log("[asr] 载入 faster-whisper small ...")
wm = WhisperModel("small", device="cuda", compute_type="float16")

results = []
for i, g in enumerate(final, 1):
    s0, s1 = int(g["start"] * sr), min(int(g["end"] * sr), len(audio))
    chunk = audio[s0:s1].copy()
    f = int(0.005 * sr)
    if len(chunk) > 2 * f:
        chunk[:f] *= np.linspace(0, 1, f, dtype=np.float32)
        chunk[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
    p = os.path.join(CLIPS, f"ppt12_{i:04d}.wav")
    pcm = np.clip(chunk * 32767, -32768, 32767).astype("<i2")
    with wave.open(p, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    results.append({"idx": i, "path": p, "dur": round((s1 - s0) / sr, 2), "text": g["text"]})

log(f"[asr] 开始逐条回译并二次对齐 {len(results)} 条 ...")
for r in results:
    ts, _ = wm.transcribe(r["path"], language="zh", vad_filter=False)
    back = clean("".join(t.text for t in ts))
    r["back_raw"] = back
    # ---- 二次对齐：用这条切片自己的 ASR 反推标签在讲稿里的真实跨度 ----
    #     边界来自 ASR，内容仍取讲稿原文 -> 不会把"没念的话"写进标签
    if back:
        sm2 = difflib.SequenceMatcher(None, back, script_stream, autojunk=False)
        blk = [b for b in sm2.get_matching_blocks() if b.size >= 3]
        if blk:
            r["s0"], r["s1"] = blk[0].b, blk[-1].b + blk[-1].size
            r["text2"] = script_stream[r["s0"]:r["s1"]]
            r["matched"] = sum(b.size for b in blk)
        else:
            r["text2"] = ""
            r["matched"] = 0
    else:
        r["text2"], r["matched"] = "", 0
    # 最终标签用二次对齐结果；它为空才退回原标签
    r["label"] = r["text2"] or r["text"]
    r["refine_ratio"] = round(len(clean(r["label"])) / max(len(clean(r["text"])), 1), 2)
    r["ov"] = round(overlap(back, r["label"]), 3)
    r["cer"] = round(cer(r["label"], back), 3)
    r["len_ratio"] = round(len(back) / max(len(clean(r["label"])), 1), 2)
    r["hit"] = round(r["matched"] / max(len(back), 1), 3)

ovs = np.array([r["ov"] for r in results])
cers = np.array([r["cer"] for r in results])
hits = np.array([r["hit"] for r in results])
log(f"\n[verify] 二次对齐命中率(ASR字落在讲稿上): 均={hits.mean():.3f} 中位={np.median(hits):.3f}")
log(f"[verify] 回译重合度 (集合IoU): 均={ovs.mean():.3f} 中位={np.median(ovs):.3f}")
log(f"[verify] 字错率 CER(编辑距离):  均={cers.mean():.3f} 中位={np.median(cers):.3f}")
log(f"[verify] 标签长度变化比(二次/原): 均={np.mean([r['refine_ratio'] for r in results]):.2f}")
for th in (0.2, 0.3, 0.4, 0.5, 0.6):
    m = cers <= th
    log(f"[verify] CER <= {th}: {int(m.sum())} 条 / {sum(r['dur'] for r in results if r['cer']<=th):.1f}s")

log("\n[verify] 最差的 10 条：(CER 高)")
for r in sorted(results, key=lambda x: -x["cer"])[:10]:
    log(f"  #{r['idx']:3d} cer={r['cer']:.2f} hit={r['hit']:.2f} ref={r['refine_ratio']:.2f} {r['dur']:.1f}s")
    log(f"      原标签: {r['text']}")
    log(f"      二次后: {r['label']}")
    log(f"      回译  : {r['back_raw']}")

TH_CER = 0.35          # 字错率上限：Whisper 自身也有错字，0.35 是"标签基本可信"的门槛
good = [r for r in results if r["cer"] <= TH_CER and r["hit"] >= 0.5 and r["len_ratio"] >= 0.6]
bad = [r for r in results if r not in good]
tot_good = sum(r["dur"] for r in good)
log(f"\n[keep] CER<={TH_CER} 且 hit>=0.5 且 长度比>=0.6:  保留 {len(good)} 条 / {tot_good:.1f}s "
    f"({tot_good/60:.2f}min)   丢弃 {len(bad)} 条")
log(f"[keep] 保留段 CER 均={np.mean([r['cer'] for r in good]):.3f}  "
    f"中位={np.median([r['cer'] for r in good]):.3f}")
for r in bad:
    if os.path.exists(r["path"]):
        os.remove(r["path"])

# 选参考音（从保留的里挑：覆盖干净、6~11s、CER 低）
cand = [r for r in good if 6 <= r["dur"] <= 11]
ref = min(cand, key=lambda x: x["cer"]) if cand else min(good, key=lambda x: x["cer"])
ref_path = os.path.join(OUT, "ref_24k.wav")
ra, _ = read_wav(ref["path"])
with wave.open(ref_path, "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(np.clip(ra * 32767, -32768, 32767).astype("<i2").tobytes())
log(f"[ref] 参考音 = {os.path.basename(ref['path'])}  {ref['dur']}s  cer={ref['cer']}")
log(f"[ref] 文本: {ref['label']}")

# 写最终训练集（文本用二次对齐后的讲稿原文）
out_jsonl = os.path.join(OUT, "train_final.jsonl")
with open(out_jsonl, "w", encoding="utf-8") as f:
    for r in good:
        f.write(json.dumps({"audio": r["path"].replace("\\", "/"),
                            "text": r["label"],
                            "ref_audio": ref_path.replace("\\", "/")},
                           ensure_ascii=False) + "\n")
log(f"[done] 最终训练集 {len(good)} 条 -> {out_jsonl}")

with open(os.path.join(OUT, "verify_log.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(log_lines))
with open(os.path.join(OUT, "verify_detail.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
