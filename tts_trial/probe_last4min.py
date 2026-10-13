# -*- coding: utf-8 -*-
"""对照：取「**最后** 4 分钟」而非「前 4 分钟」再训一轮。

为什么必须做（自查出的混淆变量）
--------------------------------
`probe_data_curve.py` 的 4min/8min 子集是按文件名顺序取的**前缀**
（4min = ppt12_0001..0030，是演讲的开头：项目概述/背景）。
于是"4min 明显更差"有两种解释：
  A. **数据量**不够（我们想要的结论）；
  B. 这 4 分钟**内容特殊**（开场更正式/语速不同/信息密度低）。
只有再取一段**同等时长但不同位置**的子集，才能分开：
  · 若"后 4min"也明显差于 8min ⇒ 是数据量（B 被排除）；
  · 若"后 4min"接近 8min      ⇒ 是内容（A 被削弱，得改结论）。

同时这也能验证"前缀单调上升"是不是内容顺序造成的假象。
"""
import glob
import json
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

BASE = r"D:\变声\media\ft_work\ppt12"
CLIPS = os.path.join(BASE, "clips24k")
PROBE = os.path.join(BASE, "data_curve", "last4min")
DATASET_RAW = r"D:\RVC\dataset_raw"
PY = r".venv\Scripts\python.exe"
RVC_PY = r"D:\RVC\.venv\Scripts\python.exe"
EXP = "myvoice_probe_last4min"
DST = os.path.join(DATASET_RAW, f"{EXP}_40k")
SEED = "1234"


def pick_last(target_s=240.0):
    """从**末尾**往前取，凑够 target_s（与 4min 档同时长）。"""
    import soundfile as sf

    names = sorted(f for f in os.listdir(CLIPS) if f.endswith(".wav"))
    out, acc = [], 0.0
    for name in reversed(names):
        info = sf.info(os.path.join(CLIPS, name))
        d = info.frames / info.samplerate
        if acc + d > target_s:
            break
        out.append(name)
        acc += d
    return sorted(out), acc


names, acc = pick_last()
print(f"后 4min 子集：{len(names)} 条 / {acc:.1f}s ({acc/60:.2f}min)")
print(f"  首={names[0]}  末={names[-1]}")

src = os.path.join(PROBE, "clips")
os.makedirs(src, exist_ok=True)
for n in names:
    shutil.copy2(os.path.join(CLIPS, n), os.path.join(src, n))

print("\n[1/3] 建集 ...", flush=True)
r = subprocess.run([PY, r"tts_trial\make_rvc_dataset.py", "--src", src, "--dst", DST],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
print((r.stdout or "").strip()[-400:], flush=True)
if r.returncode != 0:
    sys.exit(f"[FAIL] 建集：{(r.stderr or '')[-400:]}")

print("\n[2/3] 训练 40ep（同超参）...", flush=True)
t = subprocess.run([RVC_PY, r"D:\RVC\train_rvc_voice.py", "--exp", EXP,
                    "--dataset", DST, "--epochs", "40"],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
print(f"   退出码={t.returncode}", flush=True)
if t.returncode != 0:
    sys.exit(f"[FAIL] 训练：{(t.stderr or t.stdout or '')[-500:]}")

print("\n[3/3] 评测 ...", flush=True)
work = os.path.join(PROBE, "eval_work")
os.makedirs(work, exist_ok=True)
for f in os.listdir(os.path.join(BASE, "myvoice_rvc_full")):
    if f.startswith("in_") and f.endswith(".wav"):
        shutil.copy2(os.path.join(BASE, "myvoice_rvc_full", f), os.path.join(work, f))
pth = os.path.join(r"D:\RVC\assets\weights", f"{EXP}.pth")
index = next(iter(sorted(glob.glob(os.path.join(r"D:\RVC\logs", EXP, "added_*.index")))), "")
c = subprocess.run([PY, r"tts_trial\rvc_convert_full.py", "--pth", pth, "--index", index,
                    "--work", work, "--seed", SEED],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
if c.returncode != 0:
    sys.exit(f"[FAIL] 转换：{(c.stderr or '')[-400:]}")
ev = subprocess.run([PY, r"tts_trial\ab_eval_rvc_vs_qwen.py", "--dir", work,
                     "--out", os.path.join(work, "eval.json")],
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
if ev.returncode != 0:
    sys.exit(f"[FAIL] 评测：{(ev.stderr or '')[-400:]}")

d = json.load(open(os.path.join(work, "eval.json"), encoding="utf-8"))
rs = d["rows"]
n = len(rs)
row = dict(tag="last4min", minutes=round(acc / 60, 2),
           tts=sum(x["campp_after"] for x in rs) / n,
           delta=sum(x["campp_delta"] for x in rs) / n,
           asr=sum(x["asr_after"] for x in rs) / n,
           ceil=(d.get("ceiling") or {}).get("campp_after"))
print(f"\n后 4min：CAM++={row['tts']:.4f} Δ={row['delta']:+.4f} "
      f"可懂度={row['asr']:.3f} 天花板={row['ceil']:.4f}")
print("\n对照（seed=1234）：前 4min = 0.7935 / 8min = 0.8241 / 12.23min = 0.8356")
print("判读：若后 4min ≈ 前 4min(0.79) ⇒ 是数据量；若 ≈ 8min(0.82) ⇒ 是内容差异。")
with open(os.path.join(PROBE, "last4min.json"), "w", encoding="utf-8") as f:
    json.dump(row, f, ensure_ascii=False, indent=2)
