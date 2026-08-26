"""M1 辅助：把聚类分组生成合听预览，方便快速识别说话人

用法：
    python m1_workshop/make_previews.py
    → 读取 media/clips_groups.txt，把每组的片段合成一个预览 wav
    → 输出到 media/clip_previews/ 组01.wav ... 组NN.wav

之后按顺序试听这些预览文件，找到「美团老鼠」所在组，
用该组的片段名跑 build_reference.py。
"""
from pathlib import Path

from pydub import AudioSegment

ROOT = Path(__file__).resolve().parent.parent
CLIPS = ROOT / "media" / "clips"
GROUPS_FILE = ROOT / "media" / "clips_groups.txt"
OUT_DIR = ROOT / "media" / "clip_previews"


def main():
    if not GROUPS_FILE.exists():
        print("先运行 cluster_clips.py 生成分组清单")
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    lines = [l for l in GROUPS_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    for i, line in enumerate(lines, 1):
        # 格式: "组1 (7 段): clip_001, clip_002, ..."
        clips_part = line.split(":", 1)[1].strip()
        names = [n.strip() for n in clips_part.split(",") if n.strip()]
        merged = AudioSegment.silent(duration=200)
        for n in names:
            p = CLIPS / f"{n}.wav"
            if p.exists():
                merged += AudioSegment.from_wav(str(p)) + AudioSegment.silent(duration=200)
        out = OUT_DIR / f"组{i:02d}.wav"
        merged.export(str(out), format="wav")
        print(f"[{out.name}] 共 {len(names)} 段 -> 时长 {len(merged)/1000:.1f}s")

    print(f"\n预览文件已生成到 {OUT_DIR}/，按组试听，找到美团老鼠所在组！")


if __name__ == "__main__":
    main()
