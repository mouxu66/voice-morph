"""四川/重庆方言 TTS 探针：一段参考音做零样本克隆，看口音能不能跟着走。

背景：`tts_models/qwen3-tts-1.7b-base` 的 codec_language_id 只有 10 种语言，
没有任何方言标签（`spk_id` / `spk_is_dialect` 都是空字典），所以它不会「按标签
说四川话」。但 Base 是零样本克隆模型，克隆时会同时带上参考音的**音色与韵律**——
包括口音。于是判断只剩一个实证问题：

    给一段四川话参考音，出来的新句子还是不是四川话？

用法：
    # 口音迁移检验（主实验）
    python -u dialect_probe.py --ref-audio 我的四川话.wav \
        --ref-text "参考音里说的那句话" --tag sichuan

    # 对照：普通话参考音跑同一批文本
    python -u dialect_probe.py --ref-audio 我的普通话.wav \
        --ref-text "..." --tag mandarin --limit 3

    # 只跑前两句快速验证
    python -u dialect_probe.py --ref-audio x.wav --ref-text "..." --limit 2
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

MODEL_DIR = r"D:/变声/tts_models/qwen3-tts-1.7b-base"
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
    ap.add_argument("--ref-audio", required=True, help="参考音路径")
    ap.add_argument("--ref-text", required=True, help="参考音说了什么（必须逐字一致）")
    ap.add_argument("--tag", default="probe", help="本次实验标签，用于输出目录")
    ap.add_argument("--limit", type=int, default=len(TEXTS))
    ap.add_argument("--x-vector-only", action="store_true",
                    help="只取音色、不取韵律；口音迁移实验不要开")
    args = ap.parse_args()

    ref = Path(args.ref_audio)
    if not ref.exists():
        print(f"参考音不存在：{ref}", flush=True)
        return 2

    out_dir = OUT_ROOT / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"加载模型 {MODEL_DIR} ...", flush=True)
    t0 = time.time()
    model = Qwen3TTSModel.from_pretrained(
        MODEL_DIR, device_map="cuda:0", dtype=torch.bfloat16)
    print(f"模型就绪 {time.time() - t0:.1f}s", flush=True)

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
        "ref_audio": str(ref),
        "ref_text": args.ref_text,
        "x_vector_only": bool(args.x_vector_only),
        "model": MODEL_DIR,
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
