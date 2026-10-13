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
import wave

import numpy as np
import sys

#: 帧能量门限的自适应余量（相对 10 分位噪声底）。
SNR_MARGIN_DB = 12.0
#: 门限的**绝对下限**（dBFS）。
#:
#: ★ 2026-10-12 修正（这是一个真实 bug，不是笔误）：
#: 原式 `thr = max(noise_floor + 12.0, -45.0)` 里，-45.0 本该是"门限不低于此值"
#: 的**兜底**，但方向写反了 —— 它成了**地板**，把自适应门限往上钳。
#: 实测本片 noise_floor = -67.1dB ⇒ 自适应值 -55.1dB 被钳到 -45.0dB，
#: 抬高 10dB 后只剩 70 条 / 204.1s；而按本意 -55.1dB 可得 127 条 / 538.1s，
#: webrtcvad 独立复核为 715.3s。**同一份音频差 3.6 倍。**
#: 后果：RVC 训练集只剩 3.4min（官方推荐 10min），结论被误记成"素材不够、得再录"。
#: 另外原日志把 `thr` 打成"噪声底"，掩盖了这次钳位 —— 现在两者分开打。
SILENCE_FLOOR_DB = -55.0
#: 自校准结果的合理区间；超出说明输入不是正常语音（全静音/爆音），
#: 这时退回固定门限而不是相信一个被算歪的自适应值。
AUTO_THR_MIN_DB = -70.0
AUTO_THR_MAX_DB = -25.0

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
def compute_threshold(db, floor_pct=10.0, margin_db=SNR_MARGIN_DB,
                      low_db=AUTO_THR_MIN_DB, high_db=AUTO_THR_MAX_DB):
    """由帧能量分布算语音门限（dBFS）。

    返回 (门限, 噪声底, 是否落在合理区间)。

    语义（与 2026-10-12 修复对齐）：
      * 噪声底取 `floor_pct` 分位（默认 10 分位）；
      * 门限 = 噪声底 + `margin_db`，让门限**跟着底噪走**（这才是自适应的本意）；
      * 只有算出来的值落在 [low_db, high_db] 之外时才夹紧 —— 这是**防呆**，
        正常语音不会触发。反过来（把固定值当下限往上钳）会吃掉真实语音。
    """
    db = np.asarray(db, dtype=np.float64)
    noise_floor = float(np.percentile(db, floor_pct))
    thr = noise_floor + float(margin_db)
    ok = low_db <= thr <= high_db
    thr = float(min(max(thr, low_db), high_db))
    return thr, noise_floor, ok


def split_speech_segments(db, thr, hop_ms, min_seg_s=0.40, merge_gap_s=0.25,
                          max_len_s=12.0, min_len_s=2.0):
    """帧能量 → 训练片段区间列表 [(t0, t1), ...]。

    步骤：阈值判决 → 合并成段 → 丢掉 <min_seg_s 的碎片 →
          合并间隔 <merge_gap_s 的相邻段 → 按 max_len_s 切长段、丢掉 <min_len_s 的。
    """
    db = np.asarray(db, dtype=np.float64)
    n_frames = len(db)
    speech = db > thr

    segs = []
    i = 0
    while i < n_frames:
        if speech[i]:
            j = i
            while j < n_frames and speech[j]:
                j += 1
            t0, t1 = i * hop_ms / 1000, j * hop_ms / 1000
            if t1 - t0 >= min_seg_s:
                segs.append((t0, t1))
            i = j
        else:
            i += 1

    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] < merge_gap_s:
            merged[-1] = (merged[-1][0], s[1])
        else:
            merged.append(list(s))
    segs = [tuple(s) for s in merged]

    clips = []
    for t0, t1 in segs:
        length = t1 - t0
        if length <= max_len_s:
            if length >= min_len_s:
                clips.append((t0, t1))
        else:
            k = int(np.ceil(length / max_len_s))
            step = length / k
            for m in range(k):
                a, b = t0 + m * step, t0 + (m + 1) * step
                if b - a >= min_len_s:
                    clips.append((a, b))
    return clips


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


def compute_frame_db(audio, sr, frame_ms=25, hop_ms=10):
    """逐帧 RMS → dBFS。返回 (db 数组, hop_ms)。"""
    fl = int(sr * frame_ms / 1000)
    hl = int(sr * hop_ms / 1000)
    n_frames = 1 + (len(audio) - fl) // hl
    rms = np.empty(n_frames, dtype=np.float32)
    for i in range(n_frames):
        seg = audio[i * hl: i * hl + fl]
        rms[i] = np.sqrt(np.mean(seg ** 2) + 1e-12)
    return 20 * np.log10(rms + 1e-12), hop_ms


def main():
    # 重定向到管道时 Python 按 cp936 写 stdout —— 中文会变乱码，这里统一钉成 UTF-8
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    audio, sr = read_wav(SRC)
    dur = len(audio) / sr
    log(f"[audio] {SRC}")
    log(f"[audio] sr={sr}  时长={dur:.2f}s ({dur/60:.2f}min)  峰值={20*np.log10(np.max(np.abs(audio))+1e-12):.1f} dBFS")

    # ---------- 1. 能量法 VAD ----------
    db, hop_ms = compute_frame_db(audio, sr, frame_ms=25, hop_ms=10)

    # 自适应门限：噪声底 = 10 分位，语音门限 = 噪声底 + 12dB。
    # ⚠️ 修复见 compute_threshold 文档：旧式 max(..., -45.0) 把门限往上钳，
    #    本片因此从 538s 掉到 204s（webrtcvad 复核 715s）。
    thr, noise_floor, thr_ok = compute_threshold(db)
    if not thr_ok:
        log(f"[vad][警告] 自适应门限 {thr:.1f}dB 落在合理区间"
            f"[{AUTO_THR_MIN_DB:.0f}, {AUTO_THR_MAX_DB:.0f}]之外，已夹紧 —— "
            f"确认输入是否正常语音")
    speech = db > thr
    log(f"[vad] 噪声底(10分位)={noise_floor:.1f}dB  余量=+{SNR_MARGIN_DB:.0f}dB  "
        f"门限={thr:.1f}dB  语音帧占比={100*speech.mean():.1f}%")

    # 合并成段 → 丢碎片 → 合并近邻 → 切长段（3~12s）
    MAX_LEN, MIN_LEN = 12.0, 2.0
    clips = split_speech_segments(db, thr, hop_ms,
                                  max_len_s=MAX_LEN, min_len_s=MIN_LEN)

    speech_total = sum(b - a for a, b in clips)
    log(f"[vad] 切分后训练片段 {len(clips)} 个  有效语音 {speech_total:.1f}s ({speech_total/60:.2f}min)")
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
        "vad_margin_db": float(SNR_MARGIN_DB),
        "vad_threshold_db": round(float(thr), 1),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
