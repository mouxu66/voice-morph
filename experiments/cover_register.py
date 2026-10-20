"""「像童声」到底是谁的锅：源是赵雷，音区不是。

用户 2026-10-20 16:05 的裁决
    「你给我选的原声是儿声儿童的声音……导致我后面的声音也扭曲成了儿童的声音，
      搞得特别的怪，而且都带着电音的」

先把事实钉住（这一步做错，后面全白跑）
    旧候选 `candidates/成都-赵雷/vocals.wav` 到底是谁？
    · 与官方 MV（太合音乐，`youtube.com/watch?v=EPyFeQcDSUY`）分离轨的 CAM++ = **0.9866**
      （同一个人，几乎就是同一版录音；童声翻唱不可能过 0.5）；
    · 长时倒谱包络的 F1/F2 = 453/1109Hz vs 438/1109Hz —— 声道长度一致。
    所以**源就是赵雷**，上一页那句「源人声（赵雷）」没标错。

那为什么听着像童声？
    因为源选的是**最后一段副歌的最高潮**：中位音高 330Hz（E4），
    而用户自己的参照音中位 **116Hz**（p10 102 / p90 144）。
    Seed-VC 开着 `--f0-condition True`，**只跟源的音高曲线走** ——
    330Hz 喂进一个 116Hz 的参照音，出来的当然是「你的音色 + 童声的音区」，
    也就是 2.85 倍基频（+19 半音）的假声，顺带把扩散模型的失真放大成"电音"。

这一轮要验的
    音区和音色是**两个可以分开设的量**：把源降到用户自己的音区（−15 半音，
    让这段的中位落到参照音 p90 = 144Hz），其余一切不变，看三件事：
      ① 输出音高是否跟着降到 ~144Hz（不是 330Hz）；
      ② 像不像用户（CAM++ 不能掉）；
      ③ 高频与气口是否还在（8–12k / slope / 气口掉）。

臂（同一段源、同一个 Seed-VC、同 30 步）
    A0 原调 · 你的参照        ← 复现上一页的"童声/电音"
    A1 降15 · 你的参照        ← 只改音区
    A2 降15 · 你的参照先 BWE  ← 音区 + 带宽（沿用 m1w 的解耦结论）
    A3 A2 + 匹配 EQ           ← 带宽电平补到源的水准
    A4 降15 · 别人的全带宽    ← 对照：音色跑掉（CAM++ 会塌）

跑法
    .venv/Scripts/python.exe experiments/cover_register.py            # 推理 + 度量 + 页面
    .venv/Scripts/python.exe experiments/cover_register.py --measure  # 只重测已有产物
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

import cover_hf_matcher as Q  # noqa: E402
import cover_hf_tracking as H  # noqa: E402
import cover_seedvc_probe as P  # noqa: E402
import cover_seedvc_refbwe as B  # noqa: E402

OUT = ROOT / "media" / "covers" / "midsong_20261018" / "register"
WORK = OUT / "_raw"
#: 官方 MV 分离出来的人声（demucs htdemucs）。旧候选是同一版录音，但这段副歌在两个
#: 文件里位置不同（MV 多了 43s 前奏/尾奏），所以窗口按**音区**自动挑，不写死秒数。
VOCAL = (ROOT / "media/covers/midsong_20261018/candidates/成都-赵雷-真"
         / "demucs/htdemucs/source/vocals.wav")
REF = ROOT / "media/voicebank/myvoice_supp2026/reference.wav"
FULLBAND = ROOT / "media/voicebank/vbsyn_trial_ssb0033_60s/reference.wav"
#: 手臂长度（与上一页一致，才能逐条对比）
SEG_DUR = 20.0
#: 副歌只可能在这之后（MV 前 2 分钟是主歌，自动挑窗口时跳过）
SCAN_FROM = 120.0
#: 匹配 EQ 的增益上限。上一轮用 +18dB（m1w 的默认值），但搬到**降调后的**这一段会
#: 把 slope 从 0.38 压到 0.29 —— 那已经掉进“噪声底”档。+12dB 时 slope 0.36、
#: 8–12k 仍有 −65.8（源 −53.1），是这一段能拿到的最大干净量。
EQ_CAP_DB = 12.0


def read(path: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return y.mean(axis=1).astype(np.float32), int(sr)


def f0_curve(y: np.ndarray, sr: int, fmin: float = 70.0, fmax: float = 500.0,
             hop: int = 320) -> tuple[np.ndarray, float]:
    """16k 上的 pyin 音高曲线 + 帧率（20ms 帧移）。"""
    y16 = librosa.resample(np.asarray(y, dtype=np.float64), orig_sr=sr, target_sr=16000)
    f0, _voiced, _prob = librosa.pyin(y16, fmin=fmin, fmax=fmax, sr=16000,
                                      frame_length=1024, hop_length=hop)
    return f0, 16000 / hop


def pick_window(y: np.ndarray, sr: int, dur: float = SEG_DUR,
                scan_from: float = SCAN_FROM) -> tuple[float, float]:
    """挑「中位音高最高」的 dur 秒窗口，返回 (起点秒, 该窗 pyin 中位)。

    为什么按音高挑而不是按位置：用户抱怨的正是"夹着嗓子"，而"夹不夹"由音区决定。
    挑最高的一段 = 挑最可能翻车的一段，修复才可证伪。

    ★ 第一版用 YIN 挑，结果挑到 329.5s 且报 500Hz（= fmax 边界）—— 噪声帧的八度跳
      直接把"最高"这个判据骗了，而 pyin 复核同一窗口只有 178.8Hz。挑窗口必须和
      报数用同一把尺子（pyin），否则窗口选错、页面上的"源 330Hz"也不成立。
      另外 fmax 边界的帧一律剔除：那是失败帧，不是高音。
    """
    f0, fps = f0_curve(y, sr)
    lo = int(scan_from * fps)
    win = int(dur * fps)
    best, best_med = scan_from, -1.0
    for i in range(lo, max(lo, len(f0) - win), max(1, int(0.5 * fps))):
        seg = f0[i:i + win]
        v = seg[np.isfinite(seg)]
        v = v[(v > 70.0) & (v < 480.0)]
        if v.size < win * 0.4:          # 伴奏/气口为主：不算"唱上去"
            continue
        med = float(np.median(v))
        if med > best_med:
            best, best_med = i / fps, med
    return best, best_med


def shift_for(src_median: float, target: float) -> int:
    """把源中位音高搬到 target 需要的半音数（负=降）。整数，Seed-VC 只吃整数半音。"""
    if src_median <= 0 or target <= 0:
        raise ValueError("音高必须 > 0")
    return int(round(12.0 * float(np.log2(target / src_median))))


def median_f0(path: Path) -> float:
    """pyin 中位基频（只取有声帧）。这就是"听起来像谁的音区"的那个数。"""
    y, sr = read(path)
    y16 = librosa.resample(np.asarray(y, dtype=np.float64), orig_sr=sr, target_sr=16000)
    f0, _voiced, _prob = librosa.pyin(y16, fmin=60, fmax=600, sr=16000,
                                      frame_length=1024, hop_length=160)
    v = f0[np.isfinite(f0)]
    return round(float(np.median(v)), 1) if v.size else float("nan")


def cut_resampled(src: Path, out: Path, start: float, dur: float, sr_out: int = 44100) -> Path:
    y, sr = read(src)
    a, b = int(start * sr), int((start + dur) * sr)
    if b > len(y):
        raise SystemExit(f"切片越界：{src.name} 只有 {len(y) / sr:.1f}s")
    seg = y[a:b]
    if sr != sr_out:
        seg = librosa.resample(seg.astype(np.float64), orig_sr=sr, target_sr=sr_out).astype(np.float32)
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), seg, sr_out, subtype="PCM_16")
    return out


def cached_ref_bwe(ref: Path, dst: Path) -> Path:
    """你的参照音 → BWE → 44.1k。按 mtime 判新旧，换了参照音必须重跑。

    ★ 第一版直接写 `WORK/ref_bwe44.wav` 而没建 WORK —— BWE 跑了 60s、
      最后 `sf.write` 报 `System error`，整个实验白跑。写入目录必须在调用前建。
    ★ 上一轮（m1w）的产物在 `seedvc_probe/_raw3/ref_bwe44.wav`，与这里等价；
      只当它**比参照音新**时才拿过来当缓存，避免"看着有新文件、其实读的是旧参照"。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_mtime >= ref.stat().st_mtime:
        return dst.resolve()
    legacy = B.WORK / "ref_bwe44.wav"
    if legacy.exists() and legacy.stat().st_mtime >= ref.stat().st_mtime:
        import shutil as _sh

        _sh.copy2(legacy, dst)
        return dst.resolve()
    return B.ref_bwe(ref, dst).resolve()


def arms_spec(shift: int, ref_bwe: Path) -> list[dict]:
    """五个臂。A3 是 A2 的 EQ 派生，不跑模型 —— 列在这里是为了页面与度量只读一处。"""
    return [
        {"key": "A0_原调_你的参照", "shift": 0, "ref": REF, "model": True, "from": None,
         "title": "A0 原调 · 你的参照（复现上一页）", "cls": "bad"},
        {"key": "A1_降调_你的参照", "shift": shift, "ref": REF, "model": True, "from": None,
         "title": f"A1 降{abs(shift)}半音 · 你的参照", "cls": "good"},
        {"key": "A2_降调_你的参照BWE", "shift": shift, "ref": ref_bwe, "model": True, "from": None,
         "title": f"A2 降{abs(shift)} · 你的参照先 BWE", "cls": "good"},
        {"key": "A3_降调_补高频", "shift": shift, "ref": ref_bwe, "model": False,
         "from": "A2_降调_你的参照BWE",
         "title": f"A3 降{abs(shift)} · A2 + 匹配 EQ", "cls": "good"},
        {"key": "A4_降调_别人的全带宽", "shift": shift, "ref": FULLBAND, "model": True,
         "from": None, "title": f"A4 降{abs(shift)} · 别人的全带宽参照", "cls": "warn"},
    ]


def measure(path: Path, src: Path, ref: Path) -> dict:
    y, sr = read(path)
    t = H.tracking(y, sr)
    src_y, src_sr = read(src)
    row = {
        "name": path.stem,
        "f0": median_f0(path),
        "hf_db": round(float(P.analyze(y, sr)["bands"]["8000-12000"]), 1),
        "r_env": t["r_env"], "slope": t["slope"], "gap_drop": t["gap_drop_db"],
        "hf_spread": t["hf_spread_db"],
        "verdict": H.verdict(t, float(P.analyze(src_y, src_sr)["bands"]["8000-12000"])),
    }
    try:
        import cover_metrics

        row["secs_cos"] = cover_metrics.secs(path, ref)
    except Exception as e:  # noqa: BLE001
        row["secs_cos"] = f"n/a（{type(e).__name__}）"
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", action="store_true", help="跳过推理，只重测已有产物")
    ap.add_argument("--page-only", action="store_true",
                    help="只按 register.json 重出页面（改文案/样式不想再跑一遍 pyin+CAM++）")
    ap.add_argument("--vocal", default=str(VOCAL))
    ap.add_argument("--ref", default=str(REF))
    ap.add_argument("--fullband", default=str(FULLBAND))
    ap.add_argument("--start", type=float, default=None, help="手动指定起点秒（默认按音区自动挑）")
    args = ap.parse_args()

    if args.page_only:
        meta_path = OUT / "register.json"
        if not meta_path.exists():
            raise SystemExit(f"缺 {meta_path}：先跑一次完整度量")
        import cover_register_page

        cover_register_page.build(OUT, json.loads(meta_path.read_text(encoding="utf-8")))
        return 0

    vocal, ref, fb = Path(args.vocal), Path(args.ref), Path(args.fullband)
    for p in (vocal, ref):
        if not p.exists():
            raise SystemExit(f"缺素材：{p}")
    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)   # ref_bwe 的写入目录：不建就会 "System error"

    # ── ① 挑窗口 + 定调 ────────────────────────────────────────────────
    y, sr = read(vocal)
    if args.start is None:
        start, _ = pick_window(y, sr)
    else:
        start = float(args.start)
    src = cut_resampled(vocal, OUT / "src.wav", start, SEG_DUR)
    src_f0 = median_f0(src)

    ref_y, ref_sr = read(ref)
    ref16 = librosa.resample(np.asarray(ref_y, dtype=np.float64), orig_sr=ref_sr, target_sr=16000)
    ref_f0_curve, _ = librosa.pyin(ref16, fmin=60, fmax=600, sr=16000,
                                  frame_length=1024, hop_length=160)[:2]
    rv = ref_f0_curve[np.isfinite(ref_f0_curve)]
    target = float(np.percentile(rv, 90))          # 你的 p90：唱到这儿还不至于挤嗓子
    shift = shift_for(src_f0, target)

    print(f"\n=== 定调 ===\n源（{vocal.name}）{start:.1f}–{start + SEG_DUR:.1f}s  中位 {src_f0}Hz")
    print(f"你的参照中位 {float(np.median(rv)):.1f}Hz / p90 {target:.1f}Hz → 降 {abs(shift)} 半音"
          f"（{src_f0}Hz → {src_f0 * 2 ** (shift / 12):.0f}Hz）")

    ref_bwe = cached_ref_bwe(ref, WORK / "ref_bwe44.wav")
    spec = arms_spec(shift, ref_bwe)

    # ── ② 跑模型 ────────────────────────────────────────────────────────
    for a in spec:
        dst = OUT / f"{a['key']}.wav"
        if not a["model"]:
            continue
        if args.measure and dst.exists():
            continue
        if a["from"]:
            continue
        if dst.exists():
            dst.unlink()
        got = P.run_seedvc(src.resolve(), Path(a["ref"]).resolve(),
                           (WORK / a["key"]).resolve(), shift=a["shift"])
        shutil.move(str(got), str(dst))
        print(f"  ✓ {a['key']}（shift {a['shift']:+d}）")

    # ── ③ 派生臂：匹配 EQ（内容已在，只补电平）─────────────────────────
    for a in spec:
        if a["from"] is None:
            continue
        dst = OUT / f"{a['key']}.wav"
        if args.measure and dst.exists():
            continue
        base = OUT / f"{a['from']}.wav"
        ys, srs = read(src)
        ya, sra = read(base)
        af, ag = Q.eq_curve(Q.band_levels(ys, srs), Q.band_levels(ya, sra),
                            cap_db=EQ_CAP_DB)
        sf.write(str(dst), Q.apply_eq(ya, sra, af, ag), sra, subtype="FLOAT")
        print(f"  ✓ {a['key']}（EQ 派生自 {a['from']}）")

    # ── ④ 度量 ──────────────────────────────────────────────────────────
    src_hf = round(float(P.analyze(*read(src))["bands"]["8000-12000"]), 1)
    rows = [{"name": "源（赵雷，原调）", "f0": src_f0, "hf_db": src_hf,
             "r_env": None, "slope": None, "gap_drop": None, "secs_cos": None}]
    print(f"\n=== 同一段源、同一个 Seed-VC，只改音区与参照（源 8–12k = {src_hf}dB）===")
    print(f"{'':<26}{'基频':>8}{'8–12k':>9}{'r_env':>8}{'slope':>7}{'气口掉':>8}{'CAM++':>8}")
    for a in spec:
        p = OUT / f"{a['key']}.wav"
        if not p.exists():
            print(f"{a['key']:<26}{'缺产物':>8}")
            continue
        r = measure(p, src, ref)
        r["title"] = a["title"]
        r["cls"] = a["cls"]
        rows.append(r)
        cos = r["secs_cos"]
        cos_s = f"{cos:>8.3f}" if isinstance(cos, float) else f"{'—':>8}"
        print(f"{a['key']:<26}{r['f0']:>8.0f}{r['hf_db']:>9.1f}{r['r_env']:>8.2f}"
              f"{r['slope']:>7.2f}{r['gap_drop']:>8.1f}{cos_s}")
    for r in rows:
        if r.get("r_env") is not None:
            r["verdict"] = H.verdict(
                {"hf_active_db": r["hf_db"], "r_env": r["r_env"], "slope": r["slope"],
                 "gap_drop_db": r["gap_drop"]}, src_hf)

    meta = {"src": str(src), "start": start, "dur": SEG_DUR, "shift": shift,
            "src_f0": src_f0, "ref_median": round(float(np.median(rv)), 1),
            "ref_p90": round(target, 1), "rows": rows}
    (OUT / "register.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    import cover_register_page

    cover_register_page.build(OUT, meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
