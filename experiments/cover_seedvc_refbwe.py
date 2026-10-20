"""把「音色」和「高频带宽」拆开：先补你自己的参照音，再喂 Seed-VC。

上一轮（m1u §9）已经证明：Seed-VC 输出的高频完全由**参照音的带宽**决定。
但那条对照用的参照音是别人的 —— 高频对了，音色当然不像你。这就把两件事绑死在
同一个变量上了：想要高频，就得换人；想要像你，就得没有高频。

这一轮要证伪的就是这个"绑定"：**带宽是参照音的属性，不是音色的属性。**
做法是先把你的 22.05k 参照音用 BWE 补到 44.1k，音色不变、带宽上去，再喂给 Seed-VC。

已知风险（这是本实验真正要回答的）：
  · BWE 在 5k 断崖的素材上会造"气口纯音"（m1s 的结论）。参考音被污染后，
    Seed-VC 会不会把它当"音色"忠实复制，输出又变嘶？
  · 如果会，说明这条捷径不成立，只能重录 —— 那也是一个明确答案。

三个臂，同一段源、同一个模型，只换参照音：
  c = 你的 22.05k 原始参照     （音色对、带宽空）
  d = 别人的全带宽参照         （带宽对、音色错）
  e = 你的参照先 BWE 到 44.1k  （想同时拿到两个）
  f = 你的参照 + 全带宽参照拼接（便宜的对照：模型会不会平均出"半个你"）
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import cover_hf_tracking as H  # noqa: E402
import cover_seedvc_probe as P  # noqa: E402

OUT = ROOT / "media" / "covers" / "midsong_20261018" / "seedvc_probe"
WORK = OUT / "_raw3"
UNIVERSR = ROOT / "tools" / "universr_enhance.py"
#: BWE 模型的输入采样率档位只有 8/12/16/24k。参照音是 22.05k，
#: 降到 16k 丢的是 8k 以上 —— 而那个文件 8k 以上本来就是 −95.9dB 的空频带，损失可忽略
BWE_IN_SR = 16000


def _resample(y: np.ndarray, sr: int, target: int) -> np.ndarray:
    if int(sr) == target:
        return np.asarray(y, dtype=np.float32)
    return librosa.resample(np.asarray(y, dtype=np.float32), orig_sr=int(sr), target_sr=target)


def ref_bwe(ref: Path, out: Path) -> Path:
    """你的参照音 → 16k → UniverSR 48k → 44.1k。音色不动，只补带宽。"""
    WORK.mkdir(parents=True, exist_ok=True)
    y, sr = P.M.read(ref)
    mono16 = _resample(y, sr, BWE_IN_SR)
    src = WORK / "ref16.wav"
    sf.write(str(src), mono16, BWE_IN_SR, subtype="PCM_16")
    up48 = WORK / "ref48.wav"
    r = subprocess.run(
        [
            sys.executable,
            str(UNIVERSR),
            "--input",
            str(src.resolve()),
            "--output",
            str(up48.resolve()),
            "--input-sr",
            str(BWE_IN_SR),
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0 or not up48.exists():
        print(r.stdout[-2000:], r.stderr[-2000:], file=sys.stderr)
        raise SystemExit("UniverSR 失败")
    y48, sr48 = P.M.read(up48)
    sf.write(str(out), _resample(y48, sr48, 44100), 44100, subtype="FLOAT")
    return out


def ref_concat(a: Path, b: Path, out: Path) -> Path:
    """把两段参照音首尾接起来，看模型是"平均音色"还是"偏某一半"。"""
    ya, sra = P.M.read(a)
    yb, srb = P.M.read(b)
    ya, yb = _resample(ya, sra, 44100), _resample(yb, srb, 44100)
    sf.write(str(out), np.concatenate([ya, yb]), 44100, subtype="FLOAT")
    return out


def report(path: Path, ref: Path) -> dict:
    y, sr = P.M.read(path)
    t = H.tracking(y, sr)
    bands = P.analyze(y, sr)["bands"]
    row = {"name": path.stem, "hf_db": bands["8000-12000"], "hf_track": t}
    try:
        import cover_metrics

        row["secs_cos"] = cover_metrics.secs(path, ref)
    except Exception as e:  # noqa: BLE001
        row["secs_cos"] = f"n/a（{type(e).__name__}）"
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=str(ROOT / "media/voicebank/myvoice_supp2026/reference.wav"))
    ap.add_argument(
        "--fullband", default=str(ROOT / "media/voicebank/vbsyn_trial_ssb0033_60s/reference.wav")
    )
    ap.add_argument("--measure", action="store_true")
    args = ap.parse_args()

    start, dur, tag = P.SEG
    OUT.mkdir(parents=True, exist_ok=True)
    src = (OUT / f"{tag}_source.wav").resolve()
    if not src.exists():
        src = P.cut(P.M.REF, OUT / f"{tag}_source.wav", start, dur).resolve()
    ref = Path(args.ref).resolve()
    fb = Path(args.fullband).resolve()

    e_ref = ref_bwe(ref, WORK / "ref_bwe44.wav").resolve()
    f_ref = ref_concat(ref, fb, WORK / "ref_cat.wav").resolve()

    jobs = [("e_你的参照BWE", e_ref), ("f_拼接参照", f_ref)]
    for name, r in jobs:
        dst = OUT / f"{name}.wav"
        if args.measure and dst.exists():
            continue
        if dst.exists():
            dst.unlink()
        shutil.move(str(P.run_seedvc(src, r, (WORK / name).resolve())), str(dst))

    rows = []
    print(f"\n=== 参照音带宽 vs 音色（{start:.0f}–{start + dur:.0f}s，同源同模型）===")
    print(f"{'':<22}{'8–12k':>8}{'r_env':>8}{'slope':>7}{'气口掉':>8}{'CAM++':>8}  判读")
    sets = [
        ("c_你的22k参照", OUT / "c_tail_seedvc.wav"),
        ("d_别人的全带宽", OUT / "c_tail_seedvc_fullband.wav"),
        ("e_你的参照BWE", OUT / "e_你的参照BWE.wav"),
        ("f_拼接参照", OUT / "f_拼接参照.wav"),
    ]
    for name, p in sets:
        if not p.exists():
            print(f"{name:<22}{'缺产物':>8}")
            continue
        r = report(p, ref.resolve())
        r["name"] = name
        rows.append(r)
        t, cos = r["hf_track"], r["secs_cos"]
        cos_s = f"{cos:>8.3f}" if isinstance(cos, float) else f"{cos:>8}"
        print(
            f"{name:<22}{r['hf_db']:>8.1f}{t['r_env']:>8.2f}{t['slope']:>7.2f}"
            f"{t['gap_drop_db']:>8.1f}{cos_s}  {H.verdict(t, -52.5)}"
        )

    with open(OUT / "refbwe.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print("\n关键看两件事：e 的 CAM++ 是否接近 c（音色还是你的），8–12k 是否接近 d（带宽上去了）。")
    print("两个都成立 = 不用重录就能两全；只有带宽成立而音色跑了 = BWE 把音色也改了，要查。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
