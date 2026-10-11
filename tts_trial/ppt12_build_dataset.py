# -*- coding: utf-8 -*-
"""从对齐结果构建 Qwen3-TTS 微调训练集。

要点：
  1. 文本 = **讲稿原文**（不是 Whisper 输出）→ 标签近乎零噪声
  2. 切点 = Whisper 时间戳 + 静音吸附 → 不在字中间切
  3. 输出 24kHz 单声道（tokenizer input_sample_rate=24000）
  4. 顺带选一条干净参考音（声纹锚点 + A 臂零样本共用）
"""
import json
import os
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "media", "ft_work", "ppt12")
SRC = os.path.join(OUT, "source_24k.wav")
CLIPS = os.path.join(OUT, "clips24k")
os.makedirs(CLIPS, exist_ok=True)

log_lines = []


def log(m):
    print(m, flush=True)
    log_lines.append(m)


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def write_wav(path, a, sr):
    pcm = np.clip(a * 32767, -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


audio, sr = read_wav(SRC)
dur = len(audio) / sr
log(f"[src] {SRC}  sr={sr}  {dur:.1f}s")

# 削波检查
peak = np.max(np.abs(audio))
n_clip = int(np.sum(np.abs(audio) >= 0.999))
log(f"[src] 峰值={20*np.log10(peak+1e-12):.2f} dBFS  满幅样本={n_clip} ({100*n_clip/len(audio):.4f}%)")

# 帧能量（用于静音吸附）
fl, hl = int(sr * 0.025), int(sr * 0.010)
nf = 1 + (len(audio) - fl) // hl
db = np.empty(nf, dtype=np.float32)
for i in range(nf):
    seg = audio[i * hl: i * hl + fl]
    db[i] = 10 * np.log10(np.mean(seg ** 2) + 1e-12)
floor = float(np.percentile(db, 5))
log(f"[vad] 噪声底={floor:.1f} dB")


def snap(t, search=0.25, prefer_low=True):
    """把时间点吸附到附近能量最低的帧（尽量落在停顿里）。"""
    i0 = max(0, int((t - search) * sr / hl))
    i1 = min(nf, int((t + search) * sr / hl) + 1)
    if i1 <= i0:
        return t
    k = i0 + int(np.argmin(db[i0:i1])) if prefer_low else i0 + int(np.argmax(db[i0:i1]))
    return k * hl / sr


# ---------- 读对齐结果 ----------
rows = [json.loads(l) for l in open(os.path.join(OUT, "aligned.jsonl"), encoding="utf-8") if l.strip()]

# 只保留可用的、且讲稿文本非空
cand = [r for r in rows if r.get("script_text") and r.get("status") in ("ok", "weak") and r["cover"] >= 0.5]
log(f"[rows] 全部 {len(rows)}  可用 {len(cand)}")

# ---------- 合并成 3~11s 的片段 ----------
MIN_D, MAX_D = 3.0, 11.0
groups = []
cur = None
for r in cand:
    if cur is None:
        cur = {"t0": r["start"], "t1": r["end"], "text": r["script_text"],
               "cov": [r["cover"]], "pages": list(r.get("pages") or [])}
        continue
    gap = r["start"] - cur["t1"]
    newd = r["end"] - cur["t0"]
    if gap <= 0.8 and newd <= MAX_D:
        cur["t1"] = r["end"]
        cur["text"] += r["script_text"]
        cur["cov"].append(r["cover"])
        for p in (r.get("pages") or []):
            if p not in cur["pages"]:
                cur["pages"].append(p)
    else:
        groups.append(cur)
        cur = {"t0": r["start"], "t1": r["end"], "text": r["script_text"],
               "cov": [r["cover"]], "pages": list(r.get("pages") or [])}
if cur:
    groups.append(cur)

# 过滤太短的
kept = []
for g in groups:
    d = g["t1"] - g["t0"]
    if d >= MIN_D and len(g["text"]) >= 8:
        kept.append(g)
    elif d >= 1.6 and d < MIN_D and len(g["text"]) >= 6:
        g["_short"] = True
        kept.append(g)

log(f"[merge] 合并后 {len(groups)} 组，保留 {len(kept)} 组")

# 长组再切（> MAX_D 的）
final = []
for g in kept:
    d = g["t1"] - g["t0"]
    if d <= MAX_D:
        final.append(g)
        continue
    k = int(np.ceil(d / MAX_D))
    step = d / k
    for m in range(k):
        t0, t1 = g["t0"] + m * step, g["t0"] + (m + 1) * step
        frac0, frac1 = m / k, (m + 1) / k
        c0 = int(len(g["text"]) * frac0)
        c1 = int(len(g["text"]) * frac1)
        final.append({"t0": t0, "t1": t1, "text": g["text"][c0:c1],
                      "cov": g["cov"], "pages": g["pages"]})
final = [g for g in final if len(g["text"]) >= 6 and (g["t1"] - g["t0"]) >= 1.5]
log(f"[final] 训练片段 {len(final)} 条")

# ---------- 导出 ----------
items = []
for i, g in enumerate(final, 1):
    t0 = snap(g["t0"], prefer_low=True)
    t1 = snap(g["t1"], prefer_low=True)
    if t1 - t0 < 1.2:
        t1 = t0 + 1.2
    s0, s1 = int(t0 * sr), min(int(t1 * sr), len(audio))
    chunk = audio[s0:s1].copy()
    # 5ms 淡入淡出
    f = int(0.005 * sr)
    if len(chunk) > 2 * f:
        chunk[:f] *= np.linspace(0, 1, f, dtype=np.float32)
        chunk[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
    p = os.path.join(CLIPS, f"ppt12_{i:04d}.wav")
    write_wav(p, chunk, sr)
    items.append({
        "audio": p.replace("\\", "/"),
        "text": g["text"],
        "ref_audio": "REF_PLACEHOLDER",
        "_dur": round(s1 / sr - s0 / sr, 2),
        "_cover": round(float(np.mean(g["cov"])), 3),
        "_pages": g["pages"],
    })

durs = np.array([it["_dur"] for it in items])
covs = np.array([it["_cover"] for it in items])
total = durs.sum()
log(f"[export] {len(items)} 条  总时长 {total:.1f}s ({total/60:.2f}min)")
log(f"[export] 片段时长 min={durs.min():.1f} 中位={np.median(durs):.1f} 均={durs.mean():.1f} max={durs.max():.1f}")
log(f"[export] 对齐覆盖度 均={covs.mean():.2f} min={covs.min():.2f}")

# ---------- 选参考音 ----------
# 挑一条：覆盖度高、时长 6~11s、位于讲稿主体（非开头问候）
best = None
for it in items:
    if 6.0 <= it["_dur"] <= 11.0 and it["_cover"] >= 0.75:
        if best is None or it["_dur"] > best["_dur"]:
            best = it
if best is None:
    best = max(items, key=lambda x: x["_cover"] * min(x["_dur"], 11))
log(f"\n[ref] 选定参考音: {os.path.basename(best['audio'])}  {best['_dur']}s  cover={best['_cover']}")
log(f"[ref] 文本: {best['text'][:80]}")

ref_path = os.path.join(OUT, "ref_24k.wav")
s0 = 0
a2, _ = read_wav(best["audio"])
write_wav(ref_path, a2, sr)
log(f"[ref] -> {ref_path}")

# ---------- 写 JSONL ----------
for it in items:
    it["ref_audio"] = ref_path.replace("\\", "/")
out_jsonl = os.path.join(OUT, "train_raw.jsonl")
with open(out_jsonl, "w", encoding="utf-8") as f:
    for it in items:
        f.write(json.dumps({k: v for k, v in it.items() if not k.startswith("_")},
                           ensure_ascii=False) + "\n")
log(f"[done] 训练清单 -> {out_jsonl}")

with open(os.path.join(OUT, "dataset_log.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(log_lines))
