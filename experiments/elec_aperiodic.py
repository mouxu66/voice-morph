# -*- coding: utf-8 -*-
"""电音归因的决定性对照：把「非周期成分」按剂量叠回去，看周期峰是否回落。

背景（2026-10-19，`media/covers/elec/elec_pitch.json`）
    同一条域内说话素材，只改请求音高（108→324Hz），RVC 输出的周期峰反而
    **单调上升**（0.670→0.869，输入 0.691），机械变调臂则平坦（0.711–0.720）。
    ⇒ 高音段不是"周期性崩了"，更像"太周期、太干净" —— 缺的是**气声/噪声**
    这类非周期成分，而不是基频能力。

本实验怎么证伪
    把源素材的**谐波**用梳状陷波扣掉，剩下就是它的非周期残差（气声、噪声、
    辅音摩擦）。把残差按 −18/−12/−6/−3dB 叠回那段"最像电音"的 RVC 输出：
      · 若周期峰随剂量**单调回落**、并靠近输入水平 → 机制成立（缺非周期成分）；
      · 若剂量扫到 −3dB 仍不动 → 这条解释不成立，别再往"加气声"方向投资。
    耳朵单独裁决"电音有没有变少" —— 数字只负责证明"我们真的改变了那个量"。

⚠️ 这不是产品加工，只是归因实验：残差来自**源**，在真实翻唱里源是原唱，
   直接叠回会带原唱音色。要产品化必须先验证"用模型自己的气声"是否等效。

用法：
    ./.venv/Scripts/python.exe experiments/elec_aperiodic.py \
        --source media/covers/elec/00_in_domain_40k.wav \
        --vocal  media/covers/elec/rvc_p+19.wav \
        --out    media/covers/elec/aperiodic
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "experiments"))

#: 默认剂量。−18dB 是 `cover_post_chain` 里气口叠回的现役值；再往上扫到 −3dB
#: 是为了回答"要多大剂量才动得了周期峰"，而不是只测一个点。
DEFAULT_GAINS = (-18.0, -12.0, -6.0, -3.0)

#: 陷波器在每根谐波两侧各挖掉几个 bin。
#: ★ 必须是 3：2048 点 Hann 窗的主瓣宽约 ±2 bin，只挖 ±1 会留下**旁瓣**，
#: 残差里仍是谐波（实测纯正弦残差 RMS 反而比原信号还大 3.5 倍）。
GUARD_BINS = 3


def harmonic_residual(
    x: np.ndarray,
    sr: int,
    f0: np.ndarray | None = None,
    *,
    n_fft: int = 2048,
    hop: int = 512,
) -> np.ndarray:
    """把 `x` 的谐波成分扣掉，返回非周期残差（与 `x` 等长）。

    实现是 STFT 域的**梳状陷波**：逐帧在 k·f0 的每个谐波 bin（含 Hann 主瓣
    的 ±GUARD_BINS）上置零，
    再 ISTFT 叠加。它不试图完美重建激励，只要求两件事：
      ① 谐波能量显著下降（否则残差里还全是"周期"）；
      ② 谐波之间的噪声/气声保留（否则叠回去只是变轻，不是变"有气"）。
    """
    x = np.asarray(x, dtype=np.float64)
    if x.size < n_fft:
        return np.zeros_like(x, dtype=np.float32)
    if f0 is None:
        import librosa

        f0, _, _ = librosa.pyin(
            x.astype(np.float32), fmin=50.0, fmax=1400.0, sr=sr,
            frame_length=n_fft, hop_length=hop,
        )
    f0 = np.asarray(f0, dtype=np.float64)
    win = np.hanning(n_fft)
    n_frames = 1 + (len(x) - n_fft) // hop
    out = np.zeros(len(x) + n_fft, dtype=np.float64)
    wsum = np.zeros(len(x) + n_fft, dtype=np.float64)
    nyq = sr / 2.0
    for i in range(n_frames):
        seg = x[i * hop:i * hop + n_fft]
        spec = np.fft.rfft(seg * win)
        fi = f0[min(i, len(f0) - 1)] if len(f0) else np.nan
        if np.isfinite(fi) and fi > 0:
            # 每根谐波的 bin 位置；只在 Nyquist 以内挖
            k = np.arange(1, int(nyq / fi) + 1)
            bins = np.rint(k * fi * n_fft / sr).astype(int)
            for b in bins:
                lo = max(0, b - GUARD_BINS)
                hi = min(len(spec), b + GUARD_BINS + 1)
                spec[lo:hi] = 0.0
        y = np.fft.irfft(spec, n=n_fft) * win
        out[i * hop:i * hop + n_fft] += y
        wsum[i * hop:i * hop + n_fft] += win ** 2
    # ★ 不能直接除：Hann 窗端点 = 0，开头 ~6ms 的 wsum 趋近 0，会放大成 60 倍
    #   （实测首样本 59.5，整段 RMS 从 0.039 被抬到 0.76）。窗还没累加到半幅的
    #   地方直接给 0 —— 那几毫秒本来也落在帧统计的静音门之下。
    scale = np.where(wsum > 0.05, 1.0 / np.maximum(wsum, 1e-6), 0.0)
    out = out[:len(x)] * scale[:len(x)]
    return out.astype(np.float32)


def mix_at_db(
    vocal: np.ndarray, layer: np.ndarray, gain_db: float, *, relative: bool = True
) -> np.ndarray:
    """把 `layer` 按 `gain_db` 叠进 `vocal`（长度取短边）。

    ★ `relative=True`（默认）：先把层的 RMS 对齐到人声，再按 dB 叠 ——
      否则 "−18dB" 是相对**残差自己的绝对电平**，而残差往往比人声低 20dB，
      叠上去等于没叠（实测 4 档周期峰全在 0.911，剂量根本没动）。
    """
    v = np.asarray(vocal, dtype=np.float32)
    layer = np.asarray(layer, dtype=np.float32)
    if relative:
        vr = float(np.sqrt((v.astype(np.float64) ** 2).mean()))
        lr = float(np.sqrt((layer.astype(np.float64) ** 2).mean()))
        if lr > 1e-12 and vr > 1e-12:
            layer = layer * (vr / lr)
    l = layer * (10.0 ** (gain_db / 20.0))
    n = min(len(v), len(l))
    out = v.copy()
    out[:n] = out[:n] + l[:n]
    peak = float(np.abs(out).max()) if out.size else 0.0
    if peak > 0.98:  # 防削波：只缩整体，不改变剂量之间的相对关系
        out = out * (0.98 / peak)
    return out.astype(np.float32)


def envelope_noise(vocal: np.ndarray, sr: int, *, seed: int = 0,
                   frame_ms: float = 20.0) -> np.ndarray:
    """用**人声包络整形的白噪声**当气声层（RMS 对齐到人声）。

    为什么不用梳状陷波残差：它保留的漏能是**冲激式**的（实测局部包络中位
    0.0012、峰值 0.51），逐帧对齐会把几个咔哒声放大 88 倍，剂量曲线被瞬态支配。
    归因实验要的是"非周期能量"这个单一变量，包络噪声更干净；源残差仍可作
    `--layer residual` 参考，但需限幅。
    """
    from scipy.ndimage import uniform_filter1d

    v = np.asarray(vocal, dtype=np.float32)
    win = max(8, int(sr * frame_ms / 1000.0))
    env = np.sqrt(np.maximum(uniform_filter1d(v.astype(np.float64) ** 2, win), 0.0))
    noise = np.random.default_rng(seed).normal(0.0, 1.0, len(v)) * env
    vr = float(np.sqrt((v.astype(np.float64) ** 2).mean()))
    nr = float(np.sqrt((noise ** 2).mean()))
    if nr > 1e-12:
        noise = noise * (vr / nr)
    return np.clip(noise, -0.98, 0.98).astype(np.float32)


def mix_at_db_envelope(
    vocal: np.ndarray,
    layer: np.ndarray,
    gain_db: float,
    sr: int,
    *,
    frame_ms: float = 20.0,
    smooth_ms: float = 50.0,
    floor_rel: float = 0.02,
) -> np.ndarray:
    """按**逐帧**包络把 `layer` 对齐到 `vocal`，再按 `gain_db` 叠（真正可听的剂量）。

    为什么不能只按整段 RMS 对齐：残差在气口/辅音处响、在长元音处轻，
    整段对齐后它在**响的帧**里等于没叠 —— 实测 4 档周期峰全在 0.911，
    而整段 `diff/vocal` 却有 0.13/0.25/0.44/0.58，说明剂量全落在静音段了。
    """
    from scipy.ndimage import uniform_filter1d

    v = np.asarray(vocal, dtype=np.float32)
    l = np.asarray(layer, dtype=np.float32)
    n = min(len(v), len(l))
    v, l = v[:n], l[:n]
    win = max(8, int(sr * frame_ms / 1000.0))
    # uniform_filter1d 的增量求和会在长零段留下 −1e-16 量级的负值
    v_env = np.sqrt(np.maximum(uniform_filter1d(v.astype(np.float64) ** 2, win), 0.0))
    l_env = np.sqrt(np.maximum(uniform_filter1d(l.astype(np.float64) ** 2, win), 0.0))
    floor = max(float(np.median(v_env)) * floor_rel, 1e-5)
    gain = np.where(
        (v_env > floor) & (l_env > 1e-9),
        (v_env / np.maximum(l_env, 1e-9)) * (10.0 ** (gain_db / 20.0)),
        0.0,
    )
    # 平滑增益，避免 20ms 一档的"抽吸"声（那会被听成另一种加工，不是气声）
    sw = max(3, int(sr * smooth_ms / 1000.0))
    gain = uniform_filter1d(gain, sw, mode="nearest")
    out = v + l * gain.astype(np.float32)
    peak = float(np.abs(out).max()) if out.size else 0.0
    if peak > 0.98:
        out = out * (0.98 / peak)
    return out.astype(np.float32)


def _read(path: Path) -> tuple[np.ndarray, int]:
    import soundfile as sf

    d, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return np.ascontiguousarray(d.mean(1), dtype=np.float32), int(sr)


def _resample(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return np.asarray(x, dtype=np.float32)
    from scipy.signal import resample_poly

    from math import gcd

    g = gcd(src_sr, dst_sr)
    return resample_poly(x, dst_sr // g, src_sr // g).astype(np.float32)


def _measure(path: Path) -> dict:
    """周期峰统计（与 `elec_analyse.py` 同一条尺子）。"""
    import elec_ruler

    s = elec_ruler.summarise(elec_ruler.frame_stats(path))
    return {k: s[k] for k in ("frames", "f0_median", "periodicity_p10",
                              "periodicity_p50", "periodicity_p90") if k in s}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="电音归因：非周期残差剂量叠回")
    ap.add_argument("--source", required=True, type=Path, help="域内源素材（模型自己的输入）")
    ap.add_argument("--vocal", required=True, type=Path, help="RVC 输出（通常是 pitch +19 那档）")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--gains", type=float, nargs="+", default=list(DEFAULT_GAINS))
    ap.add_argument("--layer", choices=("noise", "residual"), default="noise",
                    help="noise=包络白噪声（默认、干净）；residual=源谐波残差（有冲激）")
    ap.add_argument("--sr", type=int, default=40000)
    args = ap.parse_args()

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    import soundfile as sf

    src, src_sr = _read(args.source.resolve())
    voc, voc_sr = _read(args.vocal.resolve())
    src = _resample(src, src_sr, args.sr)
    voc = _resample(voc, voc_sr, args.sr)
    n = min(len(src), len(voc))
    src, voc = src[:n], voc[:n]

    res = harmonic_residual(src, args.sr)
    sf.write(str(out / "00_residual.wav"), res, args.sr)
    if args.layer == "noise":
        layer = envelope_noise(voc, args.sr)
        sf.write(str(out / "00_layer.wav"), layer, args.sr)
    else:
        # 限幅到 3×RMS：不消除冲激，但避免单个咔哒支配整段剂量
        rms = float(np.sqrt((res.astype(np.float64) ** 2).mean())) * 3.0
        layer = np.clip(res, -rms, rms).astype(np.float32)

    # 残差里还剩多少"周期"：直接用同一把尺子量 —— 它必须明显低于源，
    # 否则我们叠回去的还是谐波，剂量实验等于自我循环。
    src_p = _measure(args.source.resolve())
    res_p = _measure(out / "00_residual.wav")

    rows = [{"gain_db": None, "label": "input", "wav": str(args.source.resolve()), **_measure(args.source.resolve())},
            {"gain_db": None, "label": "rvc", "wav": str(args.vocal.resolve()), **_measure(args.vocal.resolve())}]
    for g in args.gains:
        p = out / f"rvc_plus_{abs(int(g)):02d}db.wav"
        sf.write(str(p), mix_at_db_envelope(voc, layer, g, args.sr), args.sr)
        rows.append({"gain_db": g, "label": f"rvc{g:+g}dB", "wav": str(p), **_measure(p)})
        print(f"[残差] {g:+6.1f}dB → 周期峰 p50={rows[-1].get('periodicity_p50')}")

    print(f"[残差] 源周期峰 p50={src_p.get('periodicity_p50')} · "
          f"残差 p50={res_p.get('periodicity_p50')}")
    (out / "aperiodic.json").write_text(
        json.dumps({"source": str(args.source), "vocal": str(args.vocal),
                    "layer": args.layer, "residual_periodicity": res_p, "rows": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[残差] → {out / 'aperiodic.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
