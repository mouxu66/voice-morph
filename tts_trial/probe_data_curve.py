# -*- coding: utf-8 -*-
"""数据量学习曲线：同一份素材切 4 / 8 / 12.23 分钟三个子集，同超参各训一轮。

回答的问题：「再加录音素材，音色还能不能涨？」
  · 若 4min → 8min → 12.23min 单调上升且 12.23min 仍在涨 → **值得再录**；
  · 若 8min ≈ 12.23min（平台） → 这条素材的信息量已经吃满，
    再加同类素材收益递减，要涨得换**多样性**（话题/语速/情绪）而不是时长。

设计要点（都是踩过的坑）：
  · 子集**从同一份 clips24k/ 里取**，只改"数据量"这一个变量；
  · 取子集按**原切片顺序**（`ppt12_0001..`），不是随机抽 —— 保证时长可比、
    且覆盖演讲的不同段落（开头/中段/结尾）；
  · 每个子集都走同一个 `make_rvc_dataset.build()`（多相重采样 + 峰值归一 0.95）；
  · 训练超参与 12.23min 主模型**完全一致**（v2/40k/bs2/40ep/f0G40k）——
    否则比的是超参不是数据量。

用法：
    .venv\\Scripts\\python.exe tts_trial\\probe_data_curve.py --train
    .venv\\Scripts\\python.exe tts_trial\\probe_data_curve.py --eval
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

BASE = r"D:\变声\media\ft_work\ppt12"
CLIPS = os.path.join(BASE, "clips24k")
PROBE = os.path.join(BASE, "data_curve")
DATASET_RAW = r"D:\RVC\dataset_raw"
PY = r".venv\Scripts\python.exe"
RVC_PY = r"D:\RVC\.venv\Scripts\python.exe"
SEED = "1234"

#: (标签, 目标累计秒数) —— 4min / 8min 两档 + 已有的 12.23min 做参照
SUBSETS = [("4min", 240.0), ("8min", 480.0)]
REF_MIN = 12.23   # 已训好的全集（myvoice_ppt12_full），评测时读它的产物


def pick_clips(target_s):
    """按原顺序累加到 target_s 的切片列表（顺序取，不随机）。"""
    import soundfile as sf

    out, acc = [], 0.0
    for name in sorted(os.listdir(CLIPS)):
        if not name.endswith(".wav"):
            continue
        p = os.path.join(CLIPS, name)
        info = sf.info(p)
        d = info.frames / info.samplerate
        if acc + d > target_s:
            break
        out.append(name)
        acc += d
    return out, acc


def stage_inputs(tag, names):
    """把子集切片拷进 probe 目录（build 从这里读，保持 CLIPS 原样不动）。"""
    src = os.path.join(PROBE, tag, "clips")
    os.makedirs(src, exist_ok=True)
    for n in names:
        shutil.copy2(os.path.join(CLIPS, n), os.path.join(src, n))
    return src


def do_train():
    for tag, target in SUBSETS:
        names, acc = pick_clips(target)
        print(f"\n===== {tag}: {len(names)} 条 / {acc:.1f}s ({acc/60:.2f}min) =====", flush=True)
        src = stage_inputs(tag, names)
        dst = os.path.join(DATASET_RAW, f"myvoice_probe_{tag}_40k")
        r = subprocess.run([PY, r"tts_trial\make_rvc_dataset.py",
                            "--src", src, "--dst", dst],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        print(r.stdout.strip()[-500:] if r.stdout else "", flush=True)
        if r.returncode != 0:
            print(f"[FAIL] 建集失败：{(r.stderr or '')[-500:]}")
            continue
        exp = f"myvoice_probe_{tag}"
        print(f"→ 训练 {exp}（40ep，与主模型同超参）...", flush=True)
        t = subprocess.run([RVC_PY, r"D:\RVC\train_rvc_voice.py",
                            "--exp", exp, "--dataset", dst, "--epochs", "40"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        tail = " | ".join((t.stdout or "").strip().splitlines()[-3:])
        print(f"   退出码={t.returncode}  {tail[-300:]}", flush=True)


def do_eval():
    import soundfile as sf  # noqa: F401

    rows = []
    for tag, _ in SUBSETS + [("full", None)]:
        if tag == "full":
            work = os.path.join(BASE, "myvoice_rvc_full")
            pth = r"D:\RVC\assets\weights\myvoice_ppt12_full.pth"
            index = (r"D:\RVC\logs\myvoice_ppt12_full"
                     r"\added_IVF817_Flat_nprobe_1_myvoice_ppt12_full_v2.index")
            minutes = REF_MIN
        else:
            work = os.path.join(PROBE, tag, "eval_work")
            os.makedirs(work, exist_ok=True)
            for f in os.listdir(os.path.join(BASE, "myvoice_rvc_full")):
                if f.startswith("in_") and f.endswith(".wav"):
                    shutil.copy2(os.path.join(BASE, "myvoice_rvc_full", f),
                                 os.path.join(work, f))
            pth = os.path.join(r"D:\RVC\assets\weights", f"myvoice_probe_{tag}.pth")
            idx_dir = os.path.join(r"D:\RVC\logs", f"myvoice_probe_{tag}")
            index = next(iter(sorted(
                __import__("glob").glob(os.path.join(idx_dir, "added_*.index")))), "")
            # 报告由 make_rvc_dataset 写到 dataset_raw/<dst名>_report.json，
            # 而 dst 名带 `_40k` 后缀（曾漏掉它 → minutes 读成 0）
            rep = os.path.join(DATASET_RAW, f"myvoice_probe_{tag}_40k_report.json")
            minutes = json.load(open(rep, encoding="utf-8"))["total_min"] if os.path.exists(rep) else 0
            if not minutes:
                print(f"[warn] {tag}: 读不到建集报告（{rep}），时长记 0")
            if not os.path.exists(pth):
                print(f"[skip] {tag}: 缺权重 {pth}")
                continue

        print(f"\n===== {tag} ({minutes}min) =====", flush=True)
        c = subprocess.run([PY, r"tts_trial\rvc_convert_full.py", "--pth", pth,
                            "--index", index, "--work", work, "--seed", SEED],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if c.returncode != 0:
            print(f"[FAIL] 转换：{(c.stderr or '')[-400:]}")
            continue
        ev = subprocess.run([PY, r"tts_trial\ab_eval_rvc_vs_qwen.py", "--dir", work,
                             "--out", os.path.join(work, "eval.json")],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if ev.returncode != 0:
            print(f"[FAIL] 评测：{(ev.stderr or '')[-400:]}")
            continue
        d = json.load(open(os.path.join(work, "eval.json"), encoding="utf-8"))
        rs = d["rows"]
        n = len(rs)
        row = dict(tag=tag, minutes=minutes,
                   tts_cam=sum(x["campp_after"] for x in rs) / n,
                   tts_delta=sum(x["campp_delta"] for x in rs) / n,
                   tts_asr=sum(x["asr_after"] for x in rs) / n,
                   ceil=(d.get("ceiling") or {}).get("campp_after"))
        rows.append(row)
        print(f"   → CAM++={row['tts_cam']:.4f} Δ={row['tts_delta']:+.4f} "
              f"可懂度={row['tts_asr']:.3f} 天花板={row['ceil']:.4f}", flush=True)

    if len(rows) >= 2:
        print("\n" + "=" * 68)
        print(f"{'数据量':<12}{'TTS→RVC':>10}{'Δ':>10}{'可懂度':>9}{'天花板':>10}")
        print("-" * 68)
        for r in sorted(rows, key=lambda x: x["minutes"]):
            print(f"{r['tag'] + ' (' + str(r['minutes']) + 'min)':<12}"
                  f"{r['tts_cam']:>10.4f}{r['tts_delta']:>+10.4f}"
                  f"{r['tts_asr']:>9.3f}{r['ceil']:>10.4f}")
        s = sorted(rows, key=lambda x: x["minutes"])
        print("\n相邻档的变化：")
        for a, b in zip(s, s[1:]):
            dm = b["minutes"] - a["minutes"]
            dcam = b["tts_cam"] - a["tts_cam"]
            if dm > 0:
                print(f"  +{dm:.1f}min: TTS {dcam:+.4f}（每 +1min 约 {dcam/dm:+.5f}）"
                      f"   天花板 {(b['ceil'] - a['ceil']):+.4f}")
            else:
                # 报告缺失导致 minutes=0 时，仍报绝对变化（别因此不打印）
                print(f"  {a['tag']}→{b['tag']}: TTS {dcam:+.4f}"
                      f"   天花板 {(b['ceil'] - a['ceil']):+.4f}"
                      f"   ⚠ 时长未读到（报告缺失），无法算每-min 斜率")
    with open(os.path.join(PROBE, "data_curve.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"\n[done] {os.path.join(PROBE, 'data_curve.json')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--eval", action="store_true")
    a = ap.parse_args()
    os.makedirs(PROBE, exist_ok=True)
    if a.train:
        do_train()
    if a.eval:
        do_eval()
    if not (a.train or a.eval):
        print(__doc__)


if __name__ == "__main__":
    raise SystemExit(main())
