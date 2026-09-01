# coding=utf-8
"""把 media/clips 下的切片用 faster-whisper 转写成 (audio, text, ref_audio) 训练 JSONL。

Qwen3-TTS 的 SFT（sft_12hz_qlora.py）要求每个训练样本带文本转写：
    {"audio": <切片wav>, "text": <该切片的中文文本>, "ref_audio": <干净袋鼠参考音>}
本脚本补齐「现有流水线缺失的 text 一步」，让视频素材能直接进微调。

用法（在 venv312 里跑，因为 faster-whisper 装在那里）：
    D:/变声/tts_trial/venv312/Scripts/python.exe m1_workshop/transcribe_for_ft.py \
        --clips_dir media/clips \
        --ref_audio media/voicebank/merg_004/reference.wav \
        --output train_raw.jsonl \
        --model small --lang zh

过滤策略（避免脏数据进训练）：
    - 转写文本长度在 [min_chars, max_chars] 之间
    - 切片时长在 [min_sec, max_sec] 之间（太短是碎音/静音，太长容易混入多说话人）
    - 启用 vad_filter 去掉非语音段
被跳过的片段会打印出来，方便人工复查。
"""
import argparse
import glob
import json
import os

import soundfile as sf
from faster_whisper import WhisperModel


def clip_duration(path: str):
    try:
        info = sf.info(path)
        return info.frames / info.samplerate
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips_dir", default="media/clips")
    ap.add_argument("--ref_audio", required=True,
                    help="干净袋鼠参考音（说话人锚点），建议 10~30s 单人安静录音")
    ap.add_argument("--output", default="train_raw.jsonl")
    ap.add_argument("--model", default="small", help="faster-whisper 模型尺寸")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--min_chars", type=int, default=2)
    ap.add_argument("--max_chars", type=int, default=200)
    ap.add_argument("--min_sec", type=float, default=1.5)
    ap.add_argument("--max_sec", type=float, default=12.0)
    ap.add_argument("--device", default="auto")
    args = ap.parse_args()

    print(f"[transcribe] 加载 Whisper {args.model} ({args.lang}) ...")
    model = WhisperModel(args.model, device=args.device, compute_type="int8")

    ref_audio = os.path.abspath(args.ref_audio)
    if not os.path.exists(ref_audio):
        raise FileNotFoundError(f"ref_audio 不存在: {ref_audio}")

    clips = sorted(glob.glob(os.path.join(args.clips_dir, "*.wav")))
    out = []
    skipped = []
    for p in clips:
        dur = clip_duration(p)
        segments, _ = model.transcribe(
            p, language=args.lang, beam_size=5, vad_filter=True
        )
        text = "".join(seg.text for seg in segments).strip().replace("\n", " ")
        if (not text
                or len(text) < args.min_chars
                or len(text) > args.max_chars
                or dur is None
                or dur < args.min_sec
                or dur > args.max_sec):
            skipped.append((os.path.basename(p), round(dur, 2) if dur else None, text))
            continue
        out.append({
            "audio": os.path.abspath(p),
            "text": text,
            "ref_audio": ref_audio,
        })

    with open(args.output, "w", encoding="utf-8") as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")

    print(f"[transcribe] 写入 {len(out)} 条 -> {args.output}")
    print(f"[transcribe] 跳过 {len(skipped)} 条（噪声/过短/过长/空转写），示例如下：")
    for s in skipped[:25]:
        print("   skip:", s)

    print("\n[transcribe] 已采用样本预览：")
    for o in out[:10]:
        print(f"   {os.path.basename(o['audio'])} | {o['text']}")


if __name__ == "__main__":
    main()
