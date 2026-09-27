"""Seed-VC f0 歌声链路：`semi_tone_shift` 扫描（默认 0~9）。

## 为什么单独一个脚本

2026-09-27 那轮寻优用的是 **`inference.py`（f0 版）**，而产品里 `m2_server/seed_vc.py`
走的是 `inference_v2.py`（config 两处 `f0_condition: false`，结构上唱不了歌）。
`experiments/seedvc_param_sweep.py` 调的是 v2，参数面完全对不上 —— 所以这里另起一个，
调 f0 版。

## 这一步要回答什么

`auto_f0_adjust=True` 会把源 F0 曲线整体平移到**参考音自己的中位音高**，而
`semi_tone_shift` 是在这条已经贴到参考音的曲线上**再乘**一个半音系数
（见 `inference.py` 的 `shifted_log_f0_alt`）。也就是说：

    参考音的音高 = 模型"有音色证据"的地方；shift 每 +1 半音，就是往外推一格。

上一轮只测了 **0** 和 **10~15**，中间 **1~9 是空的** —— 而参考音 133.5Hz、原唱 275.7Hz
差 13.5 半音，往回转 1.5 半音正好是**一个整八度（−12.0）**。本脚本就是去补这个洞：
找「整八度降调 + 音色接近满格」的点。

## 固定量（沿用 2026-09-27 已标定的最优）

`--diffusion-steps 80 --inference-cfg-rate 1.2 --length-adjust 1.0
--f0-condition True --auto-f0-adjust True --fp16 True`

## 测量口径（★ 多点采样，不是单点）

单点采样会掩盖「开头崩」：上一轮只测 40–70s 得出「音色 0.571 没损失」，
全量一测开头 0–20s 只有 0.25。所以这里固定 4 个窗口
（0–20 / 60–80 / 150–170 / 240–260s）分别出分，整曲均值只作对照。

指标：
  - `sim_ref`：输出窗口 vs **参考音** 的 CAM++ 余弦（音色像不像袋鼠，越高越好）
  - `sim_src`：输出窗口 vs **源干声** 的 CAM++ 余弦（漏源，越低越干净）
  - `pitch_dev`：输出窗口中位 F0 相对**源同窗口**中位 F0 的半音偏差

⚠️ CAM++ 是**说话人验证**模型，被训练成对音高/发声方式不敏感 —— 它量的是「这是谁」，
不是「他是怎么发声的」。所以「拔高导致的夹嗓子」它**可能给高分**。
本脚本的数字只能排序音色相似度，夹嗓子要看 HNR / 谱倾斜这类指标 + 人耳，
不要只看 `sim_ref` 下结论。

## 跑法

```bash
# 全扫描（10 档，约 3 分钟/档）
.venv/Scripts/python.exe experiments/seedvc_shift_sweep.py \\
    --source <干声>.wav --target <参考音>.wav

# 只测量已有产物（不跑推理）—— 用来跟历史数字对账
.venv/Scripts/python.exe experiments/seedvc_shift_sweep.py --measure-only \\
    --source <干声>.wav --target <参考音>.wav \\
    --measure-dir D:/tmp/kangaroo_vc/final_s11
```

产出：`<out-dir>/s<NN>_*.wav`（保留推理原名里的参数编码）+ `results.json` + `report.md`
"""
import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from math import log2
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEEDVC_REPO = ROOT / "seed_vc_repo"
INFER_F0 = SEEDVC_REPO / "inference.py"       # ★ f0 版，不是 v2
PY = ROOT / ".venv" / "Scripts" / "python.exe"
M2 = ROOT / "m2_server"

DEFAULT_TARGET = ROOT / "media" / "rvc_dataset" / "meituan_rat_002_orig.wav"
DEFAULT_OUT = ROOT / "outputs" / "seedvc_shift_sweep"

# 固定量：2026-09-27 已标定的最优（见 memory/2026-09-27.md）
STEPS = 80
CFG_RATE = 1.2
LENGTH_ADJUST = 1.0
SHIFTS = list(range(10))

# 多点采样窗口（秒）；单点会掩盖「开头崩」
WINDOWS = [(0, 20), (60, 80), (150, 170), (240, 260)]
SR16 = 16000
# 「整八度降调」的目标音高偏差
OCTAVE = -12.0


# ---------- 纯函数（可单测）----------

_SHIFT_RE = re.compile(r"(?:^|[^0-9a-z])s(\d{1,2})(?:[^0-9]|$)")


def guess_shift(path: Path) -> int | None:
    """从文件名或父目录名里把 shift 抠出来（`final_s11/` 与 `s03_xxx.wav` 两种写法）。

    只给 `--measure-only` 对账历史产物用：那份产物按档分了子目录，档号只在路径里，
    不把它读出来就只能得到一堆同名的 `vc_vocals_...`，无法排序。
    上限卡 24 —— 否则 `steps80` 里的 `s80` 会被误读成 shift=80。
    """
    for cand in (path.parent.name, path.stem):
        m = _SHIFT_RE.search(cand)
        if m:
            v = int(m.group(1))
            if 0 <= v <= 24:
                return v
    return None

def semitones(f_from: float, f_to: float) -> float:
    """f_to 相对 f_from 的半音偏差；任一频率非正时返回 0（无法判定）。"""
    if f_from <= 0 or f_to <= 0:
        return 0.0
    return 12.0 * log2(f_to / f_from)


def window_slices(duration_s: float, sr: int = SR16) -> list[tuple[float, int, int]]:
    """把窗口表裁到实际时长内，返回 [(起点秒, 起样本, 止样本)]。

    时长不够的窗口直接丢掉 —— 宁可少一个点，也不要拿静音凑数。
    """
    out = []
    for t0, t1 in WINDOWS:
        if duration_s < t1:
            continue
        out.append((float(t0), int(t0 * sr), int(t1 * sr)))
    return out


def pick_octave_candidate(rows: list[dict]) -> dict | None:
    """在整八度降调附近挑「音色最高」的一档。

    判据：先把音高偏差离 −12 半音最近的那些（容差 1 半音）挑出来，
    再在其中取 sim_ref 最大者。没有落进容差的就返回 None（别硬凑）。
    """
    if not rows:
        return None
    with_dev = [r for r in rows if "pitch_dev_mean" in r]
    if not with_dev:
        return None
    near = [r for r in with_dev if abs(r["pitch_dev_mean"] - OCTAVE) <= 1.0]
    if not near:
        return None
    return max(near, key=lambda r: r["sim_ref_whole"])


# ---------- 推理 ----------

def build_cmd(shift: int, src: Path, tgt: Path, out_tmp: Path) -> list[str]:
    """拼 f0 版推理命令行（纯函数，便于测试锁死三个「不写就静默失效」的旗标）。

    `--f0-condition` / `--auto-f0-adjust` 缺任一个，跑出来的就不是歌声链路而是念白，
    而指标（CAM++）**不会报错**，只会悄悄换成另一条分支的结果 —— 必须钉住。
    """
    return [
        str(PY), str(INFER_F0),
        "--source", str(src), "--target", str(tgt), "--output", str(out_tmp),
        "--diffusion-steps", str(STEPS),
        "--inference-cfg-rate", str(CFG_RATE),
        "--length-adjust", str(LENGTH_ADJUST),
        "--f0-condition", "True",
        "--auto-f0-adjust", "True",
        "--semi-tone-shift", str(shift),
        "--fp16", "True",
    ]


def convert(shift: int, src: Path, tgt: Path, work_dir: Path) -> Path:
    """跑一次 f0 版 inference.py，返回它在临时目录里产出的 wav。

    每次用独立临时目录：会话目录里躺着上一档时，「这次没产出」会被残留伪装成成功
    （见 docs/在线扒歌-配置与边界.md 的同类坑）。
    """
    tmp = work_dir / f"_tmp_shift{shift:02d}"
    tmp.mkdir(parents=True, exist_ok=True)
    cmd = build_cmd(shift, src, tgt, tmp)
    env = dict(os.environ)
    # 权重已全部就位：走离线，否则每次启动都要联网 HEAD 探测（卡几十秒）
    env["HF_HUB_OFFLINE"] = "1"
    env["HF_HUB_ETAG_TIMEOUT"] = "60"
    env["HF_HUB_DOWNLOAD_TIMEOUT"] = "180"
    env["PYTORCH_ALLOC_CONF"] = "expandable_segments:True"
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                       encoding="utf-8", errors="replace",
                       cwd=str(SEEDVC_REPO), env=env)
    wavs = sorted(tmp.glob("*.wav"))
    if r.returncode != 0 or not wavs:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError(f"[shift={shift}] Seed-VC f0 失败: {' | '.join(tail)[-500:]}")
    return wavs[0]


# ---------- 测量 ----------

def _median_f0(y, sr: int) -> float:
    """有声段中位 F0（Hz）；判不出返回 0。"""
    import librosa
    import numpy as np

    if y is None or len(y) < sr // 2:
        return 0.0
    f0, vflag, _ = librosa.pyin(y, fmin=60, fmax=400, sr=sr, frame_length=1024)
    f0 = f0[vflag]
    f0 = f0[~np.isnan(f0)]
    if len(f0) < 5:
        return 0.0
    return float(np.median(f0))


def _cos(a, b) -> float:
    import numpy as np

    if a is None or b is None:
        return 0.0
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def measure(out_wav: Path, emb_ref, src16, src_f0_win: dict) -> dict:
    """出一个产物的全套数字（整曲音色 + 4 窗口音色/音高）。

    ⚠️ **不整曲算 F0**：`librosa.pyin` 在 271s×44.1kHz 上是分钟级开销，而
    「整曲音高」本身是个假指标 —— 历史那张表的「音高偏差」实测就是
    **各分段偏差的均值**（E_shift15 逐段 −1.5/+0.41/+0.08/+2.16 → +0.29 ≒ 表里的 +0.30）。
    所以这里用同一口径：`pitch_dev_mean` = 各窗口偏差均值，便宜且与历史可比。
    """
    sys.path.insert(0, str(M2))
    import speaker_sep  # 项目自己的 CAM++（与切片质检/说话人分离同一实现）

    y16 = speaker_sep._read16k(out_wav)
    dur = len(y16) / SR16

    # 整曲音色 vs 参考音 / 漏源 vs 干声（CAM++ 一次嵌入，开销小）
    emb_out = speaker_sep._sv_embed(y16)
    row = {
        "file": str(out_wav),
        "sim_ref_whole": round(_cos(emb_out, emb_ref), 3),
        "sim_src_whole": round(_cos(emb_out, speaker_sep._sv_embed(src16)), 3),
        "duration_s": round(dur, 2),
        "windows": [],
    }

    for t0, a, b in window_slices(dur):
        seg = y16[a:b]
        f0_src = src_f0_win[f"{t0:.0f}s"]
        f0_out = _median_f0(seg, SR16)
        row["windows"].append({
            "t": f"{t0:.0f}s",
            "sim_ref": round(_cos(speaker_sep._sv_embed(seg), emb_ref), 3),
            "sim_src": round(_cos(speaker_sep._sv_embed(seg), speaker_sep._sv_embed(src16[a:b])), 3),
            "f0_src": round(f0_src, 1),
            "f0_out": round(f0_out, 1),
            "pitch_dev": round(semitones(f0_src, f0_out), 2),
        })
    devs = [w["pitch_dev"] for w in row["windows"]]
    row["pitch_dev_mean"] = round(sum(devs) / len(devs), 2) if devs else 0.0
    row["sim_ref_mean"] = (round(sum(w["sim_ref"] for w in row["windows"]) / len(devs), 3)
                           if devs else 0.0)
    return row


def source_baseline(src: Path) -> tuple[object, dict, float]:
    """源干声的 16k 波形 + 各窗口中位 F0。

    整曲中位 F0 只算这一次（列表头要用），产物侧一律走窗口均值 —— 见 measure() 的说明。
    """
    sys.path.insert(0, str(M2))
    import speaker_sep

    src16 = speaker_sep._read16k(src)
    dur = len(src16) / SR16
    f0s = {}
    for t0, a, b in window_slices(dur):
        f0s[f"{t0:.0f}s"] = _median_f0(src16[a:b], SR16)
    return src16, f0s, dur


# ---------- 报告 ----------

def build_report(meta: dict, rows: list[dict]) -> str:
    win_cols = [w["t"] for w in rows[0]["windows"]] if rows and rows[0]["windows"] else []
    lines = [
        "# Seed-VC f0 歌声链路 · semi_tone_shift 扫描报告",
        "",
        f"- 源干声：`{Path(meta['source']).name}`（{meta['source_dur_s']}s）",
        f"- 参考音：`{Path(meta['target']).name}`（中位 F0 {meta['f0_ref_whole']}Hz）",
        f"- 源干声各窗口中位 F0 均值：**{meta['f0_src_mean']}Hz**；与参考音差 "
        f"**{round(semitones(meta['f0_src_mean'], meta['f0_ref_whole']), 1)} 半音**",
        f"- 固定量：steps={meta['steps']} cfg_rate={meta['cfg_rate']} "
        f"length_adjust={meta['length_adjust']} f0_condition=True auto_f0_adjust=True",
        "",
        "## 音色与音高（整曲 + 分窗）",
        "",
        "| shift | 音色(整曲)↑ | 漏源(整曲)↓ | 音高偏差(均值) | 音色(窗口均值) | "
        + " | ".join(f"音色{w}↑" for w in win_cols) + " |",
        "|" + "---|" * (5 + len(win_cols)),
    ]
    for r in rows:
        cells = [f"{r['shift']}", f"{r['sim_ref_whole']:.3f}", f"{r['sim_src_whole']:.3f}",
                 f"{r['pitch_dev_mean']:+.2f}", f"{r.get('sim_ref_mean', 0):.3f}"]
        cells += [f"{w['sim_ref']:.3f}" for w in r["windows"]]
        lines.append("| " + " | ".join(cells) + " |")

    lines += [
        "",
        "## 分窗音高偏差（半音，相对源同窗）",
        "",
        "| shift | " + " | ".join(win_cols) + " | 窗口内最低 |",
        "|" + "---|" * (len(win_cols) + 2),
    ]
    for r in rows:
        devs = [w["pitch_dev"] for w in r["windows"]]
        cells = [f"{r['shift']}"] + [f"{d:+.2f}" for d in devs]
        cells.append(f"{min(devs):+.2f}" if devs else "—")
        lines.append("| " + " | ".join(cells) + " |")

    best = pick_octave_candidate(rows)
    top = max(rows, key=lambda r: r["sim_ref_whole"]) if rows else None
    lines += ["", "## 结论", ""]
    if top:
        lines.append(
            f"- **音色最高**：shift={top['shift']}（整曲 `sim_ref` {top['sim_ref_whole']:.3f}，"
            f"音高偏差 {top['pitch_dev_mean']:+.2f}）")
    if best:
        lines.append(
            f"- **整八度降调附近音色最好**：shift={best['shift']}"
            f"（音高偏差 {best['pitch_dev_mean']:+.2f}，`sim_ref` {best['sim_ref_whole']:.3f}）"
            f" ← 若目标是「降一个整八度且音色损失最小」，选这一档")
    else:
        lines.append(f"- ⚠️ 没有任何一档落在整八度（{OCTAVE:+.0f} 半音）±1 半音内 —— "
                     "说明该区间要么不在本次扫描范围，要么整体偏移不在预期位置，别硬凑。")
    lines += [
        "",
        "## 怎么读",
        "- `音色(整曲)↑`：与**参考音**的 CAM++ 余弦，越高越像袋鼠。",
        "- `漏源(整曲)↓`：与**源干声**的余弦，越低说明原唱残留越少。",
        "- `音高偏差(均值)`：**各窗口中位 F0 相对源同窗的半音数的均值**（与历史那张表同口径，"
        "实测 E_shift15 逐段均值 +0.29 ≒ 表里 +0.30）；−12 即降一个整八度。",
        "- ⚠️ 本次**没有**整曲单指标：271s 上 `librosa.pyin` 是分钟级开销，且整曲均值会"
        "重新掩盖「开头崩」——分窗才是有效信息。",
        "- ⚠️ **CAM++ 量的是「这是谁」，不是「他是怎么发声的」** —— 它是说话人验证模型，"
        "被训练成对音高/发声方式不敏感，所以「夹嗓子」它可能给高分。"
        "排序用它，最终判决必须人耳 + HNR/谱倾斜。",
        f"- 试听：`{meta['out_dir']}` 下 `s<NN>_*.wav`，建议按 shift 顺序盲听。",
        "",
    ]
    return "\n".join(lines) + "\n"


# ---------- 入口 ----------

def parse_args():
    ap = argparse.ArgumentParser(description="Seed-VC f0 歌声链路 semi_tone_shift 扫描")
    ap.add_argument("--source", required=True, help="源干声 wav（如歌曲 demucs 人声轨）")
    ap.add_argument("--target", default=str(DEFAULT_TARGET), help="目标音色参考音")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--shifts", default=",".join(str(s) for s in SHIFTS),
                    help="逗号分隔的 semi_tone_shift 列表，默认 0..9")
    ap.add_argument("--steps", type=int, default=STEPS)
    ap.add_argument("--cfg-rate", type=float, default=CFG_RATE)
    ap.add_argument("--length-adjust", type=float, default=LENGTH_ADJUST)
    ap.add_argument("--reuse", action="store_true", help="已存在的产物跳过推理，只重测")
    ap.add_argument("--measure-only", action="store_true", help="不跑推理")
    ap.add_argument("--measure-dir", default="",
                    help="--measure-only 时要测量的目录（walk 出所有 *.wav）")
    return ap.parse_args()


def main():
    global STEPS, CFG_RATE, LENGTH_ADJUST
    # Windows 控制台默认 GBK，报告里的 ⚠️/箭头会直接把 print 炸掉（UnicodeEncodeError）
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    args = parse_args()
    STEPS, CFG_RATE, LENGTH_ADJUST = args.steps, args.cfg_rate, args.length_adjust

    src, tgt = Path(args.source).resolve(), Path(args.target).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        raise SystemExit(f"源干声不存在: {src}")
    if not tgt.exists():
        raise SystemExit(f"参考音不存在: {tgt}")
    # ★ inference.py 以 cwd=seed_vc_repo 启动，相对路径在那边解不开 → 一律绝对化

    sys.path.insert(0, str(M2))
    import speaker_sep

    print("=== 基线 ===", flush=True)
    src16, src_f0, src_dur = source_baseline(src)
    emb_ref = speaker_sep._sv_embed(speaker_sep._read16k(tgt))
    import soundfile as sf
    d_ref, sr_ref = sf.read(str(tgt), dtype="float32")
    if d_ref.ndim == 2:
        d_ref = d_ref[:, 0]
    f0_ref_whole = _median_f0(d_ref, int(sr_ref))
    src_f0_vals = [v for v in src_f0.values() if v > 0]
    meta = {
        "source": str(src), "target": str(tgt), "out_dir": str(out_dir),
        "steps": STEPS, "cfg_rate": CFG_RATE, "length_adjust": LENGTH_ADJUST,
        "source_dur_s": round(src_dur, 1),
        "f0_src_mean": round(sum(src_f0_vals) / len(src_f0_vals), 1) if src_f0_vals else 0.0,
        "f0_src_windows": {k: round(v, 1) for k, v in src_f0.items()},
        "f0_ref_whole": round(f0_ref_whole, 1),
        "windows_s": WINDOWS,
        "note": "CAM++ 是说话人验证模型，对发声方式（夹嗓子）不敏感，只能排序音色相似度",
    }
    print(json.dumps(meta, ensure_ascii=False), flush=True)

    # 收集待测 (tag, shift, path)
    jobs = []
    if args.measure_only:
        if not args.measure_dir:
            raise SystemExit("--measure-only 需要 --measure-dir")
        for p in sorted(Path(args.measure_dir).rglob("*.wav")):
            jobs.append((p.parent.name + "/" + p.name, guess_shift(p), p))
        if not jobs:
            raise SystemExit(f"--measure-dir 下没有 wav: {args.measure_dir}")
    else:
        shifts = [int(s) for s in args.shifts.split(",") if s.strip() != ""]
        for sh in shifts:
            # 先看有没有历史同档产物可复用（名字里带 shift 前缀）
            existing = sorted(out_dir.glob(f"s{sh:02d}_*.wav"))
            if existing and args.reuse:
                print(f"[shift={sh}] 复用 {existing[0].name}", flush=True)
                jobs.append((f"shift{sh:02d}", sh, existing[0]))
                continue
            print(f"[shift={sh}] 推理中…", flush=True)
            t0 = time.time()
            produced = convert(sh, src, tgt, out_dir)
            # ★ 保留推理原名（内含 length/steps/cfg 编码）再加 shift 前缀，便于溯源
            named = out_dir / f"s{sh:02d}_{produced.name}"
            shutil.move(str(produced), str(named))
            shutil.rmtree(produced.parent, ignore_errors=True)
            print(f"[shift={sh}] 完成，{time.time() - t0:.0f}s -> {named.name}", flush=True)
            jobs.append((f"shift{sh:02d}", sh, named))

    rows = []
    for tag, sh, path in jobs:
        print(f"=== 测量 {tag} ===", flush=True)
        row = measure(path, emb_ref, src16, src_f0)
        row["shift"] = sh if sh is not None else tag
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    numbered = [r for r in rows if isinstance(r["shift"], int)]
    numbered.sort(key=lambda r: r["shift"])
    report = build_report(meta, numbered or rows)
    (out_dir / "results.json").write_text(
        json.dumps({"meta": meta, "results": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print("\n" + report, flush=True)
    print(f"OK -> {out_dir / 'report.md'}", flush=True)


if __name__ == "__main__":
    main()
