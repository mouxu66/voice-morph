"""同一句文本扫不同随机种子，找出「底噪是否可复现」的答案。

§9 发现 8 句里 s01/s02/s06 的 SNR 只有 12–16dB（其余 70dB+），
听感就是电流嘶嘶声。本脚本固定文本与音色，只换 seed，
判定脏底噪是「采样偶然」还是「该句必然」。

用法：
    cd tts_trial && venv312/Scripts/python.exe -u ../tools/tts_seed_scan.py \
        --text "今天天气巴适得很，我们出去耍一哈。" --speaker eric
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = Path(r"D:/tts_vendor/tf4573")
if (VENDOR / "transformers").is_dir():
    sys.path.insert(0, str(VENDOR))

import soundfile as sf  # noqa: E402
import torch  # noqa: E402

sys.path.insert(0, str(ROOT / "tts_trial" / "Qwen3-TTS"))
from qwen_tts import Qwen3TTSModel  # noqa: E402

MODEL = r"D:/变声/tts_models/qwen3-tts-1.7b-customvoice"


def snr_of(path: Path) -> tuple[float, float]:
    """返回 (静音段 floor, 粗估 SNR dB)。用 p10 当噪声、p90 当语音。"""
    import librosa
    import numpy as np

    y, sr = sf.read(str(path), dtype="float32")
    if y.ndim > 1:
        y = y.mean(1)
    if sr != 16000:
        y = librosa.resample(y, orig_sr=sr, target_sr=16000)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
    lo, hi = float(np.percentile(rms, 10)), float(np.percentile(rms, 90))
    return lo, 20 * float(np.log10(hi / max(lo, 1e-9)))


def main() -> int:
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True)
    ap.add_argument("--speaker", default="eric")
    ap.add_argument("--seeds", nargs="+", type=int, default=[0, 1, 2, 3, 4])
    ap.add_argument("--greedy", action="store_true", help="额外跑一次 do_sample=False")
    ap.add_argument("--out-dir", default=str(ROOT / "media/dialect_probe/seed_scan"))
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    model = Qwen3TTSModel.from_pretrained(MODEL, dtype=torch.bfloat16, device_map="cuda:0")
    print(f"文本: {args.text}   音色: {args.speaker}\n")

    runs = [(f"seed{s}", {"seed": s, "do_sample": True}) for s in args.seeds]
    if args.greedy:
        runs.append(("greedy", {"do_sample": False}))

    rows = []
    for name, kw in runs:
        torch.manual_seed(kw.get("seed", 1234))
        wavs, sr = model.generate_custom_voice(
            text=[args.text], speaker=[args.speaker], language=["Chinese"],
            **{k: v for k, v in kw.items() if k != "seed"})
        f = out / f"{name}.wav"
        sf.write(str(f), wavs[0], sr)
        lo, snr = snr_of(f)
        rows.append((name, lo, snr, len(wavs[0]) / sr))
        print(f"  {name:8s} dur={len(wavs[0]) / sr:5.2f}s floor={lo:.5f} SNR={snr:5.1f}dB")

    clean = [r for r in rows if r[2] >= 40]
    print(f"\n≥40dB 的有 {len(clean)}/{len(rows)} 条")
    if clean:
        best = max(clean, key=lambda r: r[2])
        print(f"最佳: {best[0]}  SNR={best[2]:.1f}dB  文件 {out / (best[0] + '.wav')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
