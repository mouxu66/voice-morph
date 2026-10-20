"""门控修复验证 v2：STFT 分频 + 实测拟合的补偿曲线。

v1 的 bug：用 `y - bandpass(y)` 做分频，4 阶 Butterworth 在通带边缘只衰 3dB，
`lo` 里残留了大量 8–16k 能量（实测门控后反而比原始高 4.4dB）。改用 STFT
在频域把 f≥8k 的 bin 直接乘增益，没有残差。

补偿曲线不再是手调：对每条帧取「0–8k 帧能量」作自变量，分别统计 D1 与原唱
在该电平上的 8–16k 电平，**差值就是需要的增益**。这等于直接对齐真人的
"高频随发声强度的动态曲线"（D1 斜率 0.315 vs 原唱 0.728）。

用法：
    ./.venv/Scripts/python.exe experiments/cover_elec_gate2.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
FULL = ROOT / "media/covers/midsong_20261018/chengdu_bwe_full"
REF = ROOT / "media/covers/midsong_20261018/candidates/成都-赵雷/vocals.wav"
SR = 40000

WIN = 2048           # STFT 帧长（51ms @40k）
HOP = 512            # 12.8ms
ENV_S = 0.060
CUT = 8000.0
GAIN_MIN, GAIN_MAX = -26.0, 4.0
NBINS = 24


def read(p: Path) -> tuple[np.ndarray, int]:
    y, sr = sf.read(str(p), dtype="float32", always_2d=True)
    return y.mean(axis=1).astype(np.float64), int(sr)


def frame_env(y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 (0–8k 帧能量 dB, 8–16k 帧电平 dB)，帧长/跳与 STFT 一致。"""
    win = np.hanning(WIN)
    scl = float((win ** 2).sum())
    freqs = np.fft.rfftfreq(WIN, 1.0 / SR)
    lo_m = freqs < CUT
    hi_m = (freqs >= CUT) & (freqs < 16000)
    nf = 1 + (len(y) - WIN) // HOP
    idx = np.arange(WIN)[None, :] + HOP * np.arange(nf)[:, None]
    seg = y[idx]
    seg = (seg - seg.mean(axis=1, keepdims=True)) * win
    p = np.abs(np.fft.rfft(seg, axis=1)) ** 2 / scl
    e_lo = 10 * np.log10(np.maximum(p[:, lo_m].sum(axis=1), 1e-20))
    e_hi = 10 * np.log10(np.maximum(p[:, hi_m].sum(axis=1), 1e-20))
    return e_lo, e_hi


def main() -> int:
    d, _ = read(FULL / "D1_bwe_post.wav")
    a, _ = read(FULL / "A_full_post.wav")
    ref, rsr = read(REF)
    if rsr != SR:
        ref = resample_poly(ref, SR, rsr)
    n = min(len(d), len(ref), len(a))
    d, a, ref = d[:n], a[:n], ref[:n]
    print(f"长度 {n/SR:.2f}s")

    e_lo_d, e_hi_d = frame_env(d)
    e_lo_r, e_hi_r = frame_env(ref)
    m = min(len(e_lo_d), len(e_lo_r))
    e_lo_d, e_hi_d = e_lo_d[:m], e_hi_d[:m]
    e_lo_r, e_hi_r = e_lo_r[:m], e_hi_r[:m]

    # 只在一方有真实内容时比较；两侧都极静时差值无意义（都在噪声底）
    valid = (e_lo_d > -70) & (e_lo_r > -70)
    print(f"有效帧 {valid.sum()} / {m}（{100*valid.mean():.1f}%）")

    # 分位分档 → 每档的原唱-D 差值
    qs = np.linspace(0, 100, NBINS + 1)
    edges = np.percentile(e_lo_d[valid], qs)
    xs, ys, ns = [], [], []
    for i in range(NBINS):
        lo_e, hi_e = edges[i], edges[i + 1]
        sel = valid & (e_lo_d >= lo_e) & (e_lo_d <= hi_e)
        if sel.sum() < 50:
            continue
        xs.append(float(np.median(e_lo_d[sel])))
        ys.append(float(np.median(e_hi_r[sel]) - np.median(e_hi_d[sel])))
        ns.append(int(sel.sum()))
    xs, ys = np.array(xs), np.array(ys)
    # 单调化 + 端点延展（真静音段按最左值外推，避免抽风）
    ys = np.maximum.accumulate(ys) if ys[0] < ys[-1] else ys
    print("\n=== 拟合出的补偿曲线（0–8k 帧能量 → 8k+ 需要加多少 dB）===")
    for x, yv, nn in zip(xs, ys, ns):
        print(f"  env {x:>7.1f} dB → gain {yv:>+6.1f} dB   (n={nn})")

    env_s = 10 * np.log10(np.maximum(
        np.convolve(10 ** (e_lo_d / 10), np.ones(5) / 5, mode="same"), 1e-20))

    def gain_of(env_db: np.ndarray) -> np.ndarray:
        g = np.interp(env_db, xs, ys, left=ys[0], right=ys[-1])
        return np.clip(g, GAIN_MIN, GAIN_MAX)

    g_frame = gain_of(env_s)

    # STFT 域施加（f≥8k 的 bin 乘增益，无残差）
    y = d
    win = np.hanning(WIN)
    nf = 1 + (len(y) - WIN) // HOP
    pad = np.zeros(nf * HOP + WIN - len(y) + HOP)
    yp = np.concatenate([y, pad])
    out = np.zeros_like(yp)
    wsum = np.zeros_like(yp)
    freqs = np.fft.rfftfreq(WIN, 1.0 / SR)
    hi_m = freqs >= CUT
    for i in range(nf):
        s = i * HOP
        S = np.fft.rfft(yp[s:s + WIN] * win)
        g = g_frame[min(i, len(g_frame) - 1)]
        S[hi_m] *= 10 ** (g / 20.0)
        out[s:s + WIN] += np.fft.irfft(S, WIN) * win
        wsum[s:s + WIN] += win ** 2
    gated = (out / np.maximum(wsum, 1e-8))[: len(y)]

    out_dir = FULL / "gate"
    out_dir.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_dir / "G2_stft_8k.wav"), gated.astype(np.float32), SR, subtype="PCM_16")

    # 结果核对：同帧集分档
    e_lo_g, e_hi_g = frame_env(gated)
    print("\n=== 分档核对（0–8k 能量档 → 8–16k 电平 dB）===")
    print(f"{'档':<16}{'D1':>9}{'G2门控':>9}{'A臂':>9}{'原唱':>9}{'需要':>9}")
    for name, a_, b_ in [("silent <−45", -1e9, -45), ("quiet −45~−35", -45, -35),
                         ("soft −35~−25", -35, -25), ("mid −25~−15", -25, -15),
                         ("loud −15~−5", -15, -5), ("peak >−5", -5, 1e9)]:
        sel = (e_lo_d > a_) & (e_lo_d <= b_)
        sel = sel[: min(len(sel), len(e_hi_g))]
        if sel.sum() < 30:
            continue
        dv = np.median(e_hi_d[sel])
        gv = np.median(e_hi_g[sel])
        rv = np.median(e_hi_r[sel])
        print(f"{name:<16}{dv:>9.1f}{gv:>9.1f}{'-':>9}{rv:>9.1f}{rv-dv:>+9.1f}")

    # 关键验证：气口的高频还剩多少，扁平度是否回到人声
    freqs2 = np.fft.rfftfreq(WIN, 1.0 / SR)
    hi_sel = (freqs2 >= 8000) & (freqs2 < 16000)
    idx = np.arange(WIN)[None, :] + HOP * np.arange(nf)[:, None]
    sil = (e_lo_d <= -45)[:nf]
    print("\n=== 气口段（0–8k < −45dB）的 8–16k 谱平坦度（纯音→0，呼吸噪声→0.5）===")
    for tag, sig in [("D1 BWE", d), ("G2 门控", gated), ("原唱", ref)]:
        seg = (sig[idx] - sig[idx].mean(axis=1, keepdims=True)) * win
        p = np.abs(np.fft.rfft(seg, axis=1)) ** 2
        sub = np.maximum(p[:, hi_sel], 1e-24)
        v = np.exp(np.mean(np.log(sub), axis=1)) / np.maximum(np.mean(sub, axis=1), 1e-24)
        v = v[:len(sil)]
        print(f"  {tag:<10} SFM {np.median(v[sil]):.4f}")

    (out_dir / "gate2_curve.json").write_text(
        json.dumps({"env_x": xs.tolist(), "gain_y": ys.tolist(), "n": ns},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n音频 {out_dir / 'G2_stft_8k.wav'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
