# 对 meituan_rat 训练素材做声纹聚类，找出混入的其他说话人
import sys
sys.path.insert(0, r"D:\变声\m2_server")
from pathlib import Path

OUT = r"D:\变声\tools\cluster_result.txt"
lines = []
try:
    from qwen3_tts import analyze
    clips = [{"name": p.stem, "path": str(p)} for p in sorted(Path(r"D:\RVC\dataset\meituan_rat").glob("*.wav"))]
    lines.append(f"total={len(clips)}")
    # 阈值 0.72：尽可能拆出混入的第二说话人
    result = analyze(clips, sim_threshold=0.72, min_cluster_size=2)
    for i, c in enumerate(result["clusters"]):
        lines.append(f"cluster{i}: size={c['size']} rep={c['rep']['name']} text={c['rep']['text'][:30]}")
        lines.append(f"  members: {', '.join(c['members'])}")
except Exception as e:
    import traceback
    lines.append("EXC: " + traceback.format_exc())

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("done")
