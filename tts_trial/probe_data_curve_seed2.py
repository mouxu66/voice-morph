# -*- coding: utf-8 -*-
"""数据量曲线的 seed 稳健性：4/8/12.23min 三档各换第二个 seed 复跑。

为什么必须做（2026-10-13 的教训）：上一轮 index_rate 的"形态"结论就是被
第二个 seed 推翻的（峰值位置随 seed 变）。数据量曲线若只有单 seed，
"还在涨/已平台"这种形态判断同样不可信。

产物写到各档的 eval_work_s4321/，不覆盖主口径。
"""
import json
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

BASE = r"D:\变声\media\ft_work\ppt12"
PROBE = os.path.join(BASE, "data_curve")
PY = r".venv\Scripts\python.exe"
DATASET_RAW = r"D:\RVC\dataset_raw"
SEED2 = "4321"

CASES = [
    ("4min", os.path.join(PROBE, "4min", "eval_work_s4321"),
     r"D:\RVC\assets\weights\myvoice_probe_4min.pth",
     r"D:\RVC\logs\myvoice_probe_4min"),
    ("8min", os.path.join(PROBE, "8min", "eval_work_s4321"),
     r"D:\RVC\assets\weights\myvoice_probe_8min.pth",
     r"D:\RVC\logs\myvoice_probe_8min"),
    ("full", os.path.join(BASE, "myvoice_rvc_full_s4321"),
     r"D:\RVC\assets\weights\myvoice_ppt12_full.pth",
     r"D:\RVC\logs\myvoice_ppt12_full"),
]

rows = []
for tag, work, pth, idxdir in CASES:
    import glob
    index = next(iter(sorted(glob.glob(os.path.join(idxdir, "added_*.index")))), "")
    if not os.path.exists(pth):
        print(f"[skip] {tag}: 缺权重 {pth}")
        continue
    os.makedirs(work, exist_ok=True)
    # 输入统一取主目录的 in_*.wav（三档同输入 ⇒ 只改数据量）
    for f in os.listdir(os.path.join(BASE, "myvoice_rvc_full")):
        if f.startswith("in_") and f.endswith(".wav"):
            shutil.copy2(os.path.join(BASE, "myvoice_rvc_full", f), os.path.join(work, f))

    # full @4321 已有产物，跳过转换（省 2 分钟）
    need = not all(os.path.exists(os.path.join(work, n))
                   for n in ("out_real.wav", "out_tts01.wav", "out_tts02.wav", "out_tts03.wav"))
    print(f"\n===== {tag} @seed{SEED2} =====", flush=True)
    if need:
        c = subprocess.run([PY, r"tts_trial\rvc_convert_full.py", "--pth", pth,
                            "--index", index, "--work", work, "--seed", SEED2],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if c.returncode != 0:
            print(f"[FAIL] 转换 {(c.stderr or '')[-400:]}")
            continue
    else:
        print("   （复用已有 seed4321 产物）")

    ev = subprocess.run([PY, r"tts_trial\ab_eval_rvc_vs_qwen.py", "--dir", work,
                         "--out", os.path.join(work, "eval.json")],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if ev.returncode != 0:
        print(f"[FAIL] 评测 {(ev.stderr or '')[-400:]}")
        continue
    d = json.load(open(os.path.join(work, "eval.json"), encoding="utf-8"))
    rs = d["rows"]
    n = len(rs)
    rep = os.path.join(DATASET_RAW, f"myvoice_probe_{tag}_40k_report.json")
    minutes = (json.load(open(rep, encoding="utf-8"))["total_min"]
               if os.path.exists(rep) else 12.23)
    row = dict(tag=tag, minutes=minutes,
               tts=sum(x["campp_after"] for x in rs) / n,
               delta=sum(x["campp_delta"] for x in rs) / n,
               asr=sum(x["asr_after"] for x in rs) / n,
               ceil=(d.get("ceiling") or {}).get("campp_after"))
    rows.append(row)
    print(f"   → CAM++={row['tts']:.4f} Δ={row['delta']:+.4f} "
          f"可懂度={row['asr']:.3f} 天花板={row['ceil']:.4f}", flush=True)

rows.sort(key=lambda x: x["minutes"])
print("\n" + "=" * 74)
print(f"{'数据量':<10}{'TTS→RVC':>10}{'Δ':>10}{'可懂度':>9}{'天花板':>10}")
print("-" * 74)
for r in rows:
    print(f"{r['tag']:<10}{r['tts']:>10.4f}{r['delta']:>+10.4f}{r['asr']:>9.3f}{r['ceil']:>10.4f}")
print("\n相邻档斜率（seed=4321）：")
for a, b in zip(rows, rows[1:]):
    dm = b["minutes"] - a["minutes"]
    dcam = b["tts"] - a["tts"]
    if dm > 0:
        print(f"  +{dm:.2f}min: TTS {dcam:+.4f}（{dcam/dm:+.5f}/min）  "
              f"天花板 {b['ceil'] - a['ceil']:+.4f}")
with open(os.path.join(PROBE, "data_curve_seed4321.json"), "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)
print(f"\n[done] {os.path.join(PROBE, 'data_curve_seed4321.json')}")
