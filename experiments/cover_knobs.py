# -*- coding: utf-8 -*-
"""把「同一个输入、只换一个旋钮」变成可测实验（在 RVC venv 里跑）。

为什么要有它（2026-10-14）
    用户对第一版翻唱的评价是「声音混杂、没有最纯粹的声音」。查完社区（三条线的调研）
    与 RVC 源码后，可疑的原因**不止一个**，而且其中有一条能在源码上读出来、却
    **不一定真的生效**：

        `protect < 0.5` 时，RVC 会把非浊音帧写成
            `feats = protect × 转换后 + (1 - protect) × 原唱特征`
        —— 0.33 就是「非浊音帧里掺 67% 原唱」。这正是\"混杂\"的字面机制。
        **但**本 fork 的 `get_f0()` 先对 f0 做了插值（`f0[uv] = np.interp(...)`）
        再返回 `f0bak`，于是 `pitchf` 几乎处处 > 0 ⇒ `pitchff` 恒为 1 ⇒ 该分支是空操作。
        源码读得出，是否生效只能**实测**：所以每个旋钮都跑一遍并比 md5。

所以本脚本做三件事，而不是复制一份 `convert_audio`：
    ① 走产品那条**真函数**（`offline_vc_infer.convert_audio`，旋钮是它新加的关键字参数）；
    ② 每个配置逐字节比 md5 —— 相同就是\"这个旋钮在本 fork 里没用\"，不带情绪地下结论；
    ③ 装一个 `get_f0` 探针，把**真正被用来做 protect 判据**的 `pitchf` 记下来
       （含零点占比），免得又靠读源码猜。

跑法（必须用 RVC venv 的 python —— 主 venv 没有 torch/infer）：
    D:/RVC/.venv/Scripts/python.exe experiments/cover_knobs.py \
        --source media/covers/lorezo_feed_the_one/01_original_vocal.wav \
        --voice myvoice_ppt12_full --pitch -12 --start 78 --seconds 20 \
        --out media/covers/knobs

复现产品那次产物（行为保持的端到端证明）：
    D:/RVC/.venv/Scripts/python.exe experiments/cover_knobs.py --verify-product \
        --source media/covers/lorezo_feed_the_one/01_original_vocal.wav \
        --verify-against media/covers/lorezo_feed_the_one/02_my_voice_vocal.wav \
        --voice myvoice_ppt12_full --pitch -12 --out media/covers/knobs
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

#: 仓库根（本文件在 experiments/ 下）
ROOT = Path(__file__).resolve().parent.parent

#: `offline_vc_infer` 在 m2_server/ 下（产品是按"工作目录 = m2_server"来 import 它的），
#: 本脚本从 experiments/ 跑，必须自己把它接进 sys.path。
for _p in (ROOT, ROOT / "m2_server"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

#: 音域建议值（上一版实测：歌声 219.5Hz、目标音色 108.0Hz ⇒ −12 半音）
DEFAULT_PITCH = -12

#: 扫描表：(名字, 一句话说明, 传给 convert_audio 的 kwargs)
#:
#: 顺序有意：A 是现役值，B/C 测 protect 到底有没有生效，D/F 测检索与包络，
#: E 测那个 11 kHz 低通，G/H 换掉变调 —— 用户抱怨的两个版本正好是 G/H 之外的那两档。
CONFIGS: list[tuple[str, str, dict]] = [
    ("A_base", "现役值：protect 0.33 · 低通 11k · index 0.5", {}),
    ("B_protect50", "protect 0.5 = 关保护（非浊音帧不再掺原唱特征）", {"protect": 0.5}),
    ("C_protect0", "protect 0.0 = 极端对照（非浊音帧全用原唱特征）", {"protect": 0.0}),
    ("D_ir0", "index_rate 0 = 不检索训练特征（纯模型音色）", {"index_rate": 0.0}),
    ("E_nolp", "去掉 11 kHz 低通（后处理只留 RMS 锚定 + 峰值安全）", {"lowpass_hz": 0.0}),
    ("F_rms1", "rms_mix_rate 1.0（用模型自己的包络，不贴原唱包络）", {"rms_mix_rate": 1.0}),
    ("G_p0", "原调（+0 半音）—— 第一版 D/E 那一档", {"pitch": 0}),
    (
        "H_p0_clean",
        "原调 + ir0 + 无低通 + protect0.5（几个\"更纯\"的方向叠加）",
        {"pitch": 0, "index_rate": 0.0, "lowpass_hz": 0.0, "protect": 0.5},
    ),
]


def _md5(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()[:12]


def _install_f0_spy(engine) -> dict:
    """把 `get_f0` 包一层，记下**真正被 pipeline 用来做 protect 判据**的 pitchf。

    为什么要探针而不是自己算一遍：protect 的分支拿的是 pipeline 内部那个 pitchf
    （f0 插值 + 变调之后），自己复算一遍等于\"用另一份实现测这份实现\"。
    """
    seen: dict = {"calls": 0, "pitchf": None}
    # `get_f0` 在 pipeline 对象上（`engine.vc` 是 VC，`engine.vc.pipeline` 才是 Pipeline）
    orig = engine.vc.pipeline.get_f0

    def spy(*a, **kw):
        pitch, pitchf = orig(*a, **kw)
        import numpy as np

        arr = np.asarray(pitchf, dtype=np.float64).ravel()
        seen["calls"] += 1
        seen["pitchf"] = arr if seen["pitchf"] is None else np.concatenate(
            [seen["pitchf"], arr]
        )
        return pitch, pitchf

    engine.vc.pipeline.get_f0 = spy
    return seen


def _pitchf_stats(pitchf) -> dict:
    """非浊音帧占比 —— 就是 protect 分支会掺原唱特征的那些帧。"""
    import numpy as np

    if pitchf is None or len(pitchf) == 0:
        return {"uv_frac": None, "frames": 0, "median_f0_hz": None}
    uv = pitchf <= 0
    voiced = pitchf[~uv]
    return {
        "uv_frac": round(float(uv.mean()), 4),
        "frames": int(len(pitchf)),
        "median_f0_hz": round(float(np.median(voiced)), 2) if len(voiced) else None,
    }


def run_sweep(args) -> int:
    # ★ 先把路径钉成绝对路径：`setup_env()` 会 chdir 到 RVC 根，之后再 `resolve()`
    #   会把仓库相对路径解成 `D:\RVC\...`（2026-10-14 实测踩了一次）。
    src = Path(args.source).resolve()
    out_dir = Path(args.out).resolve()

    import numpy as np  # noqa: PLC0415

    import offline_vc_infer as ov  # noqa: PLC0415

    ov.setup_env()
    from infer.audio import load_audio

    out_dir.mkdir(parents=True, exist_ok=True)
    pth, index = ov_module_resolve(args.voice)

    print(f"[旋钮] 模型 {pth.name}（index={'有' if index else '无'}）· 输入 {src.name}")
    t0 = time.time()
    engine = ov.load_vc(str(pth), str(index) if index else "")
    print(f"[旋钮] 模型加载 {time.time() - t0:.1f}s · 目标采样率 {engine.tgt_sr}Hz")

    full = load_audio(str(src), 16000)
    a, b = int(args.start * 16000), int((args.start + args.seconds) * 16000)
    clip = np.asarray(full[a:b], dtype=np.float32)
    print(f"[旋钮] 片段 {args.start:.0f}s–{args.start + args.seconds:.0f}s（{len(clip) / 16000:.1f}s）")

    # 原声片段也留一份 40k 参考（页面里当\"原唱\"听，和 A 同一段时间轴）
    import soundfile as sf
    from scipy.signal import resample_poly

    ref_pcm = resample_poly(np.asarray(full[a:b], dtype=np.float64), 5, 2)  # 16k→40k
    sf.write(str(out_dir / "00_source_40k.wav"), (ref_pcm * 0.7).astype(np.float32), 40000)

    spy = _install_f0_spy(engine)
    rows: list[dict] = []
    for name, why, kwargs in CONFIGS:
        seed = args.seed
        kw = dict(kwargs)
        pitch = int(kw.pop("pitch", args.pitch))
        index_rate = float(kw.pop("index_rate", args.index_rate))
        t = time.time()
        y = ov.convert_audio(
            engine,
            clip.copy(),
            pitch=pitch,
            index_rate=index_rate,
            seed=seed,
            protect=float(kw.pop("protect", ov.PROTECT)),
            rms_mix_rate=float(kw.pop("rms_mix_rate", ov.RMS_MIX_RATE)),
            lowpass_hz=float(kw.pop("lowpass_hz", ov.POST_LOWPASS_HZ)),
        )
        assert not kw, f"配置 {name} 里有没被消费的参数: {kw}"
        dst = out_dir / f"{name}.wav"
        sf.write(str(dst), y, engine.tgt_sr)
        peak = float(np.abs(y).max())
        rows.append(
            {
                "name": name,
                "why": why,
                "pitch": pitch,
                "index_rate": index_rate,
                "md5": _md5(dst),
                "peak": round(peak, 4),
                "clipped": int((np.abs(y) >= 0.999).sum()),
                "convert_s": round(time.time() - t, 1),
                "wav": str(dst),
            }
        )
        print(f"[旋钮] {name:12s} {time.time() - t:5.1f}s  peak={peak:.3f}  md5={rows[-1]['md5']}  {why}")

    # ★ 非浊音帧占比：protect 分支的唯一判据（pitchf ≤ 0 的帧才会掺原唱）
    stats = _pitchf_stats(spy["pitchf"])
    print(f"[旋钮] get_f0 探针：{stats}")

    ceiling = _ceiling_test(engine, ov, args) if args.ceiling_clips else None

    uniq = {}
    for r in rows:
        uniq.setdefault(r["md5"], []).append(r["name"])
    same = {h: names for h, names in uniq.items() if len(names) > 1}

    report = {
        "source": str(src),
        "voice": args.voice,
        "pitch": args.pitch,
        "index_rate": args.index_rate,
        "seed": args.seed,
        "start_s": args.start,
        "seconds": args.seconds,
        "tgt_sr": int(engine.tgt_sr),
        "f0_probe": stats,
        "configs": rows,
        "identical_groups": same,
        "ceiling": ceiling,
    }
    (out_dir / "knobs.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[旋钮] 报告 → {out_dir / 'knobs.json'}")
    if same:
        for h, names in same.items():
            print(f"[旋钮] ⚠️ 这几个配置输出**逐字节相同**（该旋钮在本 fork 里无效果）：{names}")
    return 0


def _ceiling_test(engine, ov, args) -> dict:
    """模型天花板：拿**它自己的训练素材**过一遍同一模型，看它把输入变暗/变糊多少。

    为什么必须有这一条：如果模型在自己的域内（同一个人、同一支麦、纯说话）
    已经把高频与清晰度弄丢，那么用户在**歌声**上听到的"闷/不纯"就不该归因于歌或旋钮
    —— 那是在追一个被训练集封死的天花板。反过来，若域内几乎无损，
    那问题就出在"唱歌"这个跨域输入上。

    口径：输入按 16 kHz 喂给模型（这就是产品干的），但**比较在 40 kHz 上做** ——
    输入的 24k 素材重采样到 40k，与输出同域。8 kHz 以上那一段完全是模型自己合成的
    （HuBERT 看不到它），所以那一段的差距就是天花板的直接度量。
    """
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    clips = sorted(Path(args.ceiling_clips).glob("*.wav"))
    if not clips:
        print(f"[天花板] 跳过：{args.ceiling_clips} 下没有 wav")
        return {"error": "没有素材"}
    # 挑时长最长的前 N 条（训练切片长度不均，长的大概率是完整句子）
    picked = sorted(clips, key=lambda p: -sf.info(str(p)).frames)[: args.ceiling_n]
    segs, sr = [], None
    for p in picked:
        d, sr = sf.read(str(p), dtype="float32", always_2d=True)
        segs.append(d.mean(1))
    raw = np.concatenate(segs)
    raw = raw / (np.abs(raw).max() + 1e-9) * 0.9
    in40 = resample_poly(raw.astype(np.float64), 40000, int(sr)).astype(np.float32)
    in16 = resample_poly(raw.astype(np.float64), 16000, int(sr)).astype(np.float32)
    y = ov.convert_audio(engine, in16, pitch=0, index_rate=args.index_rate, seed=args.seed)
    out_dir = Path(args.out).resolve()
    sf.write(str(out_dir / "ceiling_in_40k.wav"), in40, 40000)
    sf.write(str(out_dir / "ceiling_out.wav"), y, engine.tgt_sr)
    res = {
        "clips": [p.name for p in picked],
        "in_sr": int(sr),
        "seconds": round(len(y) / engine.tgt_sr, 2),
        "in_wav": str(out_dir / "ceiling_in_40k.wav"),
        "out_wav": str(out_dir / "ceiling_out.wav"),
        "out_md5": _md5(out_dir / "ceiling_out.wav"),
    }
    print(f"[天花板] {len(picked)} 条训练素材（{res['seconds']}s）→ {out_dir / 'ceiling_out.wav'}")
    return res


def ov_module_resolve(voice: str):
    """找模型与 index（复用产品口径：logs/<voice>/ 优先，其次 assets/weights/）。"""
    rvc = Path(os.environ.get("VM_RVC_ROOT", r"D:\RVC"))
    log_dir = rvc / "logs" / voice
    pth = log_dir / f"{voice}.pth"
    if not pth.exists():
        pth = rvc / "assets" / "weights" / f"{voice}.pth"
    if not pth.exists():
        raise SystemExit(f"找不到音色模型 {voice}.pth")
    index = next(iter(sorted(log_dir.glob("added_*.index"))), None)
    return pth, index


def run_verify(args) -> int:
    """把产品那次的输入重跑一遍现役值，与产品产物比 md5。

    这是**行为保持**的端到端证明：`convert_audio` 的关键字参数是 2026-10-14 加上的，
    默认值必须与加参数之前逐字节一致。比\"我读了 diff\"硬得多的一条证据。
    """
    src = Path(args.source).resolve()
    against = Path(args.verify_against).resolve()
    out_dir = Path(args.out).resolve()

    import numpy as np  # noqa: PLC0415
    import soundfile as sf  # noqa: PLC0415

    import offline_vc_infer as ov  # noqa: PLC0415

    ov.setup_env()
    from infer.audio import load_audio

    pth, index = ov_module_resolve(args.voice)
    engine = ov.load_vc(str(pth), str(index) if index else "")
    audio = np.asarray(load_audio(str(src), 16000), dtype=np.float32)
    y = ov.convert_audio(engine, audio, pitch=args.pitch, index_rate=args.index_rate)
    dst = out_dir / "_verify.wav"
    dst.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), y, engine.tgt_sr)

    ok_md5 = _md5(dst) == _md5(against)
    # ★ 逐样本差必须**两边都从文件读**：拿内存里的 `y`（float32、未量化）去比
    #   磁盘上的 PCM_16 文件，差的永远是一个量化步长的一半（1/65536≈3.05e-5），
    #   于是"md5 相同"与"逐样本差 3e-5"会同时成立 —— 那一步差是在比两种东西。
    d_new, sr_new = sf.read(str(dst), dtype="float64", always_2d=True)
    d_ref, sr_ref = sf.read(str(against), dtype="float64", always_2d=True)
    same_len = d_new.shape == d_ref.shape and sr_new == sr_ref and sr_ref == engine.tgt_sr
    maxdiff = float(np.abs(d_new.mean(1) - d_ref.mean(1)).max()) if same_len else None
    ok_samples = bool(same_len and maxdiff is not None and maxdiff < 1e-9)
    print(f"[核验] 重跑 {dst}  vs  产品产物 {against.name}")
    print(f"[核验] 长度/采样率一致：{same_len}（{d_new.shape[0]} @{sr_new}Hz vs {d_ref.shape[0]} @{sr_ref}Hz）")
    print(f"[核验] md5 相同：{ok_md5}（{_md5(dst)} vs {_md5(against)}）")
    print(f"[核验] 最大逐样本差（两边都从文件读）：{maxdiff}")
    return 0 if (ok_md5 or ok_samples) else 1


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="RVC 推理旋钮扫描（同一输入只换一个旋钮）")
    ap.add_argument("--source", required=True, type=Path, help="源歌声 wav（demucs 的 vocals）")
    ap.add_argument("--voice", default="myvoice_ppt12_full", help="RVC 实验名")
    ap.add_argument("--pitch", type=int, default=DEFAULT_PITCH, help="变调半音数")
    ap.add_argument("--index-rate", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=1234, help="RVC 推理种子（固定才可比）")
    ap.add_argument("--start", type=float, default=78.0, help="片段起点（秒）")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--out", required=True, type=Path, help="产物目录")
    ap.add_argument("--ceiling-clips", type=Path, default=None,
                    help="模型天花板测试的素材目录（如 media/ft_work/ppt12/clips24k）")
    ap.add_argument("--ceiling-n", type=int, default=8)
    ap.add_argument("--verify-product", action="store_true", help="只跑现役值并与产品产物核验")
    ap.add_argument("--verify-against", type=Path, default=None, help="产品产物（核验用）")
    args = ap.parse_args()
    # ★ 全部先钉成绝对路径：`setup_env()` 会 chdir 到 RVC 根，之后任何 `resolve()`
    #   都会把仓库相对路径解成 `D:\RVC\...`（2026-10-14 实测踩了两次）。
    args.source = args.source.resolve()
    args.out = args.out.resolve()
    if args.ceiling_clips is not None:
        args.ceiling_clips = args.ceiling_clips.resolve()
    if args.verify_against is not None:
        args.verify_against = args.verify_against.resolve()

    if args.verify_product:
        if not args.verify_against:
            print("[!] --verify-product 需要 --verify-against <产品产物 wav>")
            return 2
        return run_verify(args)
    return run_sweep(args)


if __name__ == "__main__":
    sys.exit(main())
