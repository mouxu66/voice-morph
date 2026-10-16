# -*- coding: utf-8 -*-
"""index_rate 扫描：同一段歌声、同一权重，只换检索强度（在 RVC venv 里跑）。

为什么要有它（2026-10-16）
    `cover_knobs.py`（2026-10-14）在 index 这条轴上只测过 0 与 0.5 两点。
    2026-10-16 社区调研的通行口径：像度不足 / 被源音带跑 → 把 index_rate
    从 0.5 提到 **0.7–0.75**；>0.8 起齿音尖、咬字糊（要配 De-Esser 再极限）。
    用户对现役产物仍评「不像」——这是**不重训**成本下最值得先补的一格。
    产物交给 `experiments/cover_metrics.py` 打客观分（CAM++ / 频段 / 质心）并出试听页。

做法与 `cover_knobs.py` 同口径：
    ① 走产品真函数 `offline_vc_infer.convert_audio`（与现役链路同一个入口）；
    ② 固定 seed，逐字节比 md5（相同即说明该旋钮无效果，不带情绪下结论）；
    ③ 单变量：除 index_rate 外一切相同（同段、同 seed、同 pitch、同后处理）。

跑法（必须用 RVC venv 的 python —— 主 venv 没有 torch/infer）：
    D:/RVC/.venv/Scripts/python.exe experiments/cover_index_sweep.py \
        --source media/covers/song2/01_vocals.wav \
        --voice myvoice_tilt_t03 \
        --pitch -12 --start 85 --seconds 45 \
        --rates 0.5,0.7 \
        --out media/covers/index_ab/t03
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: 自己的目录也要进 path —— `cover_knobs` 是 experiments/ 下的兄弟脚本。
#: 直接 `python experiments/cover_index_sweep.py` 时靠 sys.path[0] 恰好可用，
#: 但被测试用 importlib 按路径加载时就得自己保底（test_cover_index_sweep 会加载它）。
for _p in (ROOT, ROOT / "m2_server", Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 复用产品口径解析 pth/index（logs/<voice>/ 优先，其次 assets/weights/）。
#: 不复制一份实现 —— 同族教训：副本会悄悄漂移（见 cover_metrics.normalize_configs 注释）。
from cover_knobs import ov_module_resolve  # noqa: E402

#: 默认扫描档位：0.5 = 现役值，0.7 = 社区「更像」候选档（2026-10-16 社区调研口径 0.7–0.75）
DEFAULT_RATES = "0.5,0.7"


def parse_rates(text: str) -> list[float]:
    """解析 `--rates`（"0.5,0.7" → [0.5, 0.7]）；空串返回空表，由 main 显式拒绝。"""
    return [float(r) for r in str(text).split(",") if r.strip()]


def config_name(index: int, rate: float) -> str:
    """A_index0.5 / B_index0.7。序号+档位都进名字 —— 体检按名字唯一，防互相覆盖。"""
    tag = "ABCD"[index] if index < 4 else str(index)
    return "%s_index%s" % (tag, ("%.2f" % rate).rstrip("0").rstrip("."))


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()[:12]


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="RVC index_rate 扫描（同一输入只换检索强度）")
    ap.add_argument("--source", required=True, type=Path, help="源歌声 wav（demucs 的 vocals）")
    ap.add_argument("--voice", default="myvoice_tilt_t03", help="RVC 实验名")
    ap.add_argument("--pitch", type=int, default=-12, help="变调半音数")
    ap.add_argument("--seed", type=int, default=1234, help="RVC 推理种子（固定才可比）")
    ap.add_argument("--start", type=float, default=85.0, help="片段起点（秒）")
    ap.add_argument("--seconds", type=float, default=45.0)
    ap.add_argument("--rates", default=DEFAULT_RATES, help="要扫的 index_rate，逗号分隔")
    ap.add_argument("--out", required=True, type=Path, help="产物目录")
    args = ap.parse_args()

    # ★ 全部先钉成绝对路径：`setup_env()` 会 chdir 到 RVC 根，
    #   之后任何 `resolve()` 都会把仓库相对路径解成 `D:\RVC\...`（cover_knobs 踩过两次）。
    src = args.source.resolve()
    out_dir = args.out.resolve()

    import numpy as np  # noqa: PLC0415
    import soundfile as sf  # noqa: PLC0415
    from scipy.signal import resample_poly  # noqa: PLC0415

    import offline_vc_infer as ov  # noqa: PLC0415

    ov.setup_env()
    from infer.audio import load_audio  # noqa: PLC0415

    rates = parse_rates(args.rates)
    if not rates:
        print("[索引] --rates 为空")
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    pth, index = ov_module_resolve(args.voice)
    if index is None:
        print(f"[索引] ⚠️ {args.voice} 没有 added_*.index —— index_rate 会被引擎忽略，结果是空扫描")
    print(f"[索引] 模型 {pth.name}（index={'有：' + index.name if index else '无'}）· 输入 {src.name}")

    t0 = time.time()
    engine = ov.load_vc(str(pth), str(index) if index else "")
    print(f"[索引] 模型加载 {time.time() - t0:.1f}s · 目标采样率 {engine.tgt_sr}Hz")

    full = load_audio(str(src), 16000)
    a, b = int(args.start * 16000), int((args.start + args.seconds) * 16000)
    clip = np.asarray(full[a:b], dtype=np.float32)
    print(f"[索引] 片段 {args.start:.0f}s–{args.start + args.seconds:.0f}s（{len(clip) / 16000:.1f}s）")

    # 原声片段也留一份 40k 参考（试听页里当「原唱」，与产物同一时间轴）
    ref_pcm = resample_poly(np.asarray(full[a:b], dtype=np.float64), 5, 2)  # 16k→40k
    sf.write(str(out_dir / "00_source_40k.wav"), (ref_pcm * 0.7).astype(np.float32), 40000)

    rows: list[dict] = []
    for i, rate in enumerate(rates):
        name = config_name(i, rate)
        why = {
            0.5: "index_rate 0.5（现役值）",
            0.7: "index_rate 0.7（社区'更像'档 0.7–0.75）",
        }.get(round(rate, 2), f"index_rate {rate:g}")
        t = time.time()
        y = ov.convert_audio(engine, clip.copy(), pitch=args.pitch, index_rate=rate, seed=args.seed)
        dst = out_dir / f"{name}.wav"
        sf.write(str(dst), y, engine.tgt_sr)
        peak = float(np.abs(y).max())
        rows.append(
            {
                "name": name,
                "why": why,
                "pitch": args.pitch,
                "index_rate": rate,
                "md5": _md5(dst),
                "peak": round(peak, 4),
                "clipped": int((np.abs(y) >= 0.999).sum()),
                "convert_s": round(time.time() - t, 1),
                "wav": str(dst),
            }
        )
        print(f"[索引] {name:16s} {time.time() - t:5.1f}s  peak={peak:.3f}  md5={rows[-1]['md5']}  {why}")

    uniq: dict[str, list[str]] = {}
    for r in rows:
        uniq.setdefault(r["md5"], []).append(r["name"])
    same = {h: names for h, names in uniq.items() if len(names) > 1}
    if same:
        print(f"[索引] ⚠️ 有配置输出逐字节相同（该旋钮没生效）：{same}")

    report = {
        "source": str(src),
        "voice": args.voice,
        "pitch": args.pitch,
        "index_rate": rows[0]["index_rate"] if rows else None,
        "seed": args.seed,
        "start_s": args.start,
        "seconds": args.seconds,
        "tgt_sr": int(engine.tgt_sr),
        "configs": rows,
        "identical_groups": same,
    }
    (out_dir / "index_sweep.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[索引] 报告 → {out_dir / 'index_sweep.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
