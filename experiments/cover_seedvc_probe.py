"""Seed-VC（零样本 SVC，44.1k BigVGAN）vs RVC 的带宽/谱结构对照。

用法：
    python experiments/cover_seedvc_probe.py            # 跑推理 + 度量
    python experiments/cover_seedvc_probe.py --measure  # 只重测已有产物

设计要点：

1. **只换转换器**：源和参照固定，差异才能归给架构。
2. **必须先把响度对齐再比频带**。第一版用 m1t 的绝对能量档（−40/−20/−8 dBFS），
   在整曲上成立，搬到 20 秒片段就崩了：Seed-VC 输出接近满刻度、RVC 干声小得多，
   同一个"silent 档"装的根本不是同一类内容（实测 RVC 有 71% 的帧被判成 silent，
   而它明明在唱）。这里改成：**按低频能量取前 50% 帧当"有声帧"，把它们统一拉到
   −20 dBFS，再比各频带绝对电平**。
3. **不做产品 post_chain**（treble +6dB @7k / deesser）：那层会同时抬高两边，
   掩盖掉"谁本来有高频"这个问题。
4. **加一条只换参照的对照**（全带宽参照）：用来判定"高频是模型先验给的，
   还是参照条件给的"。这是这一轮唯一能证伪的假设。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import cover_clip_attrib as M  # noqa: E402

SEEDVC = ROOT / "_ext" / "seed-vc"
OUT = ROOT / "media" / "covers" / "midsong_20261018" / "seedvc_probe"
#: 用户裁决里"最电 + 最夹嗓子"的那一段（C 段 320Hz 高音区）
SEG = (285.0, 20.0, "c_tail")
BANDS = [(200, 1000), (1000, 2000), (2000, 3000), (3000, 3500), (3500, 4000),
         (4000, 4500), (4500, 5000), (5000, 6000), (6000, 8000),
         (8000, 12000), (12000, 16000)]
#: 响度对齐的目标（0.2–3k 频带的中位电平）
ALIGN_DB = -20.0


def cut(src: Path, out: Path, start: float, dur: float) -> Path:
    y, sr = sf.read(str(src), dtype="float32", always_2d=True)
    y = y.mean(axis=1)
    a, b = int(start * sr), int((start + dur) * sr)
    if b > len(y):
        raise SystemExit(f"切片越界：{src.name} 只有 {len(y) / sr:.1f}s，要切到 {start + dur:.0f}s")
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), y[a:b], sr, subtype="PCM_16")
    return out


def run_seedvc(source: Path, ref: Path, out_dir: Path, steps: int = 30) -> Path:
    """调 seed-vc 的 CLI。--f0-condition True 是歌声模式的开关。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, "inference.py",
        "--source", str(source),
        "--target", str(ref),
        "--output", str(out_dir),
        "--diffusion-steps", str(steps),
        "--f0-condition", "True",
        "--semi-tone-shift", "0",
        "--fp16", "True",
    ]
    env = {**os.environ,
           "HF_ENDPOINT": "https://hf-mirror.com",
           "HF_HUB_DISABLE_SYMLINKS_WARNING": "1"}
    r = subprocess.run(cmd, cwd=str(SEEDVC), capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print(r.stdout[-3000:], file=sys.stderr)
        print(r.stderr[-3000:], file=sys.stderr)
        raise SystemExit(f"seed-vc 失败：{r.returncode}")
    got = sorted(out_dir.glob("*.wav"))
    if not got:
        raise SystemExit(f"seed-vc 没产物：{out_dir}")
    return got[0]


def analyze(y: np.ndarray, sr: int) -> dict:
    """响度对齐后的频带谱形 + 8–16k 结构。

    `bands` 是**对齐到 ALIGN_DB 之后的绝对电平**，所以跨文件可比。
    不返回"相对 0.2–3k 的比值"：那个量在安静帧上会被底噪抬亮，短片段尤其明显。
    """
    n, h = int(0.064 * sr), int(0.010 * sr)
    win = np.hanning(n)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    lo = (freqs >= 200) & (freqs < 3000)
    hi = (freqs >= 8000) & (freqs < 16000)
    masks = [(freqs >= a) & (freqs < b) for a, b in BANDS]

    elo, pows = [], []
    for s in range(0, len(y) - n, h):
        seg = y[s:s + n]
        p = np.abs(np.fft.rfft((seg - seg.mean()) * win)) ** 2
        elo.append(10 * np.log10(max(p[lo].sum(), 1e-20)))
        pows.append(p)
    elo = np.array(elo)
    pows = np.array(pows)
    # 有声帧 = 低频能量前 50%。用分位数而不是绝对阈值：不同文件的增益差几十 dB，
    # 绝对阈值一换素材就会把"在唱"判成"静音"。
    sel = elo >= np.percentile(elo, 50)
    gain = ALIGN_DB - 10 * np.log10(np.median(pows[sel][:, lo].sum(axis=1)))

    bands = {}
    for (a, b), m in zip(BANDS, masks):
        bands[f"{a}-{b}"] = round(float(np.median(
            10 * np.log10(np.maximum(pows[sel][:, m].sum(axis=1), 1e-20))) + gain), 1)

    hp = np.maximum(pows[sel][:, hi], 1e-24)
    return {
        "secs": round(len(y) / sr, 1),
        "sr": int(sr),
        "frames": int(sel.sum()),
        "bands": bands,
        "hf_db": round(float(np.median(10 * np.log10(hp.sum(axis=1))) + gain), 1),
        "sfm": round(float(np.median(
            np.exp(np.mean(np.log(hp), axis=1)) / np.maximum(np.mean(hp, axis=1), 1e-24))), 3),
        "crest": round(float(np.median(
            10 * np.log10(hp.max(axis=1) / np.maximum(np.median(hp, axis=1), 1e-24)))), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", action="store_true", help="跳过推理，只重测已有 wav")
    ap.add_argument("--ref", default=str(ROOT / "media/voicebank/myvoice_supp2026/reference.wav"))
    ap.add_argument("--fullband-ref",
                    default=str(ROOT / "media/voicebank/vbsyn_trial_ssb0033_60s/reference.wav"),
                    help="对照用全带宽参照：同一段源、只换参照，用来证明'高频来自参照条件'")
    args = ap.parse_args()

    start, dur, tag = SEG
    OUT.mkdir(parents=True, exist_ok=True)
    # 一律走绝对路径：subprocess 的 cwd 是 _ext/seed-vc，相对路径会解析到那儿去
    src = cut(M.REF, OUT / f"{tag}_source.wav", start, dur).resolve()
    ref = Path(args.ref).resolve()
    fb_ref = Path(args.fullband_ref).resolve()

    seed_out = OUT / f"{tag}_seedvc.wav"
    if not args.measure or not seed_out.exists():
        shutil.move(str(run_seedvc(src, ref, (OUT / "_raw").resolve())), str(seed_out))

    fb_out = OUT / f"{tag}_seedvc_fullband.wav"
    if fb_ref.exists() and (not args.measure or not fb_out.exists()):
        shutil.move(str(run_seedvc(src, fb_ref, (OUT / "_raw2").resolve())), str(fb_out))

    dry, _ = M.read(M.DRY)
    a = int(start * 40000)
    dry_seg = dry[a:a + int(dur * 40000)]

    sets = [
        ("源人声(赵雷)", *M.read(src)),
        ("参照音(你的8.9s,22k)", *M.read(ref)),
        ("RVC 干声", dry_seg, 40000),
        ("Seed-VC+你的参照", *M.read(seed_out)),
    ]
    if fb_out.exists():
        sets.append(("Seed-VC+全带宽参照", *M.read(fb_out)))

    rows = []
    print(f"\n=== 响度对齐到 0.2–3k = {ALIGN_DB:g}dB，有声帧取前 50%  "
          f"{tag} {start:.0f}-{start + dur:.0f}s ===")
    head = "".join(f"{a // 1000}-{b // 1000}k".rjust(9) for a, b in BANDS)
    print(f"{'':<24}{head}{'8-16k':>9}{'SFM':>7}{'crest':>7}")
    for name, y, sr in sets:
        r = analyze(y, sr)
        r["name"] = name
        rows.append(r)
        print(f"{name:<24}" + "".join(f"{r['bands'][f'{a}-{b}']:>9.1f}" for a, b in BANDS)
              + f"{r['hf_db']:>9.1f}{r['sfm']:>7.3f}{r['crest']:>7.1f}")

    # 声纹余弦：和 m1t 同一把尺子（CAM++）。注意参照音本身 8k 以上是空的，
    # 这把尺子对高频不敏感，只能横向比，不能当"像不像"的绝对判据。
    dry_wav = OUT / f"{tag}_rvc_dry.wav"
    sf.write(str(dry_wav), dry_seg, 40000, subtype="PCM_16")
    try:
        import cover_metrics

        for name, p in (("RVC 干声", dry_wav), ("Seed-VC+你的参照", seed_out)):
            got = cover_metrics.secs(p, Path(args.ref))
            for r in rows:
                if r["name"] == name:
                    r["secs_cos"] = got
            print(f"声纹余弦 {name:<20} {got}")
    except Exception as e:  # noqa: BLE001
        print(f"声纹余弦算不出：{e}")

    json.dump(rows, open(OUT / "probe.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"\n产物目录：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
