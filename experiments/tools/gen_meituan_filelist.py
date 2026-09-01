# 生成 meituan_rat 的 filelist.txt：只收录本轮(19:40后)预处理的新素材，排除上轮污染残留
import os
from pathlib import Path

LOGDIR = Path(r"D:\RVC\logs\meituan_rat")
CUTOFF = LOGDIR.stat().st_mtime  # 不用，改用固定时间线
import datetime
cutoff = datetime.datetime(2026, 8, 29, 19, 40).timestamp()

gt, ft, f0d, f0n = (LOGDIR / d for d in ("0_gt_wavs", "3_feature768", "2a_f0", "2b-f0nsf"))
lines, skipped = [], 0
for wav in sorted(gt.glob("*.wav")):
    if wav.stat().st_mtime < cutoff:
        skipped += 1
        continue
    stem = wav.stem
    if not (ft / f"{stem}.npy").exists() or not (f0d / f"{stem}.wav.npy").exists() or not (f0n / f"{stem}.wav.npy").exists():
        print(f"[缺特征] {stem}")
        continue
    lines.append(f"D:/RVC/logs/meituan_rat\\0_gt_wavs\\{stem}.wav|D:/RVC/logs/meituan_rat\\3_feature768\\{stem}.npy|D:/RVC/logs/meituan_rat\\2a_f0\\{stem}.wav.npy|D:/RVC/logs/meituan_rat\\2b-f0nsf\\{stem}.wav.npy|0")

with open(LOGDIR / "filelist.txt", "w", encoding="utf8") as f:
    f.write("\n".join(lines))
print(f"filelist: {len(lines)} 条 (排除旧残留 {skipped})")
