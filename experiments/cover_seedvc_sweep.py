"""Seed-VC 参照窗口扫描：同一个模型，只换「拿他哪 25 秒当参照」和 CFG/步数。

为什么值得扫
    m1y 的结论是「参照音选错了对象」，但换成他的真唱后，Seed-VC 只用了**文件前 25 秒**
    （`inference.py` 里 `ref_audio[:sr * 25]`）。而 `唱歌.m4a` 前 25 秒中位 129Hz、
    37–62 秒中位 149.5Hz —— 前 25 秒恰好在低音区。m1x 已经证明「参照音区低于输出
    音区 ⇒ 童声/夹嗓子」，所以参照窗口本身就是一个没扫过的变量。

实测（《成都》285–305s，锚=他的真唱，CAM++）

    ref 0–25   cfg0.7/30  → 0.721   r_env 0.36 slope 1.00
    ref 37–62  cfg0.7/30  → 0.799   r_env 0.17 slope 0.46
    ref 37–62  cfg1.0/50  → 0.800   r_env 0.36 slope 0.75
    ref 37–62 去静音拼接 cfg1.0/50 → 0.816   r_env 0.28 slope 0.73
    第二段源（100–120s）换参照：0.644 → 0.798（不是那一段的特例）

用法
    .venv/Scripts/python.exe experiments/cover_seedvc_sweep.py           # 缺的跑、全量度量
    .venv/Scripts/python.exe experiments/cover_seedvc_sweep.py --measure # 只度量
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import cover_clip_attrib as M  # noqa: E402
import cover_hf_tracking as T  # noqa: E402
import cover_seedvc_probe as P  # noqa: E402
import cover_voice_ab as AB  # noqa: E402

BASE = ROOT / "media" / "covers" / "midsong_20261018" / "seedvc_sweep"
SING = BASE.parent / "voice_ab" / "_my_sing_44k.wav"
SRC = BASE.parent / "register" / "src.wav"
ANCHOR = BASE.parent / "voice_ab" / "anchor_my_sing.wav"

#: (key, 参照窗口 / "concat", cfg, 步数)
ARMS = [
    ("ref00_cfg07_s30", (0.0, 25.0), 0.7, 30),
    ("ref37_cfg07_s30", (37.0, 62.0), 0.7, 30),
    ("ref37_cfg10_s50", (37.0, 62.0), 1.0, 50),
    ("ref45_cfg07_s30", (45.0, 70.0), 0.7, 30),
    ("concat_cfg10_s50", "concat", 1.0, 50),
]
#: 拼接窗口（与 ref37 同段，去掉停顿）
CONCAT_WINDOW = (37.0, 62.0)
CONCAT_TOP_DB = 32.0
CONCAT_MAX_S = 24.5


def make_ref(y: np.ndarray, sr: int, spec, top_db: float = CONCAT_TOP_DB) -> np.ndarray:
    """切/拼参照。`spec` 是 (start, end) 秒，或 "concat" = 同段里去掉静音。"""
    if spec == "concat":
        a, b = int(CONCAT_WINDOW[0] * sr), int(CONCAT_WINDOW[1] * sr)
        seg = np.asarray(y[a:b], dtype=np.float32)
        keep, total, need = [], 0, int(CONCAT_MAX_S * sr)
        for s, e in librosa.effects.split(seg, top_db=top_db, frame_length=2048, hop_length=512):
            if e - s < int(0.15 * sr):
                continue
            if total + (e - s) > need:
                break
            keep.append(seg[s:e])
            total += e - s
        return np.concatenate(keep) if keep else seg
    a, b = int(spec[0] * sr), int(spec[1] * sr)
    return np.asarray(y[a:b], dtype=np.float32)


def ensure_arm(key: str, ref: Path, cfg: float, steps: int) -> Path:
    dst = BASE / f"{key}.wav"
    if dst.exists() and dst.stat().st_mtime >= ref.stat().st_mtime:
        return dst
    tmp = BASE / f"_run_{key}"
    shutil.rmtree(tmp, ignore_errors=True)
    got = Path(P.run_seedvc(SRC, ref, tmp, steps=steps, cfg=cfg))
    shutil.copy2(got, dst)
    shutil.rmtree(tmp, ignore_errors=True)
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", action="store_true", help="跳过生成，只度量")
    args = ap.parse_args()

    BASE.mkdir(parents=True, exist_ok=True)
    y, sr = sf.read(str(SING), dtype="float32")
    y = y.mean(axis=1) if y.ndim > 1 else np.asarray(y, dtype=np.float32)

    rows = []
    for key, spec, cfg, steps in ARMS:
        ref = BASE / f"{key}_ref.wav"
        sf.write(str(ref), make_ref(y, sr, spec), sr, subtype="PCM_16")
        path = BASE / f"{key}.wav"
        if not args.measure:
            path = ensure_arm(key, ref, cfg, steps)
        r = AB.measure(path, ANCHOR)
        r.update({k: v for k, v in T.tracking(*M.read(path)).items()
                  if k in ("r_env", "slope", "gap_drop_db")})
        r.update({"key": key, "cfg": cfg, "steps": steps,
                  "ref": spec if spec == "concat" else list(spec)})
        rows.append(r)
        print(f"{key:<18}{r['f0']:>6.0f}Hz cam {r['cam_anchor']:.3f}  "
              f"hf {r['hf_db']:>6.1f}  r_env {r['r_env']:.2f} slope {r['slope']:.2f} "
              f"gap {r['gap_drop_db']:.1f}")

    (BASE / "sweep_metrics.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n指标：{BASE / 'sweep_metrics.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
