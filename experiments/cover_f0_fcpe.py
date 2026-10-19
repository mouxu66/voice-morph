# -*- coding: utf-8 -*-
"""换档 smoke：FCPE vs RMVPE（只换 F0 提取器，其他全部不动）。

为什么是它（2026-10-19）
    五条"换档"路里，48k 重训/DDSP/ContentVec 都要几十分钟到几小时；FCPE 是
    `infer/vc/pipeline.py` 已经支持的一行开关（`f0_method in ("pm","rmvpe","fcpe")`），
    10 分钟就能回答"歌声的 F0 跟得准不准"。先做最便宜的，再决定要不要动大的。

口径
    输入 = 原唱人声的同一段（25s），模型/音高/索引/seed 全同，只改 `f0_method`：
      · 音高跟随：与源逐帧半音差中位 / 八度错误率（|差|>6 半音）
      · 周期峰：`elec_ruler`（已修正 lag 归一）
      · 耗时：同一段音频的转换秒数
    听感页 `listen.html`（相对路径，不内嵌 base64）。

用法（RVC venv）：
    D:/RVC/.venv/Scripts/python.exe experiments/cover_f0_fcpe.py \
        --input "media/covers/lowsong_20261018/原曲_人声.wav" \
        --voice myvoice_supp2026 --start 20 --seconds 25 --out media/covers/f0method
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "m2_server", ROOT / "experiments"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

METHODS = ("rmvpe", "fcpe")


def _read_16k(path: Path, start: float, seconds: float):
    import numpy as np
    import soundfile as sf
    from math import gcd

    from scipy.signal import resample_poly

    d, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = d.mean(1)
    a, b = int(start * sr), int((start + seconds) * sr)
    mono = mono[a:b]
    g = gcd(16000, sr)
    y = resample_poly(mono.astype(np.float64), 16000 // g, sr // g).astype(np.float32)
    return np.ascontiguousarray(y)


def _pitch_stats(src40k: Path, out_wav: Path) -> dict:
    """与源逐帧比半音差（两声部都重采样到 22.05k 做 pyin）。"""
    import librosa
    import numpy as np

    def f0(path):
        y, _ = librosa.load(str(path), sr=22050, mono=True)
        f, vf, _ = librosa.pyin(np.asarray(y, dtype=np.float32), fmin=65, fmax=1000,
                                sr=22050, frame_length=2048, hop_length=256)
        return np.asarray(f, dtype=np.float64), np.asarray(vf, dtype=bool)

    fs, vs = f0(src40k)
    fo, vo = f0(out_wav)
    n = min(len(fs), len(fo))
    m = vs[:n] & vo[:n] & np.isfinite(fs[:n]) & np.isfinite(fo[:n])
    if not m.any():
        return {"frames": 0}
    semis = 12 * np.log2(fo[:n][m] / fs[:n][m])
    return {
        "frames": int(m.sum()),
        "semitone_median": round(float(np.median(semis)), 3),
        "semitone_p90_abs": round(float(np.percentile(np.abs(semis), 90)), 3),
        "octave_error_rate": round(float(np.mean(np.abs(semis) > 6)), 4),
    }


def _periodicity(path: Path) -> dict:
    import elec_ruler

    s = elec_ruler.summarise(elec_ruler.frame_stats(path))
    return {k: s.get(k) for k in ("frames", "f0_median", "periodicity_p10",
                                  "periodicity_p50", "periodicity_p90")}


def _page(out: Path, rows: list[dict], src_name: str) -> None:
    lines = ["<!doctype html><meta charset='utf-8'>",
             "<body style='font-family:system-ui;max-width:860px;margin:32px auto;line-height:1.6'>",
             "<h1>F0 提取器 A/B：RMVPE vs FCPE</h1>",
             f"<p>输入：{src_name}（同一段、同模型、同 pitch/index/seed，只换 f0_method）</p>",
             "<p><b>请只听两件事</b>：① 高音/转音有没有跑调或掉字；② 有没有多了毛刺。</p>"]
    for r in rows:
        lines.append(f"<h2>{r['method']}</h2>")
        lines.append(f"<audio controls src='{Path(r['wav']).name}'></audio>")
        lines.append("<pre>" + json.dumps({k: r[k] for k in r if k != 'wav'},
                                          ensure_ascii=False, indent=1) + "</pre>")
    lines.append("</body>")
    (out / "listen.html").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="FCPE vs RMVPE（只换 F0 提取器）")
    ap.add_argument("--input", required=True, type=Path)
    ap.add_argument("--voice", default="myvoice_supp2026")
    ap.add_argument("--start", type=float, default=20.0)
    ap.add_argument("--seconds", type=float, default=25.0)
    ap.add_argument("--pitch", type=int, default=0)
    ap.add_argument("--index-rate", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    src_path = args.input.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    import soundfile as sf

    import offline_vc_infer as ov

    ov.setup_env()
    pth = Path(ov.os.environ.get("VM_RVC_ROOT", r"D:\RVC")) / "assets" / "weights" / f"{args.voice}.pth"
    if not pth.exists():
        raise SystemExit(f"找不到 {pth}")
    log_dir = pth.parent.parent.parent / "logs" / args.voice
    index = next(iter(sorted(log_dir.glob("added_*.index"))), None)
    engine = ov.load_vc(str(pth), str(index) if index else "")

    y16 = _read_16k(src_path, args.start, args.seconds)
    # 源切片另存 40k，给音高对齐用（用引擎的目标采样率）
    import numpy as np
    from scipy.signal import resample_poly

    src40 = resample_poly(y16.astype(np.float64), engine.tgt_sr, 16000).astype(np.float32)
    src40_path = out / "00_source_slice_40k.wav"
    sf.write(str(src40_path), src40, engine.tgt_sr)

    rows = []
    for method in METHODS:
        t0 = time.time()
        y = ov.convert_audio(engine, y16.copy(), pitch=args.pitch,
                             index_rate=args.index_rate, seed=args.seed, f0_method=method)
        dt = time.time() - t0
        wav = out / f"cover_{method}.wav"
        sf.write(str(wav), y, engine.tgt_sr)
        row = {"method": method, "wav": str(wav), "convert_s": round(dt, 2),
               "pitch_track": _pitch_stats(src40_path, wav),
               "periodicity": _periodicity(wav)}
        rows.append(row)
        print(f"[f0] {method}: {dt:.1f}s  {row['pitch_track']}")

    (out / "f0_method.json").write_text(
        json.dumps({"input": str(src_path), "start": args.start, "seconds": args.seconds,
                    "voice": args.voice, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    _page(out, rows, src_path.name)
    print(f"[f0] → {out / 'listen.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
