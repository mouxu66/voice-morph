# -*- coding: utf-8 -*-
"""epoch 曲线探测：同一模型在 e10/e20/e30/e40 四个检查点上的音色表现。

回答的问题：「12 分钟 + 40 epoch 是不是已经吃满？」
  · 若 e40 仍明显优于 e30 → 还没吃满，加数据/加 epoch 有空间；
  · 若 e20≈e30≈e40（平台） → 模型在**当前数据**上已经收敛，继续训无益。

口径与报告 §5 完全一致：同 seed（1234）、同输入、同参考音、同 index。
"""
import json
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

BASE = r"D:\变声\media\ft_work\ppt12"
SRC_WORK = os.path.join(BASE, "myvoice_rvc_full")          # 取 in_*.wav
PROBE = os.path.join(BASE, "epoch_probe")
WEIGHTS = r"D:\RVC\assets\weights"
INDEX = (r"D:\RVC\logs\myvoice_ppt12_full"
         r"\added_IVF817_Flat_nprobe_1_myvoice_ppt12_full_v2.index")
PY = r".venv\Scripts\python.exe"
SEED = "1234"

CKPTS = [
    ("e10", os.path.join(WEIGHTS, "myvoice_ppt12_full_e10_s990.pth")),
    ("e20", os.path.join(WEIGHTS, "myvoice_ppt12_full_e20_s1980.pth")),
    ("e30", os.path.join(WEIGHTS, "myvoice_ppt12_full_e30_s2970.pth")),
    ("e40", os.path.join(WEIGHTS, "myvoice_ppt12_full_e40_s3960.pth")),
]

rows = []
for tag, pth in CKPTS:
    if not os.path.exists(pth):
        print(f"[skip] {tag} 缺权重 {pth}")
        continue
    work = os.path.join(PROBE, tag)
    os.makedirs(work, exist_ok=True)
    # 输入复用同一批（保证只换 epoch 这一个变量）
    for f in os.listdir(SRC_WORK):
        if f.startswith("in_") and f.endswith(".wav"):
            shutil.copy2(os.path.join(SRC_WORK, f), os.path.join(work, f))

    print(f"\n===== {tag} =====", flush=True)
    r = subprocess.run(
        [PY, r"tts_trial\rvc_convert_full.py", "--pth", pth, "--index", INDEX,
         "--work", work, "--seed", SEED],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        print(f"[FAIL] 转换失败：{(r.stderr or '')[-600:]}")
        continue
    for ln in (r.stdout or "").splitlines():
        if ln.startswith("[ok]"):
            print("   " + ln)

    # 评测（写出到自己的 json，不覆盖主目录）
    ev = subprocess.run(
        [PY, r"tts_trial\ab_eval_rvc_vs_qwen.py", "--dir", work,
         "--out", os.path.join(work, "eval.json")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if ev.returncode != 0:
        print(f"[FAIL] 评测失败：{(ev.stderr or '')[-600:]}")
        continue

    d = json.load(open(os.path.join(work, "eval.json"), encoding="utf-8"))
    rs = d["rows"]
    n = len(rs)
    tts_cam = sum(x["campp_after"] for x in rs) / n
    tts_delta = sum(x["campp_delta"] for x in rs) / n
    tts_asr = sum(x["asr_after"] for x in rs) / n
    ceil_after = (d.get("ceiling") or {}).get("campp_after")
    rows.append(dict(tag=tag, tts_cam=tts_cam, tts_delta=tts_delta,
                     tts_asr=tts_asr, ceil=ceil_after, n=n))
    print(f"   → CAM++ 后={tts_cam:.4f}  Δ={tts_delta:+.4f}  可懂度={tts_asr:.3f}  "
          f"天花板={ceil_after:.4f}" if ceil_after else "", flush=True)

print("\n" + "=" * 72)
print(f"{'epoch':<8}{'TTS→RVC':>10}{'Δ':>10}{'可懂度':>9}{'真人→RVC':>11}{'两者差距':>10}")
print("-" * 72)
for r in rows:
    gap = (r["ceil"] - r["tts_cam"]) if r["ceil"] else None
    print(f"{r['tag']:<8}{r['tts_cam']:>10.4f}{r['tts_delta']:>+10.4f}{r['tts_asr']:>9.3f}"
          f"{(r['ceil'] if r['ceil'] else float('nan')):>11.4f}"
          f"{(gap if gap is not None else float('nan')):>10.4f}")

if len(rows) >= 2:
    print("\n相邻检查点的变化（看是否还在爬）：")
    for a, b in zip(rows, rows[1:]):
        print(f"  {a['tag']}→{b['tag']}: TTS {b['tts_cam'] - a['tts_cam']:+.4f}   "
              f"天花板 {(b['ceil'] or 0) - (a['ceil'] or 0):+.4f}")
    total = rows[-1]["tts_cam"] - rows[0]["tts_cam"]
    last = rows[-1]["tts_cam"] - rows[-2]["tts_cam"]
    print(f"\n  全程 {rows[0]['tag']}→{rows[-1]['tag']}: {total:+.4f}")
    print(f"  末段 {rows[-2]['tag']}→{rows[-1]['tag']}: {last:+.4f}")
    print(f"  ⇒ 末段占全程比例 = {100 * last / total:.0f}%" if total else "")

with open(os.path.join(PROBE, "epoch_curve.json"), "w", encoding="utf-8") as f:
    json.dump(rows, f, ensure_ascii=False, indent=2)
print(f"\n[done] {os.path.join(PROBE, 'epoch_curve.json')}")
