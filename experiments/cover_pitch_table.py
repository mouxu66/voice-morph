# -*- coding: utf-8 -*-
"""音域扫描结果表：变调 vs「像你 / 撞原唱 / 质心」。"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
rep = json.loads((ROOT / "media/covers/pitchscan/metrics.json").read_text(encoding="utf-8"))
scan = json.loads((ROOT / "media/covers/pitchscan/pitch_scan.json").read_text(encoding="utf-8"))

# 把 rows 里的 pitch → 预期中位基频，按 wav 文件名对齐
by_name = {}
for r in scan["rows"]:
    by_name[pathlib.Path(r["wav"]).stem] = r

print(f"{'档位':>6} {'输出中位Hz':>11} {'谱质心Hz':>9} {'4-8k dB':>8} "
      f"{'CAM++对你':>10} {'CAM++对原唱':>12}")
rows = []
for name, m in rep["files"].items():
    if name == "source":
        print(f"{'原唱':>6} {'259.4':>11} {m.get('centroid_hz'):>9} "
              f"{str(m.get('band_db', {}).get('4-8k')):>8} "
              f"{str(m.get('secs_vs_your_ref')):>10} {str(m.get('secs_vs_orig_singer')):>12}")
        continue
    r = by_name.get(name, {})
    p = r.get("pitch")
    print(f"{p:>+6} {str(r.get('median_f0_hz_expected')):>11} "
          f"{m.get('centroid_hz'):>9} {str(m.get('band_db', {}).get('4-8k')):>8} "
          f"{str(m.get('secs_vs_your_ref')):>10} {str(m.get('secs_vs_orig_singer')):>12}")
    rows.append((p, r.get("median_f0_hz_expected"), m.get("centroid_hz"),
                 m.get("band_db", {}).get("4-8k"),
                 m.get("secs_vs_your_ref"), m.get("secs_vs_orig_singer")))

rows.sort(key=lambda x: x[0])
print()
print("★ 读法：『CAM++ 对你』越高越像你；『对原唱』越高越是掺了原唱的音色。")
if rows:
    best_you = max(rows, key=lambda x: x[4] or -9)
    print(f"  最像你的一档：pitch {best_you[0]:+d}（{best_you[1]} Hz）→ "
          f"对你 {best_you[4]} / 对原唱 {best_you[5]}")
    print(f"  最不撞原唱的一档：pitch {min(rows, key=lambda x: x[5] or 9)[0]:+d}")
