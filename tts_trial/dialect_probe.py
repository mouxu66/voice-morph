"""方言 TTS 探针：两种发声方式跑同一批文本，用于判断「口音到底跟不跟得上」。

两种模式（二选一）：

1. **内置音色模式** —— 用 CustomVoice 检查点自带的音色。
   `eric` 在 config 里是 `sichuan_dialect`（成都男声，language_id 2062），
   `dylan` 是 `beijing_dialect`，其余是标准普通话音色。
   这一模式不依赖任何外部音频，用来回答「开源模型说四川话到底行不行」。

       python -u dialect_probe.py --model D:/变声/tts_models/qwen3-tts-1.7b-customvoice \
           --speaker eric --tag eric_sichuan

2. **零样本克隆模式** —— 给一段参考音 + 逐字文本，把参考音的音色和口音搬过来。
   这一模式用来回答「换成任意声音之后，口音还在不在」。

       python -u dialect_probe.py --ref-audio 我的四川话.wav \
           --ref-text "参考音里说的那句话" --tag myclone

   注意 ref_text 必须与音频逐字一致，写错会直接毁掉韵律。

Base 检查点（`qwen3-tts-1.7b-base`）的 `spk_id` 与 `spk_is_dialect` 是空的，
只能跑模式 2；CustomVoice 检查点才有内置方言音色，跑模式 1。
"""

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

# venv312 的 transformers 已被升到 5.x，而仓库内嵌的 Qwen3-TTS 锁的是 4.57.3
# （其 pyproject.toml）。5.x 下 `@check_model_inputs()` 会 TypeError。
# 这里把 4.57.3 用 --target 装在**仓库外**（不进 git，避免污染门禁），仅本脚本可见。
# 重建命令见 docs/方言-四川重庆-2026-10-22.md §7。
_VENDOR_CANDIDATES = (
    Path(r"D:/tts_vendor/tf4573"),
    Path(__file__).resolve().parent / "_vendor_tf",
)
for _p in _VENDOR_CANDIDATES:
    if (_p / "transformers").is_dir():
        sys.path.insert(0, str(_p))
        break

import soundfile as sf  # noqa: E402
import torch  # noqa: E402

# Qwen3-TTS 是仓库内挂载的源码目录，不在 site-packages 里，必须显式加路径
sys.path.insert(0, str(Path(__file__).resolve().parent / "Qwen3-TTS"))
from qwen_tts import Qwen3TTSModel  # noqa: E402

BASE_MODEL = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
OUT_ROOT = Path(r"D:/变声/media/dialect_probe")

# 探针文本：方言词密度从低到高，故意混入普通话里没有对照的用法，
# 只靠音色改不掉、必须真的换成四川话音系才念得顺。
TEXTS = {
    "s01": "今天天气巴适得很，我们出去耍一哈。",
    "s02": "你吃饭没得？没吃的话我们一起。",
    "s03": "这个事情我晓得，你放心嘛。",
    "s04": "你在爪子哦，啷个半天不回消息。",
    "s05": "莫得事，慢慢来，不着急。",
    "s06": "这个东西有点贵，但是味道确实安逸。",
    "s07": "他昨天在屋头耍了一整天，啥子都没做。",
    "s08": "要得，那就这么说定了，明天见。",
}


def main() -> int:
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=BASE_MODEL, help="检查点目录")
    ap.add_argument("--speaker", help="内置音色名（CustomVoice 检查点专用），如 eric")
    ap.add_argument("--ref-audio", help="参考音路径（克隆模式）")
    ap.add_argument("--ref-text", help="参考音的逐字文本（克隆模式，必须与音频一致）")
    ap.add_argument("--x-vector-only", action="store_true",
                    help="只取音色不取韵律；口音迁移实验不要开")
    ap.add_argument("--tag", default="probe", help="本次实验标签，用于输出目录")
    ap.add_argument("--limit", type=int, default=len(TEXTS))
    args = ap.parse_args()

    clone_mode = bool(args.ref_audio or args.ref_text)
    if args.speaker and clone_mode:
        print("--speaker 与 --ref-audio/--ref-text 互斥，只能选一种模式", flush=True)
        return 2
    if not args.speaker and not clone_mode:
        print("必须指定 --speaker 或 --ref-audio + --ref-text", flush=True)
        return 2
    if clone_mode and not (args.ref_audio and args.ref_text):
        print("克隆模式要求 --ref-audio 与 --ref-text 同时给出", flush=True)
        return 2

    ref = Path(args.ref_audio) if args.ref_audio else None
    if ref is not None and not ref.exists():
        print(f"参考音不存在：{ref}", flush=True)
        return 2

    out_dir = OUT_ROOT / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"加载模型 {args.model} ...", flush=True)
    t0 = time.time()
    model = Qwen3TTSModel.from_pretrained(
        args.model, device_map="cuda:0", dtype=torch.bfloat16)
    print(f"模型就绪 {time.time() - t0:.1f}s", flush=True)

    speakers = model.get_supported_speakers()
    if args.speaker:
        print(f"可用音色：{speakers}", flush=True)
        if args.speaker.lower() not in {s.lower() for s in (speakers or [])}:
            print(f"音色 {args.speaker} 不在清单里", flush=True)
            return 2
        prompt = None
    else:
        prompt = model.create_voice_clone_prompt(
            ref_audio=str(ref),
            ref_text=args.ref_text,
            x_vector_only_mode=bool(args.x_vector_only),
        )
        print(f"音色向量已建（x_vector_only={bool(args.x_vector_only)}）", flush=True)

    items = list(TEXTS.items())[: args.limit]
    manifest = []
    for key, text in items:
        t1 = time.time()
        if args.speaker:
            wavs, sr = model.generate_custom_voice(
                text=[text], speaker=[args.speaker], language=["Chinese"])
        else:
            wavs, sr = model.generate_voice_clone(
                text=[text], language=["Chinese"], voice_clone_prompt=prompt)
        path = out_dir / f"{key}.wav"
        sf.write(str(path), wavs[0], sr)
        dur = len(wavs[0]) / sr
        cost = time.time() - t1
        print(f"  {key}  {dur:5.2f}s  用时 {cost:5.1f}s  {text}", flush=True)
        manifest.append({
            "key": key,
            "text": text,
            "file": str(path),
            "dur": round(dur, 3),
            "gen_sec": round(cost, 2),
            "sr": sr,
        })

    meta = {
        "mode": "custom_voice" if args.speaker else "voice_clone",
        "model": args.model,
        "speaker": args.speaker,
        "ref_audio": str(ref) if ref else None,
        "ref_text": args.ref_text,
        "x_vector_only": bool(args.x_vector_only) if prompt else None,
        "items": manifest,
    }
    meta_path = out_dir / "manifest.json"
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n产物：{out_dir}（{len(manifest)} 条）", flush=True)
    print(f"清单：{meta_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
