# -*- coding: utf-8 -*-
"""e30 vs e40 的细频段差值：两个 seed 是否同号（同号才是真差异）。"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
rows = json.loads((ROOT / "media/covers/ckpt/fine_bands.json").read_text(encoding="utf-8"))

print("band        s1234(e30-e40)  s4321(e30-e40)   同号?")
stable_up, stable_dn = [], []
for r in rows:
    a, b, c, d = r["vals"]
    d1, d2 = a - b, c - d
    same = (d1 > 0) == (d2 > 0)
    flag = "YES" if same else "no "
    print(f"{r['band']:>10s}  {d1:>+14.2f}  {d2:>+14.2f}   {flag}")
    if same and abs(d1) > 0.3:
        (stable_up if d1 > 0 else stable_dn).append((r["band"], round(d1, 2), round(d2, 2)))

print()
print(f"跨 seed 一致**更亮**的段：{stable_up}")
print(f"跨 seed 一致**更暗**的段：{stable_dn}")
