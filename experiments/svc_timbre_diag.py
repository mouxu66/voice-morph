"""SVC/变声输出的音色迁移诊断（客观判据，不靠人耳）。

背景：听感说「不像」时，需要先判明是「音色没迁移过去（源音残留）」还是
「迁移过去了但发声方式不对」。前者是 timbre leakage，后者是 prosody/formant。

用法：
  python experiments/svc_timbre_diag.py ltas    --ref <参考音> --src <源音> <输出...>
  python experiments/svc_timbre_diag.py spec    --ref <参考音> --src <源音> <输出...>
  python experiments/svc_timbre_diag.py forensic <音频...>

判据：
  ltas      LTAS 谱形相关（项目 m1e 调研判定音色迁移的主判据）。
            corr(输出, 源) >= corr(输出, 参考音) ⇒ 音色未迁移，源音残留。
  spec      带宽（-20/-40/-60dB 截止）、频带能量、谱倾斜、HNR、jitter/shimmer。
  forensic  合成音取证：静音底噪、精确零值占比、固定 hop 帧边界伪影。

踩过的坑（务必注意）：
  ★★ --src 必须是「与输出同段」的源音频。2026-09-28 实测：同一批 SaMoye 输出，
     拿 271s 全曲源当 --src 判出「音色未迁移」（vs源 0.964 / vs参考 0.801），
     换 30s 同段源当 --src 就翻成「迁移成功」（vs源 0.718 / vs参考 0.848）。
     两个数集都自洽，但结论相反 —— 因为 LTAS 是帧平均谱，
     对比对象时长差 88 倍时谱估计方差不对等，判据被系统性带偏。
  · LPC 求共振峰在高音歌唱上会「锁到谐波」——F0=240Hz 时 F1 测出 318 就是假象。
    共振峰只用于同元音、同素材横向比，不要拿歌唱 vs 说话比 F1 中位数。
  · SECS/x-vector 余弦在项目里量程塌缩（未转换底噪即 0.964），单独用会把
    「没转换」判得比「转换后」更相似 —— 必须配 LTAS。
  · 比 LTAS 时两边都要先去静音（丢最低 30% 能量帧），否则静音占比会主导结果。
  · 参考音与输出时长差一个量级时，元音分布不同会带来偏置，只做相对比较。

★ 更可靠的判据：CAM++ 说话人嵌入余弦（m2_server/speaker_sep.py 的 _sv_embed）。
  它是**池化后的固定 192 维向量，与音频时长无关**，口径天然公平，且与
  experiments/seedvc_shift_sweep.py 报出的 sim_ref 同源，数字可直接横向比。
  实测（2026-09-28，同段 60-90s）：SeedVC f0 s11 得 sim_ref 0.675 / sim_src 0.204
  （分离度 +0.471），而 SaMoye 四个参考音版本只有 0.19~0.31 / 0.27~0.33（分离度 ≈0）——
  LTAS 两套口径都没能区分出这么大的差距。**先看 CAM++，再用 LTAS 看细节。**
"""
import sys
import os
import glob
import numpy as np
import soundfile as sf
import librosa

SR = 16000
F_LO, F_HI = 100.0, 6000.0
N_BANDS = 48


def _mono(path, sr_target=SR):
    d, sr = sf.read(path, dtype="float32")
    if d.ndim > 1:
        d = d.mean(axis=1)
    if sr != sr_target:
        d = librosa.resample(d, orig_sr=sr, target_sr=sr_target)
    return d, sr


def ltas_vec(path, sr_target=SR):
    d, sr0 = sf.read(path, dtype="float32")
    if d.ndim > 1:
        d = d.mean(axis=1)
    if sr0 != sr_target:
        d = librosa.resample(d, orig_sr=sr0, target_sr=sr_target)
    n, hop = 1024, 256
    fr = librosa.util.frame(d, frame_length=n, hop_length=hop)
    rms = np.sqrt((fr ** 2).mean(axis=0))
    fr = fr[:, rms >= np.percentile(rms, 30)]
    if fr.shape[1] < 5:
        return None
    spec = (np.abs(np.fft.rfft(fr * np.hanning(n)[:, None], axis=0)) ** 2).mean(axis=1)
    fq = np.fft.rfftfreq(n, 1 / sr_target)
    grid = np.logspace(np.log10(F_LO), np.log10(F_HI), N_BANDS)
    v = np.interp(grid, fq, 10 * np.log10(spec + 1e-12))
    return (v - v.mean()) / (v.std() + 1e-12)


def corr(a, b):
    return float(np.corrcoef(a, b)[0, 1])


def _dur(path):
    """读时长（秒），读不到返回 None。"""
    try:
        info = sf.info(path)
        return info.frames / info.samplerate
    except Exception:
        return None


def cmd_ltas(ref, src, outs):
    r, s = ltas_vec(ref), ltas_vec(src)

    # ★ 时长对等检查 —— 对比对象时长差一个量级会让判定翻转（见文件头 ★★ 条）
    d_ref, d_src = _dur(ref), _dur(src)
    if d_ref and d_src and max(d_ref, d_src) / max(min(d_ref, d_src), 1e-9) >= 8:
        print(f"⚠️  时长不对等：参考音 {d_ref:.1f}s vs 源 {d_src:.1f}s（差 "
              f"{max(d_ref, d_src)/max(min(d_ref, d_src), 1e-9):.0f} 倍）。")
        print("    判定可能反向 —— 请让 --src 用与输出**同段**的源音频，或改用 CAM++ 判据。")
        print()
    if d_src:
        for p in outs[:1]:
            d_o = _dur(p)
            if d_o and max(d_o, d_src) / max(min(d_o, d_src), 1e-9) >= 8:
                print(f"⚠️  输出 {d_o:.1f}s 与 --src {d_src:.1f}s 不同段，结果不可用。")
                print()

    print(f"{'素材':40s} {'vs参考音':>9s} {'vs源音':>9s}  判定")
    for p in outs:
        o = ltas_vec(p)
        if o is None:
            print(f"{os.path.basename(p):40s}   (帧不足，跳过)")
            continue
        cr, cs = corr(o, r), corr(o, s)
        v = "← 更像源（音色未迁移）" if cs >= cr else "更像参考"
        print(f"{os.path.basename(p):40s} {cr:9.3f} {cs:9.3f}  {v}")


def cmd_spec(ref, src, outs):
    def one(p, label):
        d, sr0 = sf.read(p, dtype="float32")
        if d.ndim > 1:
            d = d.mean(axis=1)
        n = 2048
        fr = librosa.util.frame(d, frame_length=n, hop_length=512)
        rms = np.sqrt((fr ** 2).mean(axis=0))
        fr = fr[:, rms >= np.percentile(rms, 30)]
        spec = (np.abs(np.fft.rfft(fr * np.hanning(n)[:, None], axis=0)) ** 2).mean(axis=1)
        fq = np.fft.rfftfreq(n, 1 / sr0)
        pk = spec.max()
        cut = {}
        for db in (-20, -40, -60):
            i = np.where(spec > pk * 10 ** (db / 20))[0]
            cut[db] = fq[i[-1]] if len(i) else 0.0
        bands = [(0, 4000), (4000, 6000), (6000, 8000), (8000, min(10000, sr0 / 2))]
        tot = spec.sum() + 1e-12
        pct = [spec[(fq >= lo) & (fq < hi)].sum() / tot * 100 for lo, hi in bands]
        m = (fq > 100) & (fq < min(cut[-40], sr0 / 2 - 100))
        tilt = float(np.polyfit(np.log2(fq[m] / 100), 10 * np.log10(spec[m] + 1e-12), 1)[0])
        f0 = librosa.pyin(d, fmin=60, fmax=800, sr=sr0, frame_length=2048)[0]
        f0v = f0[np.isfinite(f0) & (f0 > 0)]
        print(f"{label:34s} sr={sr0:5d} dur={len(d)/sr0:6.1f}s F0={np.median(f0v):6.1f} "
              f"cut -20/-40/-60={cut[-20]:5.0f}/{cut[-40]:5.0f}/{cut[-60]:5.0f} "
              f"tilt={tilt:+5.1f} >4k={pct[1]+pct[2]+pct[3]:5.2f}%")

    for p, l in [(ref, "参考音"), (src, "源音")] + [(o, os.path.basename(o)) for o in outs]:
        if os.path.exists(p):
            one(p, l)


def cmd_forensic(paths):
    for p in paths:
        d, sr = _mono(p, 22050)
        d = d / (np.abs(d).max() + 1e-9)
        n, hop = 512, 256
        fr = librosa.util.frame(d, frame_length=n, hop_length=hop)
        rms = np.sqrt((fr ** 2).mean(axis=0))
        quiet = fr[:, rms <= np.percentile(rms, 10)]
        floor = float(np.sqrt((quiet ** 2).mean())) if quiet.size else 0.0
        zero = float((d == 0).mean() * 100)
        diff = np.abs(np.diff(d))
        best = (0.0, 0)
        for h in (128, 160, 200, 256, 320, 400, 512, 640):
            if h >= len(diff):
                continue
            seg = diff[: (len(diff) // h) * h].reshape(-1, h)
            col = seg.mean(axis=0)
            ratio = float(col.max() / (col.mean() + 1e-12))
            if ratio > best[0]:
                best = (ratio, h)
        print(f"{os.path.basename(p):44s} 静音底噪={floor:.2e} 精确0={zero:5.3f}% "
              f"帧边界峰/均={best[0]:.3f}@hop{best[1]}")


def _expand(paths):
    out = []
    for p in paths:
        out.extend(sorted(glob.glob(os.path.join(p, "*.wav"))) if os.path.isdir(p) else [p])
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        sys.exit(0)
    mode = a[0]
    rest = a[1:]
    if mode in ("ltas", "spec"):
        ref = rest[rest.index("--ref") + 1] if "--ref" in rest else None
        src = rest[rest.index("--src") + 1] if "--src" in rest else None
        outs = [x for i, x in enumerate(rest)
                if x not in ("--ref", "--src") and (i == 0 or rest[i - 1] not in ("--ref", "--src"))]
        outs = _expand(outs)
        (cmd_ltas if mode == "ltas" else cmd_spec)(ref, src, outs)
    elif mode == "forensic":
        cmd_forensic(_expand(rest))
    else:
        print(__doc__)
