"""混合臂：换参照（RVC 产物当参照）+ 频带拼接（RVC 低频 + 别人高频）。

为什么补这一轮
    前几轮已经证明两件事：
      ① RVC 用他自己的素材训过，**像**（CAM++ 0.717），但高频跟随差（slope 0.52）；
      ② Seed-VC / SoulX 高频更像真嗓音（slope 0.75 / 0.95），但"像"只到 0.62–0.80。
    于是有两个不用重训就能试的组合：
      A. **换参照**：把 RVC 转好的产物（已是他的音色、且干净）喂给 Seed-VC / SoulX
         当参照 —— 参照音本身更像、音区也对，零样本模型只需要搬过去。
      B. **频带拼接**：6.5k 以下全用 RVC（保住音色与咬字），6.5k 以上换成
         Seed-VC / SoulX（补气口、齿音、空气感）。拼接点放在 6.5k，是因为
         RVC 的 8–12k 只有 −82.4dB 而 slope 0.52，问题集中在高频段。

这一轮不重训任何模型，全部是推理或信号处理，产物直接进盲听页。

用法
    .venv/Scripts/python.exe experiments/cover_hybrid.py            # 生成缺失臂 + 度量 + 页面
    .venv/Scripts/python.exe experiments/cover_hybrid.py --measure  # 只度量 + 出页面
"""

from __future__ import annotations

import argparse
import json
import random
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
import cover_register as REG  # noqa: E402
import cover_seedvc_probe as P  # noqa: E402
import cover_soulx as SX  # noqa: E402
import cover_voice_ab as AB  # noqa: E402

BASE = SX.BASE
OUT = BASE / "hybrid"
SRC = SX.SRC
VOICE_AB = SX.VOICE_AB
ANCHOR = SX.ANCHOR

SEED = 20261022
#: 拼接点：以下用 RVC，以上用零样本模型
CROSSOVER = 6500.0

RVC_SINGDOM = VOICE_AB / "singdom_p0.wav"
SEEDVC_BEST = BASE / "seedvc_sweep" / "concat_cfg10_s50.wav"
SEEDVC_CONCAT_REF = BASE / "seedvc_sweep" / "ref_concat_37_62.wav"
SOULX_BEST = SX.OUT / "soulx_ref37_noshift_cfg4_s48.wav"

#: (key, 说明) —— 说明只进 json，不进盲听页
ARMS = [
    ("rvc_singdom_p0", "RVC 现役（他的素材训的）"),
    ("seedvc_concat", "Seed-VC，拼接真唱参照，cfg1.0/50"),
    ("seedvc_ref_rvc", "Seed-VC，参照=RVC 产物（音色已像、音区已对）"),
    ("seedvc_ref_up4", "Seed-VC，拼接参照升 4 半音（对齐源的音区）"),
    ("soulx_prompt_rvc", "SoulX，参照=RVC 产物"),
    ("soulx_prompt_seedvc", "SoulX，参照=Seed-VC 产物"),
    ("blend_rvc_seedvc", "合成：6.5k 以下 RVC，以上 Seed-VC"),
    ("blend_rvc_soulx", "合成：6.5k 以下 RVC，以上 SoulX"),
]


def _align_lag(a: np.ndarray, b: np.ndarray, sr: int, max_lag: float = 0.30) -> int:
    """样本级互相关求 B 相对 A 的延迟（正=B 晚）。

    1ms 包络的精度不够：高频（8–12k）包络跟着低频走，差 2–3ms 就足以把
    `r_env`/`slope` 从 0.73 打到 0.19。这里在 300–3000Hz 上用 FFT 互相关，
    取样本级峰值。
    """
    from scipy.signal import butter, correlate, sosfiltfilt

    sos = butter(4, [300, 3000], btype="band", fs=sr, output="sos")
    x = sosfiltfilt(sos, a)
    y = sosfiltfilt(sos, b)
    c = correlate(y, x, mode="full", method="fft")
    idx = int(np.argmax(c)) - (len(x) - 1)
    return int(np.clip(idx, -int(max_lag * sr), int(max_lag * sr)))


def _hf_gain_db(a: np.ndarray, b: np.ndarray, sr: int = 44100) -> float:
    """把 B 的 6.5–12k 中位电平对齐到 A（保住 RVC 的高频总量，只借时间结构）。"""
    n_fft = 2048
    A = np.abs(librosa.stft(a, n_fft=n_fft, hop_length=512))
    B = np.abs(librosa.stft(b, n_fft=n_fft, hop_length=512))
    f = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    band = (f >= 6500) & (f < 12000)
    pa = A[band].sum(axis=0)
    pb = B[band].sum(axis=0)
    sel = pa >= np.percentile(pa, 50)
    db = 10 * np.log10(max(float(np.median(pa[sel])), 1e-20)) - \
        10 * np.log10(max(float(np.median(pb[sel])), 1e-20))
    return float(np.clip(db, -12.0, 12.0))


def blend_band(a: Path, b: Path, out: Path, crossover: float = CROSSOVER) -> Path:
    """A 提供 6.5k 以下，B 提供 6.5k 以上 —— 用零相位 FIR 在时域分频。

    第一版在 STFT 上直接换幅度+相位，CAM++ 从 0.717 掉到 0.55、slope 归零；
    第二版保留 A 的相位换 B 的幅度，仍掉到 0.65。根因是**两侧相位在拼接点
    不连续**：ISTFT 出来的波形不再周期。时域互补滤波不碰相位：
    低通取 A、高通取 B，两个分量共享同一条 F0，叠加后周期性完整。
    B 若是 24k（SoulX），先重采样到 A 的采样率，否则会被当成 44.1k 放慢。
    """
    from scipy.signal import fftconvolve, firwin

    ya, sr = sf.read(str(a), dtype="float32", always_2d=True)
    ya = ya.mean(axis=1)
    yb, sr_b = sf.read(str(b), dtype="float32", always_2d=True)
    yb = yb.mean(axis=1)
    if sr_b != sr:
        yb = librosa.resample(yb, orig_sr=sr_b, target_sr=sr)
    n = min(len(ya), len(yb))
    ya, yb = ya[:n], yb[:n]
    lag = _align_lag(ya, yb, sr)
    if lag > 0:            # B 晚了 ⇒ 把 B 提前
        yb = np.concatenate([yb[lag:], np.zeros(lag, dtype=np.float32)])
    elif lag < 0:          # B 早了 ⇒ 把 B 推后
        k = -lag
        yb = np.concatenate([np.zeros(k, dtype=np.float32), yb[:-k]])
    gain = 10 ** (_hf_gain_db(ya, yb, sr) / 20.0)

    h = firwin(1023, crossover, fs=sr, window="hann")
    low_a = fftconvolve(ya, h, mode="same")
    high_b = yb - fftconvolve(yb, h, mode="same")
    y = low_a + high_b * gain
    out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out), y.astype(np.float32), sr, subtype="PCM_16")
    return out


def _seedvc(key: str, ref: Path) -> Path:
    dst = OUT / f"{key}.wav"
    if dst.exists() and dst.stat().st_mtime >= ref.stat().st_mtime:
        return dst
    tmp = OUT / f"_seed_{key}"
    if tmp.exists():
        shutil.rmtree(tmp)
    got = P.run_seedvc(SRC.resolve(), ref.resolve(), tmp.resolve(), steps=50, cfg=1.0)
    shutil.move(str(got), str(dst))
    shutil.rmtree(tmp, ignore_errors=True)
    return dst


def _up4() -> Path:
    """拼接参照升 4 半音（不升源：源是歌，不能改调）。"""
    dst = OUT / "ref_concat_up4.wav"
    if dst.exists() and dst.stat().st_mtime >= SEEDVC_CONCAT_REF.stat().st_mtime:
        return dst
    y, sr = sf.read(str(SEEDVC_CONCAT_REF), dtype="float32", always_2d=True)
    y = y.mean(axis=1)
    y = librosa.effects.pitch_shift(y=y, sr=sr, n_steps=4.0)
    OUT.mkdir(parents=True, exist_ok=True)
    sf.write(str(dst), y.astype(np.float32), sr, subtype="PCM_16")
    return dst


def _soulx_prompt(key: str, prompt_src: Path) -> Path:
    prompt = SX.ensure_target(prompt_src, OUT / f"prompt_{key}.wav")
    got = SX.ensure_arm(key, (0.0, 0.0), False, 3.0, 32,
                        target=OUT / "target.wav", prompt=prompt)
    dst = OUT / f"{key}.wav"
    if got != dst:
        shutil.copy2(got, dst)
    return dst


def ensure_all(measure: bool) -> dict[str, Path]:
    """生成（或复用）八条臂，返回 key→wav。"""
    OUT.mkdir(parents=True, exist_ok=True)
    SX.ensure_target(SRC, OUT / "target.wav")
    paths: dict[str, Path] = {}
    for key, _note in ARMS:
        dst = {"rvc_singdom_p0": RVC_SINGDOM, "seedvc_concat": SEEDVC_BEST}.get(
            key, OUT / f"{key}.wav")
        if not measure:
            print(f"  · {key} …", flush=True)
            if key == "seedvc_ref_rvc":
                dst = _seedvc(key, RVC_SINGDOM)
            elif key == "seedvc_ref_up4":
                dst = _seedvc(key, _up4())
            elif key == "soulx_prompt_rvc":
                dst = _soulx_prompt(key, RVC_SINGDOM)
            elif key == "soulx_prompt_seedvc":
                dst = _soulx_prompt(key, SEEDVC_BEST)
            elif key == "blend_rvc_seedvc":
                dst = blend_band(RVC_SINGDOM, SEEDVC_BEST, dst)
            elif key == "blend_rvc_soulx":
                dst = blend_band(RVC_SINGDOM, SOULX_BEST, dst)
            elif key == "seedvc_concat":
                dst = SEEDVC_BEST
            elif key == "rvc_singdom_p0":
                dst = RVC_SINGDOM
        if not dst.exists():
            raise SystemExit(f"缺臂 {key}：{dst}（去掉 --measure 先生成）")
        paths[key] = dst
    return paths


def build_key(rows: list[dict], order: list[int]) -> dict:
    shuffled = [rows[i] for i in order]
    return {
        "src": {"window": SX.WINDOW, "f0": REG.median_f0(SRC)},
        "seed": SEED,
        "mapfile": "answer.json",
        "map": {AB.LABELS[i]: r["key"] for i, r in enumerate(shuffled)},
        "rows": {r["key"]: {k: v for k, v in r.items() if k not in ("key", "path")}
                 for r in rows},
        "notes": dict(ARMS),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--measure", action="store_true")
    args = ap.parse_args()

    paths = ensure_all(args.measure)
    rows = []
    for key, note in ARMS:
        p = paths[key]
        r = AB.measure(p, ANCHOR)
        r.update({k: v for k, v in T.tracking(*M.read(p)).items()
                  if k in ("r_env", "slope", "gap_drop_db")})
        r.update({"key": key, "path": str(p), "note": note})
        rows.append(r)
        print(f"{key:<24}{r['f0']:>6.0f}Hz cam {r['cam_anchor']:.3f}  "
              f"hf {r['hf_db']:>6.1f}  r_env {r['r_env']:.2f} slope {r['slope']:.2f} "
              f"gap {r['gap_drop_db']:.1f}")

    order = list(range(len(rows)))
    random.Random(SEED).shuffle(order)
    key = build_key(rows, order)
    page = AB.build_page([rows[i] for i in order], ANCHOR, key, OUT)
    (OUT / "answer.json").write_text(json.dumps(key, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    print(f"\n页面：{page}\n映射：{OUT / 'answer.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
