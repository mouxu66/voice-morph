# -*- coding: utf-8 -*-
"""检查训练集标签质量：讲稿对齐文本 vs 音频回译，量化标签噪声。"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

OUT = r"D:\变声\media\ft_work\ppt12"
rows = [json.loads(l) for l in open(os.path.join(OUT, "train_raw.jsonl"), encoding="utf-8")]

print(f"共 {len(rows)} 条，总时长 {sum(1 for _ in rows)} 段")
print("=" * 100)
for i, r in enumerate(rows, 1):
    print(f"{i:3d} | {r['text']}")
print("=" * 100)
print(f"字符总数 {sum(len(r['text']) for r in rows)}")
