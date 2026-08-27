# -*- coding: utf-8 -*-
"""用 faster_whisper 给 feidudu 切片转写，组装 Qwen3-TTS 微调 JSONL（结果落盘 ft_prepare_out.txt）。"""
import json
import os

from faster_whisper import WhisperModel
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
CLIPS_DIR = os.path.join(HERE, "ft_data")            # 24kHz 重采样后的切片
REF = os.path.join(HERE, "ft_data", "feidudu_full.wav")
OUT_JSONL = os.path.join(HERE, "ft_train_raw.jsonl")
OUT_TXT = os.path.join(HERE, "ft_prepare_out.txt")

open(OUT_TXT, "w").close()


def log(m):
    with open(OUT_TXT, "a", encoding="utf-8") as f:
        f.write(m + "\n")


whisper = WhisperModel("small", device="cuda", compute_type="float16")

rows = []
for name in sorted(os.listdir(CLIPS_DIR)):
    if not name.endswith(".wav"):
        continue
    path = os.path.join(CLIPS_DIR, name)
    audio, sr = sf.read(path)
    dur = len(audio) / sr
    segments, _ = whisper.transcribe(path, language="zh", vad_filter=True)
    text = "".join(s.text for s in segments).strip()
    log(f"[clip] {name} {dur:.1f}s -> {text!r}")
    if text:
        rows.append({"audio": path.replace("\\", "/"),
                     "text": text,
                     "ref_audio": REF.replace("\\", "/")})

# ref_audio 本身也作为一条训练样本（文本=它的整体转写）
segments, _ = whisper.transcribe(REF, language="zh", vad_filter=True)
ref_text = "".join(s.text for s in segments).strip()
audio, sr = sf.read(REF)
log(f"[ref ] full {len(audio)/sr:.1f}s -> {ref_text!r}")
if ref_text:
    rows.append({"audio": REF.replace("\\", "/"), "text": ref_text,
                 "ref_audio": REF.replace("\\", "/")})

with open(OUT_JSONL, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")

total_speech = sum(len(sf.read(r["audio"])[0]) / sr for r in rows)
log(f"[done] {len(rows)} 条样本写入 {OUT_JSONL}，总语音时长约 {total_speech:.1f}s")
