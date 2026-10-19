# -*- coding: utf-8 -*-
"""电音归因：把「请求的输出音高」当唯一变量，看毛刺是否随它涨（在 RVC venv 里跑）。

假设（可证伪）
    模型训练时见过的音高极窄 —— 实测 64,851 个浊音帧里 p99=173.7Hz、p99.9=218.5Hz，
    >250Hz 只有 **1 帧**；而《唯一》中位 275.5Hz、p90 415Hz。
    RVC 的条件是 `emb_pitch[256档]`（`infer/module/models.py:40`），
    所以"请求到没训过的档位"= 用**别人/底模**那一档的嵌入去驱动一个只学了低音的声码器。

设计（单变量）
    输入用**模型自己的训练素材**（域内，p50≈108Hz），只改 `pitch`：
        0 / +5 / +7 / +12 / +16 / +19   ⇒ 输出中位 F0 ≈ 108 / 144 / 161 / 216 / 272 / 323 Hz
    另出**机械变调对照**（同一段做多相重采样，不过模型）—— 它应该**不随音高劣化**；
    若 RVC 臂劣化而对照臂不劣化，就把"劣化"钉在模型上、而不是"音高本身难"。

为什么要域内输入
    跨域（歌声）本来就难，会把"模型没训过高音"和"模型没训过唱歌"混在一起。
    域内输入只剩"音高"这一个变量。

★ 2026-10-19 追加 `--audio`：上一轮台阶用的是**说话**切片，而用户听到电音是在**唱歌**上 ——
  那其实是两个变量（音高 + 素材类型）。用 `--audio` 把同一套台阶跑在**唱歌**素材上，
  才能回答"电音到底跟音高走、还是跟唱歌走"。两条都跑、缺一条就不算归因。

用法：
    D:/RVC/.venv/Scripts/python.exe experiments/cover_elec_pitch.py \
        --clips media/ft_work/ppt12/clips24k --voice myvoice_tilt_t03 --out media/covers/elec
    D:/RVC/.venv/Scripts/python.exe experiments/cover_elec_pitch.py \
        --audio D:/RVC/dataset_raw/myvoice_supp2026_40k/supp2026_sing.wav \
        --voice myvoice_supp2026 --out media/covers/elec_sing
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 要扫的变调档。选点让输出中位 F0 穿过 218Hz（训练集 p99.9）这条线。
PITCHES: list[int] = [0, 5, 7, 12, 16, 19]

#: 输入片段合成用的总时长（秒）—— 太短会让统计量抖
INPUT_SECONDS = 24.0


def build_input(clips_dir: Path, sr_target: int, seconds: float):
    """把训练切片拼成一段域内输入（16k 与 40k 各要一份，口径同 `cover_knobs`）。"""
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    files = sorted(clips_dir.glob("*.wav"))
    if not files:
        raise SystemExit(f"[!] {clips_dir} 下没有 wav")
    picked = sorted(files, key=lambda p: -sf.info(str(p)).frames)[:16]
    segs, sr = [], None
    for p in picked:
        d, sr = sf.read(str(p), dtype="float32", always_2d=True)
        segs.append(d.mean(1))
    raw = np.concatenate(segs)
    raw = raw[: int(seconds * sr)]
    raw = raw / (np.abs(raw).max() + 1e-9) * 0.9
    in40 = resample_poly(raw.astype(np.float64), sr_target, int(sr)).astype(np.float32)
    in16 = resample_poly(raw.astype(np.float64), 16000, int(sr)).astype(np.float32)
    return in16, in40, [p.name for p in picked], int(sr)


def build_input_from_file(path: Path, sr_target: int, seconds: float):
    """把单个音频文件截成台阶输入（16k 给模型、目标采样率给机械对照臂）。

    与 `build_input` 的区别只在"输入从哪来"：这里是一条完整的唱歌/说话素材，
    不做切片拼接 —— 拼接会把乐句边界抹掉，而电音恰恰常出现在乐句尾。
    """
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    d, sr = sf.read(str(path), dtype="float32", always_2d=True)
    mono = d.mean(1)
    mono = mono[: int(seconds * sr)]
    mono = mono / (np.abs(mono).max() + 1e-9) * 0.9
    in40 = resample_poly(mono.astype(np.float64), sr_target, int(sr)).astype(np.float32)
    in16 = resample_poly(mono.astype(np.float64), 16000, int(sr)).astype(np.float32)
    return in16, in40, [path.name], int(sr)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="电音归因：只改请求音高（域内输入）")
    ap.add_argument("--clips", type=Path, help="模型自己的训练切片目录（与 --audio 二选一）")
    ap.add_argument("--audio", type=Path, help="单条输入音频（唱歌素材走这里；与 --clips 二选一）")
    ap.add_argument("--voice", default="myvoice_tilt_t03")
    ap.add_argument("--index-rate", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--seconds", type=float, default=INPUT_SECONDS)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    if not args.clips and not args.audio:
        raise SystemExit("必须给 --clips 或 --audio")
    # ★ 先钉绝对路径：setup_env() 会 chdir 到 RVC 根（同 cover_knobs 的注释）
    clips = args.clips.resolve() if args.clips else None
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    import offline_vc_infer as ov

    ov.setup_env()
    pth, index = _resolve(args.voice)
    print(f"[电音] 模型 {pth.name}（index={'有' if index else '无'}）")
    engine = ov.load_vc(str(pth), str(index) if index else "")
    print(f"[电音] 加载完成 · 目标采样率 {engine.tgt_sr}Hz")

    if args.audio:
        audio = args.audio.resolve()
        in16, in40, used, src_sr = build_input_from_file(audio, engine.tgt_sr, args.seconds)
        in_name = f"00_{audio.stem}_40k.wav"
        print(f"[电音] 单条输入 {audio.name} → {len(in16) / 16000:.1f}s（源 {src_sr}Hz）")
    else:
        in16, in40, used, src_sr = build_input(clips, engine.tgt_sr, args.seconds)
        in_name = "00_in_domain_40k.wav"
        print(f"[电音] 域内输入 {len(in16) / 16000:.1f}s，来自 {len(used)} 条切片（源 {src_sr}Hz）")
    sf.write(str(out / in_name), in40, engine.tgt_sr)

    rows: list[dict] = []
    # ---- RVC 臂：只改 pitch ----
    for p in PITCHES:
        t = time.time()
        y = ov.convert_audio(engine, in16.copy(), pitch=p,
                             index_rate=args.index_rate, seed=args.seed)
        dst = out / f"rvc_p{p:+03d}.wav"
        sf.write(str(dst), y, engine.tgt_sr)
        rows.append({"arm": "rvc", "pitch": p, "wav": str(dst),
                     "convert_s": round(time.time() - t, 1),
                     "expected_f0_hz": round(108.0 * 2 ** (p / 12), 1)})
        print(f"[电音] RVC pitch {p:+3d} → 期望 F0 {rows[-1]['expected_f0_hz']:6.1f}Hz "
              f"（{rows[-1]['convert_s']}s）")

    # ---- 机械对照臂：同样的音高变化，但不过模型 ----
    for p in PITCHES:
        r = 2 ** (p / 12)
        shifted = resample_poly(in40.astype(np.float64), 1000, int(round(1000 * r)))
        dst = out / f"mech_p{p:+03d}.wav"
        sf.write(str(dst), shifted.astype(np.float32), engine.tgt_sr)
        rows.append({"arm": "mech", "pitch": p, "wav": str(dst),
                     "expected_f0_hz": round(108.0 * 2 ** (p / 12), 1)})

    (out / "elec_pitch.json").write_text(
        json.dumps({"voice": args.voice, "clips": used, "input": in_name,
                    "mode": "audio" if args.audio else "clips", "src_sr": src_sr,
                    "seed": args.seed, "index_rate": args.index_rate,
                    "tgt_sr": int(engine.tgt_sr), "rows": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[电音] {len(rows)} 条产物 → {out}")
    return 0


def _resolve(voice: str):
    import os

    rvc = Path(os.environ.get("VM_RVC_ROOT", r"D:\RVC"))
    log_dir = rvc / "logs" / voice
    pth = log_dir / f"{voice}.pth"
    if not pth.exists():
        pth = rvc / "assets" / "weights" / f"{voice}.pth"
    if not pth.exists():
        raise SystemExit(f"找不到音色模型 {voice}.pth")
    return pth, next(iter(sorted(log_dir.glob("added_*.index"))), None)


if __name__ == "__main__":
    raise SystemExit(main())
