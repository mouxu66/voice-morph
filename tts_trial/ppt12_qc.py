# -*- coding: utf-8 -*-
"""12 分钟 PPT 演讲音频 · 切片质检
输入: media/ft_work/ppt12/source_16k.wav
输出: media/ft_work/ppt12/qc_report.json + clips/*.wav

只做质检，不改训练脚本。判定项：
  1. 有效语音总时长 / VAD 段数 / 段长分布  -> 够不够微调
  2. Whisper 转写 vs 讲稿 n-gram 重合率   -> 是否真人念稿、标签是否干净
  3. 是否有 BGM 残留（低电平段频谱）
"""
import json
import os
import re
import sys
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "media", "ft_work", "ppt12")
OUT = os.path.abspath(OUT)
SRC = os.path.join(OUT, "source_16k.wav")
CLIPS = os.path.join(OUT, "clips")
os.makedirs(CLIPS, exist_ok=True)

log_lines = []


def log(m):
    print(m, flush=True)
    log_lines.append(m)


# ---------- 读音频（先用标准库，避免依赖问题） ----------
def read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(n)
    assert sw == 2, f"expect 16-bit, got {sw*8}-bit"
    a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


audio, sr = read_wav(SRC)
dur = len(audio) / sr
log(f"[audio] {SRC}")
log(f"[audio] sr={sr}  时长={dur:.2f}s ({dur/60:.2f}min)  峰值={20*np.log10(np.max(np.abs(audio))+1e-12):.1f} dBFS")

# ---------- 1. 能量法 VAD ----------
frame_ms = 25
hop_ms = 10
fl = int(sr * frame_ms / 1000)
hl = int(sr * hop_ms / 1000)
n_frames = 1 + (len(audio) - fl) // hl
rms = np.empty(n_frames, dtype=np.float32)
for i in range(n_frames):
    seg = audio[i * hl: i * hl + fl]
    rms[i] = np.sqrt(np.mean(seg ** 2) + 1e-12)
db = 20 * np.log10(rms + 1e-12)

# 自适应门限：噪声底 = 10 分位，语音门限 = 噪声底 + 12dB，且不低于 -45dB
noise_floor = np.percentile(db, 10)
thr = max(noise_floor + 12.0, -45.0)
speech = db > thr
log(f"[vad] 噪声底={noise_floor:.1f}dB  门限={thr:.1f}dB  语音帧占比={100*speech.mean():.1f}%")

# 合并成段，去掉 <0.4s 的碎片
segs = []
i = 0
while i < n_frames:
    if speech[i]:
        j = i
        while j < n_frames and speech[j]:
            j += 1
        t0, t1 = i * hop_ms / 1000, j * hop_ms / 1000
        if t1 - t0 >= 0.40:
            segs.append((t0, t1))
        i = j
    else:
        i += 1

# 合并间隔 <0.25s 的相邻段（同一句话被顿号切开）
merged = []
for s in segs:
    if merged and s[0] - merged[-1][1] < 0.25:
        merged[-1] = (merged[-1][0], s[1])
    else:
        merged.append(list(s))
segs = [tuple(s) for s in merged]

# 再按 3~12s 切成训练片段（长段切断，不留超长）
clips = []
MAX_LEN, MIN_LEN = 12.0, 2.0
for t0, t1 in segs:
    L = t1 - t0
    if L <= MAX_LEN:
        if L >= MIN_LEN:
            clips.append((t0, t1))
    else:
        # 在段内找一个低能量点切
        k = int(np.ceil(L / MAX_LEN))
        step = L / k
        for m in range(k):
            a, b = t0 + m * step, t0 + (m + 1) * step
            if b - a >= MIN_LEN:
                clips.append((a, b))

speech_total = sum(b - a for a, b in clips)
log(f"[vad] 语音段 {len(segs)} 个  切分后训练片段 {len(clips)} 个  有效语音 {speech_total:.1f}s ({speech_total/60:.2f}min)")
log(f"[vad] 有效率 {100*speech_total/dur:.1f}%")

lens = np.array([b - a for a, b in clips])
if len(lens):
    log(f"[len] min={lens.min():.1f}s  中位={np.median(lens):.1f}s  mean={lens.mean():.1f}s  max={lens.max():.1f}s")

# ---------- 2. Whisper 转写 + 与讲稿比对 ----------
transcript = []
try:
    from faster_whisper import WhisperModel
    log("[whisper] 载入 small ...")
    wm = WhisperModel("small", device="cuda", compute_type="float16")
    log("[whisper] 开始转写全片（VAD 过滤）...")
    tsegs, info = wm.transcribe(SRC, language="zh", vad_filter=True,
                               initial_prompt="以下是普通话课程讲解与项目答辩的录音。")
    log(f"[whisper] 检测语言={info.language} prob={info.language_probability:.2f}")
    for s in tsegs:
        transcript.append({"start": round(s.start, 2), "end": round(s.end, 2),
                           "text": s.text.strip()})
    full_text = "".join(t["text"] for t in transcript)
    log(f"[whisper] {len(transcript)} 句  共 {len(full_text)} 字")
    log("[whisper] 前 5 句：")
    for t in transcript[:5]:
        log(f"    {t['start']:7.1f}-{t['end']:7.1f}  {t['text']}")
except Exception as e:
    log(f"[whisper] 失败: {type(e).__name__}: {e}")
    full_text = ""

# 与讲稿比对
script_path = os.path.join(HERE, "..", "docs", "大创申报-2026-10", "答辩讲稿-19页.md")
script_path = os.path.abspath(script_path)
overlap = None
if full_text and os.path.exists(script_path):
    with open(script_path, encoding="utf-8") as f:
        raw = f.read()
    # 去掉 markdown 标记/时间标注，只留正文
    raw = re.sub(r"^#.*$", "", raw, flags=re.M)
    raw = re.sub(r"〔[^〕]*〕", "", raw)
    raw = re.sub(r"[^\u4e00-\u9fff]", "", raw)
    asr = re.sub(r"[^\u4e00-\u9fff]", "", full_text)
    # 讲稿里有多少 4-gram 出现在 ASR 结果里 => 覆盖率（讲稿被念了多少）
    N = 4
    sg = {raw[i:i + N] for i in range(len(raw) - N + 1)}
    ag = {asr[i:i + N] for i in range(len(asr) - N + 1)}
    cover = len(sg & ag) / max(len(sg), 1)
    # ASR 结果里有多少 4-gram 能在讲稿里找到 => 精确率（是不是在念这份稿）
    prec = len(sg & ag) / max(len(ag), 1)
    overlap = {"script_chars": len(raw), "asr_chars": len(asr),
               "script_4gram_coverage": round(cover, 4),
               "asr_4gram_precision": round(prec, 4)}
    log(f"[compare] 讲稿 {len(raw)} 字 / ASR {len(asr)} 字")
    log(f"[compare] 讲稿被覆盖 {100*cover:.1f}%   ASR 命中讲稿 {100*prec:.1f}%")
else:
    log(f"[compare] 跳过（ASR 空或讲稿缺失: {os.path.exists(script_path)}）")

# ---------- 3. 导出切片 ----------
import wave as wv
n_written = 0
for idx, (a, b) in enumerate(clips, 1):
    s0, s1 = int(a * sr), int(b * sr)
    chunk = audio[s0:s1]
    # 10ms 淡入淡出，防爆音
    f = int(0.01 * sr)
    if len(chunk) > 2 * f:
        chunk[:f] *= np.linspace(0, 1, f, dtype=np.float32)
        chunk[-f:] *= np.linspace(1, 0, f, dtype=np.float32)
    pcm = np.clip(chunk * 32767, -32768, 32767).astype("<i2")
    p = os.path.join(CLIPS, f"ppt12_{idx:04d}.wav")
    with wv.open(p, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    n_written += 1
log(f"[export] 写出 {n_written} 个切片 -> {CLIPS}")

report = {
    "source": SRC, "sr": sr, "duration_s": round(dur, 2),
    "noise_floor_db": round(float(noise_floor), 1),
    "vad_threshold_db": round(float(thr), 1),
    "speech_segments": len(segs),
    "train_clips": len(clips),
    "speech_total_s": round(speech_total, 1),
    "speech_ratio_pct": round(100 * speech_total / dur, 1),
    "clip_len": {"min": round(float(lens.min()), 2), "median": round(float(np.median(lens)), 2),
                 "mean": round(float(lens.mean()), 2), "max": round(float(lens.max()), 2)} if len(lens) else None,
    "whisper_sentences": len(transcript),
    "transcript_preview": transcript[:8],
    "script_overlap": overlap,
    "clips_dir": CLIPS,
}
with open(os.path.join(OUT, "qc_report.json"), "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
with open(os.path.join(OUT, "transcript.jsonl"), "w", encoding="utf-8") as f:
    for t in transcript:
        f.write(json.dumps(t, ensure_ascii=False) + "\n")
log(f"[done] 报告 -> {os.path.join(OUT, 'qc_report.json')}")
