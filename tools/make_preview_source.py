"""生成市场试听源句（assets/preview_source.wav）。

背景：市场试听是 RVC voice-to-voice —— 试听音频"说什么"完全由源句 wav 决定，
代码里的 PREVIEW_TEXT 只是文档说明，改它不会改变听到的内容。
所以要换试听台词，必须换这个源句音频。

做法：用当前源句（魔搭官方干净人声，与任何市场音色无源关系）当声纹参考，
让 TTS 念出新句子 —— 既换了内容，又保留"中性、无源音色残留"的优点。
ref_text 留空 → 走 x-vector 声纹模式，只取声纹不克隆原句韵律。

用法：
    python tools/make_preview_source.py --text "大家好，这是我的新声音，你觉得怎么样？"
    python tools/make_preview_source.py --text "..." --ref 某参考音.wav --out 指定路径

注意：换源句后，市场里已生成的试听缓存会因源句指纹变化自动失效重建，无需手动清理。
"""
from __future__ import annotations

import argparse
import io
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
M2 = ROOT / "m2_server"
sys.path.insert(0, str(M2))

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
from scipy.signal import resample_poly  # noqa: E402

DEFAULT_OUT = M2 / "assets" / "preview_source.wav"
TARGET_SR = 16000      # 与原内置源句一致（16k 单声道）
TARGET_PEAK = 0.7      # 归一到约 -3dB，避免源句过轻

DEFAULT_TEXT = "大家好，这是我的新声音，你觉得怎么样？"


def synth(text: str, ref: Path) -> tuple[np.ndarray, int]:
    import qwen3_tts

    raw = qwen3_tts.tts(text=text, ref_audio=str(ref), ref_text="", language="Chinese")
    x, sr = sf.read(io.BytesIO(raw))
    if x.ndim > 1:
        x = x.mean(axis=1)
    return np.asarray(x, dtype=np.float32), int(sr)


def normalize(x: np.ndarray, sr: int) -> np.ndarray:
    if sr != TARGET_SR:
        x = resample_poly(x, TARGET_SR, sr).astype(np.float32)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak > 0:
        x = x * (TARGET_PEAK / peak)
    return x


def main() -> int:
    ap = argparse.ArgumentParser(description="生成市场试听源句")
    ap.add_argument("--text", default=DEFAULT_TEXT, help="试听台词（建议 4~5 秒、口语化）")
    ap.add_argument("--ref", default="", help="声纹参考音；默认用当前源句本身")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出路径")
    ap.add_argument("--no-backup", action="store_true", help="不备份原源句")
    args = ap.parse_args()

    out = Path(args.out)
    ref = Path(args.ref) if args.ref else DEFAULT_OUT
    if not ref.exists():
        print(f"[fail] 声纹参考音不存在：{ref}")
        return 1

    print(f"[1/4] 台词：{args.text}")
    print(f"[2/4] 声纹参考：{ref.name}（x-vector 模式，只取声纹）")
    x, sr = synth(args.text, ref)
    dur = len(x) / sr if sr else 0
    print(f"      合成完成：{sr}Hz / {dur:.2f}s")

    x = normalize(x, sr)
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    if rms <= 1e-3:
        print("[fail] 合成结果近乎无声，未写入")
        return 1

    if out.exists() and not args.no_backup:
        bak = out.with_suffix(".wav.orig.bak")
        if not bak.exists():
            shutil.copy2(out, bak)
            print(f"[3/4] 已备份原源句 → {bak.name}")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + "_tmp.wav")
    sf.write(str(tmp), x, TARGET_SR)
    tmp.replace(out)
    print(f"[4/4] 已写入 {out}（{TARGET_SR}Hz / {len(x)/TARGET_SR:.2f}s / RMS {rms:.4f}）")
    print("      旧试听缓存会因源句指纹变化自动重建，无需手动清理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
