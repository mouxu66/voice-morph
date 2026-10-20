"""「像童声」到底是谁的锅：**挑窗错了，素材是一段没人唱的尾巴**。

用户 2026-10-20 的裁决（两次）
    16:05「你给我选的原声是儿声儿童的声音……导致我后面的声音也扭曲成了儿童的声音」
    18:0x「这段根本就不是赵雷的声音啊。我要克隆，也不是这段声音啊」

★ 第二次是对的，而 m1v 那版的结论（「源就是赵雷，错的是音区 −15」）**必须撤回**：
    它靠 CAM++ 0.9866，但那个数只证明「两份下载是同一条录音」，不证明「是谁」——
    我把一个自己标成"官方 MV"的视频当金标准，再拿它和被怀疑的文件互相印证，是循环论证。
    更致命的是：它是在**整曲**上量的，而我喂给模型的那 20 秒根本不在这段里。

真因（2026-10-20 实测，`cover_identity_check.py` 可复现）
    《成都》在 5:10 左右就唱完了，视频尾巴还有 50 多秒衰减；
    demucs 人声轨在那段只剩 **−31 ~ −36dBFS**（正段落 −16 ~ −18）——
    pyin 在这些噪声帧上吐出 330–345Hz 的八度跳，于是「中位音高最高」的窗口
    正好选中了一段**没人唱**的地方。旧窗口 313.5–333.5s / 345Hz 就在这里。
    同一份文件内：正文段 172Hz vs 尾巴段 339Hz，CAM++ 只有 0.18–0.30
    （比"两个不同成男"的 0.39–0.47 还低）—— 那点"人声"是残响，不是演唱。
    所以"童声 + 电"不是音区问题：是拿近静音去合成，噪声被放大成假声。

这一轮要验的
    挑窗加电平门限后，素材回到真唱段（成男 170–195Hz、−18dBFS 左右）；
    这时只需要降 5 个半音（而不是 −15）就能落到用户的 p90。

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


#: 窗口内最安静那 20% 帧，允许比全场"响帧"（p90）低多少 dB。
#: 20dB 是"主歌到副歌 / 唱到气口"的量级；再低就是尾巴残响、淡出、静音。
MIN_REL_DB = 20.0


def rms_curve(y: np.ndarray, sr: int, hop: int = 320) -> np.ndarray:
    """16k / 20ms 帧的 RMS 包络，与 `f0_curve` 同一套帧格，便于逐帧对齐。"""
    y16 = librosa.resample(np.asarray(y, dtype=np.float64), orig_sr=sr, target_sr=16000)
    return librosa.feature.rms(y=y16.astype(np.float32), frame_length=1024,
                               hop_length=hop)[0].astype(np.float64)


def pick_window(y: np.ndarray, sr: int, dur: float = SEG_DUR,
                scan_from: float = SCAN_FROM,
                min_rel_db: float = MIN_REL_DB) -> tuple[float, float]:
    """挑「中位音高最高**且真的在唱**」的 dur 秒窗口，返回 (起点秒, 该窗 pyin 中位)。

    为什么按音高挑而不是按位置：用户抱怨的正是"夹着嗓子"，而"夹不夹"由音区决定。
    挑最高的一段 = 挑最可能翻车的一段，修复才可证伪。

    ★ 第一版用 YIN 挑，结果挑到 329.5s 且报 500Hz（= fmax 边界）—— 噪声帧的八度跳
      直接把"最高"这个判据骗了，而 pyin 复核同一窗口只有 178.8Hz。挑窗口必须和
      报数用同一把尺子（pyin），否则窗口选错、页面上的"源 330Hz"也不成立。
      另外 fmax 边界的帧一律剔除：那是失败帧，不是高音。

    ★★ 第二版（本函数上一版）仍有致命漏洞，2026-10-20 用户第二次裁决后实测发现：
      「只看音高、不看电平」= 让**歌曲结束后的残响/近似静音**赢下比赛。
      《成都》原曲 5:10 左右结束，视频尾巴还有 50 多秒的衰减，demucs 人声轨在
      275–300s 只剩 **−31 ~ −36dBFS**（正段落是 −15 ~ −17dB），pyin 在这些噪声帧上
      吐出 330–345Hz 的八度跳 —— 于是"最高音区"落在了一段根本没人唱的地方。
      后果不是"数字不准"，而是整轮实验的素材是噪声：Seed-VC 拿近静音去合成，
      听到的就是"童声 + 电"。用户那句「这段根本就不是赵雷的声音」是对的。
      修法：窗口 RMS 必须不低于**本次扫描区内所有候选窗口电平的中位数** + min_rel_db。
    """
    f0, fps = f0_curve(y, sr)
    rms = rms_curve(y, sr)
    n = min(len(f0), len(rms))
    f0, rms = f0[:n], rms[:n]
    lo = int(scan_from * fps)
    win = int(dur * fps)
    step = max(1, int(0.5 * fps))

    # 帧电平门限：窗口内**最安静的 20% 帧**也必须还在“响帧”的 20dB 以内。
    # 用均值/中位数都会被“5s 唱 + 15s 静音”这种拼接窗骗到（实测 −26.9dBFS 就混过了 −27.4 的门限），
    # 用低分位数则是直接问：这个窗口里有没有一段是空的。
    fdb = 20.0 * np.log10(rms + 1e-12)
    gate = float(np.percentile(fdb[lo:], 90)) - min_rel_db

    starts = list(range(lo, max(lo + 1, n - win), step))
    best, best_med = scan_from, -1.0
    for i in starts:
        if float(np.percentile(fdb[i:i + win], 20)) < gate:
            continue                     # 窗口里有静音/残响：不是一段可以当素材的“唱”
        seg = f0[i:i + win]
        v = seg[np.isfinite(seg)]
        v = v[(v > 70.0) & (v < 480.0)]
        if v.size < win * 0.4:           # 伴奏/气口为主：不算"唱上去"
            continue
        med = float(np.median(v))
        if med > best_med:
            best, best_med = i / fps, med
    return best, best_med


def loudest_window(y: np.ndarray, sr: int, dur: float = SEG_DUR,
                   scan_from: float = SCAN_FROM,
                   avoid: tuple[float, float] | None = None) -> float:
    """全曲最响的 dur 秒窗口起点 —— 用作"这首歌真正在唱的那一段"的参照。

    用途是自检：挑中的窗口如果与它**不是同一个人**，那就是又挑到残响了。
    只看电平不看音高，所以不会被噪声帧的八度跳骗到。

    `avoid=(t0, t1)` 把一段区间排除在外 —— 自检必须用**另一段**去比：
    不排除时最响窗常常就压在选中窗身上（实测挑中 292.5s、最响 288.5s），
    两段重叠 → CAM++ 0.9586 是必然的，什么都证明不了。
    """
    rms = rms_curve(y, sr)
    fps, lo = 50.0, int(scan_from * 50)
    win = max(1, int(dur * fps))
    best, best_db = float(scan_from), -1e9
    for i in range(lo, max(lo + 1, len(rms) - win), max(1, int(0.5 * fps))):
        t0 = i / fps
        if avoid and t0 + dur > avoid[0] and t0 < avoid[1]:
            continue
        db = float(20.0 * np.log10(np.mean(rms[i:i + win]) + 1e-12))
        if db > best_db:
            best, best_db = i / fps, db
    return best


def level_db(y: np.ndarray, sr: int, start: float, dur: float) -> float:
    """某个窗口的电平（dBFS）。用于在页面/日志里给出「这段真的在唱」的证据。"""
    rms = rms_curve(y, sr)
    fps = 50.0
    i, n = int(start * fps), max(1, int(dur * fps))
    seg = rms[i:i + n]
    if seg.size == 0:
        return float("nan")
    return float(20.0 * np.log10(np.mean(seg) + 1e-12))


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
    src_db = level_db(y, sr, start, SEG_DUR)

    ref_y, ref_sr = read(ref)
    ref16 = librosa.resample(np.asarray(ref_y, dtype=np.float64), orig_sr=ref_sr, target_sr=16000)
    ref_f0_curve, _ = librosa.pyin(ref16, fmin=60, fmax=600, sr=16000,
                                  frame_length=1024, hop_length=160)[:2]
    rv = ref_f0_curve[np.isfinite(ref_f0_curve)]
    target = float(np.percentile(rv, 90))          # 你的 p90：唱到这儿还不至于挤嗓子
    shift = shift_for(src_f0, target)

    print(f"\n=== 定调 ===\n源（{vocal.name}）{start:.1f}–{start + SEG_DUR:.1f}s  "
          f"电平 {src_db:.1f}dBFS  中位 {src_f0}Hz")
    print(f"你的参照中位 {float(np.median(rv)):.1f}Hz / p90 {target:.1f}Hz → 降 {abs(shift)} 半音"
          f"（{src_f0}Hz → {src_f0 * 2 ** (shift / 12):.0f}Hz）")

    # ── ①b 自检：挑中的这段和"全曲最响那段"是不是同一个人 ──────────────
    # 上一轮的素材就是靠这个没做而混过去的：整曲 CAM++ 很高，窗口却不在同一段。
    loud_start = loudest_window(y, sr, avoid=(start - 2 * SEG_DUR, start + 2 * SEG_DUR))
    win_vs_loud = None
    try:
        import cover_metrics as _cm

        loud = cut_resampled(vocal, WORK / "_loud.wav", loud_start, SEG_DUR)
        win_vs_loud = _cm.secs(src, loud)
    except Exception as e:  # noqa: BLE001
        print(f"  [自检] 同人检验失败：{e}")
    print(f"自检：新窗 vs 全曲最响段（{loud_start:.1f}s）CAM++ = {win_vs_loud}"
          f"（歌声不同人基线 0.39–0.47）")

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
    rows = [{"name": "源（正段落，原调）", "f0": src_f0, "hf_db": src_hf,
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
            "src_f0": src_f0, "src_db": round(src_db, 1),
            "loud_start": round(loud_start, 1), "win_vs_loud": win_vs_loud,
            "ref_median": round(float(np.median(rv)), 1),
            "ref_p90": round(target, 1), "rows": rows}
    (OUT / "register.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    import cover_register_page

    cover_register_page.build(OUT, meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
