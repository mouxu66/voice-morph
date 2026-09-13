# 用用户指定的两个纯净视频重建 meituan_rat 训练集：清空旧素材 -> 提取 -> 分离 -> 切片 -> 装入
import os
import sys
import shutil
from pathlib import Path

# 路径一律从「本文件位置 / 环境变量 / 家目录」推导，不写死作者机器路径
# （2026-09-13 开源前脱敏：原先写死 D:\变声 与 C:\Users\<作者名>\Downloads）
ROOT = Path(__file__).resolve().parents[2]        # experiments/tools/x.py -> 项目根
DOWNLOADS = Path.home() / "Downloads"
RVC_ROOT = Path(os.environ.get("VM_RVC_ROOT", "D:/RVC"))

sys.path.insert(0, str(ROOT / "m2_server"))
import importlib.util

spec = importlib.util.spec_from_file_location("pipeline", str(ROOT / "m1_workshop" / "pipeline.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

DATASET = RVC_ROOT / "dataset" / "meituan_rat"
VIDEOS = [
    DOWNLOADS / "video_260828_105338.mp4",
    DOWNLOADS / "video_260828_110637.mp4",
]

# 1) 清空旧数据集
DATASET.mkdir(parents=True, exist_ok=True)
removed = 0
for f in DATASET.glob("*.wav"):
    f.unlink()
    removed += 1
print(f"[清理] 删除旧素材 {removed} 条")

new_clips = []
for v in VIDEOS:
    wav = mod.step1_extract(v)
    vocal = mod.step2_separate(wav)
    prefix = v.stem  # 用完整 stem 避免两个视频前缀冲突
    n = mod.step3_slice(vocal, prefix)
    print(f"[切片] {v.name} -> {n} 条")
    # 收集本视频切出的片段
    new_clips.extend(sorted(mod.CLIPS_DIR.glob(f"{prefix}*.wav")))

# 2) 装入数据集
total_sec = 0.0
for i, c in enumerate(new_clips):
    dst = DATASET / f"rat_{i:04d}.wav"
    shutil.copy(c, dst)
    from pydub import AudioSegment
    total_sec += len(AudioSegment.from_wav(dst)) / 1000

print(f"[完成] 数据集共 {len(new_clips)} 条, 总时长 {total_sec:.0f}s, 位于 {DATASET}")
