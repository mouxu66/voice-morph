"""M1：勾选片段 → 参考音频（音色档案）

支持两种调用方式：

1) 从零生成（新建音色档案）：
   python m1_workshop/build_reference.py meituan_rat clip_001 clip_004 clip_007

2) 追加素材（已有档案，再加片段，支持"多素材聚合"）：
   python m1_workshop/build_reference.py --append meituan_rat clip_009 clip_012

第二个参数起是 media/clips/ 里文件名去掉 .wav 的部分。
只挑【美团老鼠音色】的片段，剔除 BGM 残留和其他角色的声音！
"""
import argparse
from pathlib import Path

from pydub import AudioSegment

ROOT = Path(__file__).resolve().parent.parent
CLIPS = ROOT / "media" / "clips"
BANK = ROOT / "media" / "voicebank"

SILENCE_GAP_MS = 300   # 片段间留的静音缝隙
SAMPLE_RATE = 22050


def resolve_clip(name: str) -> Path:
    """支持传入 'clip_001' 或 'clip_001.wav'"""
    p = CLIPS / name
    if not p.suffix:
        p = p.with_suffix(".wav")
    if not p.exists():
        raise FileNotFoundError(f"片段不存在: {p}（请先跑 pipeline.py）")
    return p


def main():
    parser = argparse.ArgumentParser(description="构建/追加音色参考音频")
    parser.add_argument("voice_id", help="音色ID，如 meituan_rat")
    parser.add_argument("clips", nargs="+", help="片段名，如 clip_001")
    parser.add_argument("--append", action="store_true", help="追加到已有音色档案")
    args = parser.parse_args()

    voice_id, selected = args.voice_id, args.clips
    out_dir = BANK / voice_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "reference.wav"

    # 追加模式：先载入已有参考音频
    if args.append and out.exists():
        merged = AudioSegment.from_wav(out)
    else:
        merged = AudioSegment.silent(duration=SILENCE_GAP_MS)

    added = 0
    for name in selected:
        p = resolve_clip(name)
        seg = AudioSegment.from_wav(p)
        merged += seg + AudioSegment.silent(duration=SILENCE_GAP_MS)
        added += 1

    merged = merged.set_channels(1).set_frame_rate(SAMPLE_RATE)
    merged.export(out, format="wav")
    mode = "追加" if args.append else "生成"
    print(f"[{mode}] 音色 [{voice_id}] 参考音频 -> {out}（时长 {len(merged)/1000:.1f}s，新增 {added} 个片段）")
    print(f"       提示：零样本档建议素材 ≥30s；精细档(GPT-SoVITS)需 ≥60s 纯净音频。")


if __name__ == "__main__":
    main()
