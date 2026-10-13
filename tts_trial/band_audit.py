# -*- coding: utf-8 -*-
"""素材带宽体检：判断一份人声素材「能不能喂 40k RVC」。

为什么需要（2026-10-13 实测发现）
--------------------------------
RVC 是 40k 模型，但它的**前端硬编码 16kHz**（`offline_vc_infer.py` 用
`load_audio(src, 16000)`，`pipeline.change_rms(audio, 16000, ...)`）——
即信息瓶颈只有 **8kHz**；40k 声码器是在"重建"高频，重建得像不像完全取决于
**训练集里高频长什么样**。

于是出现一条反直觉的坑：**千问 TTS 输出里本来有 8~10kHz 的内容（实测 -53dB），
过一遍 RVC 反被抹成 -72dB**（RVC 把训练集的限带"烙"进了输出）。
在限带素材上继续加时长，只会把同一个天花板练得更牢。

判据用**逐频点绝对 dB**（项目 MEMORY.md 的规矩）：
  · 不用"99.5% 累积能量带宽" —— 语音能量本就集中，累积法区分不开；
  · 参考带取 400–800Hz，只统计活跃帧（≥60 分位 RMS），避免静音拉低高频。

阈值：
  · ≥8kHz 仍有内容（> -60dB）→ 够喂 40k；
  · 6–8kHz 已 < -45dB     → 明显限带，40k 的高频全靠"猜"；
  · 与本人另一份素材对比更能说明问题（同人不同设备）。

用法：
    D:\\RVC\\.venv\\Scripts\\python.exe tts_trial\\band_audit.py <目录或wav> [...]
"""
import argparse
import os
import sys

import numpy as np
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")

#: 频带（Hz），从低到高；第一带用作 0dB 参考。
REF_BAND = (400, 800)
BANDS = [(400, 800), (800, 1600), (1600, 2400), (2400, 3200), (3200, 4800),
         (4800, 6400), (6400, 8000), (8000, 10000), (10000, 12000),
         (12000, 14000), (14000, 16000), (16000, 18000)]
#: "这个频带算不算有内容"的门限（相对参考带的 dB）—— 用于报"内容上界"，仅供参考。
#:
#: ★ 标定依据（2026-10-13 实测，见 tests/test_band_audit.py 的砖墙用例）：
#: 4096 点 Hann 窗 + 砖墙低通时，**紧邻截止频率的那一个频带**会因窗的主瓣
#: 落在截止点附近而读到 **-30 ~ -36dB**，再往上立刻掉到 -87dB。
#: 也就是说"泄漏地板"本身就在 -30dB 附近。取 -20dB 是为了让
#: "内容上界"不被泄漏抬高（偏保守）。
PRESENT_DB = -20.0
#: 「高频是否为空」的门限：用于**远离语音主能量**的高频带（12-14k 起）。
#:
#: 取 -50dB 的依据（2026-10-13 标定）：
#:   · 纯窗泄漏地板（砖墙/零高频）= -87dB → 远低于门限，不会被误判"有高频"
#:   · 本 PPT 素材（高频空但有底噪）= -67dB → 命中"高频为空" ✅
#:   · 全带宽白噪声 = 0dB → 判"充足" ✅
#: 注意：**不要用 8-10k 判**，那一带被低频的窗主瓣抬到 -36dB，
#: 与高频里到底有没有内容无关（实测两种截然不同的信号读出同一个 -36.0dB）。
HF_EMPTY_DB = -50.0
#: 40k RVC 的实际信息瓶颈（前端 16kHz）。
RVC_FRONTEND_HZ = 8000


def read_mono(path):
    a, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return a.mean(axis=1), int(sr)


def band_levels(path, nfft=4096, active_pct=60.0):
    """返回 (sr, {频带上界: 相对 dB}, 活跃帧数)。数据不足返回 (sr, None, 0)。"""
    a, sr = read_mono(path)
    if len(a) < nfft * 3:
        return sr, None, 0
    hop = nfft // 2
    win = np.hanning(nfft)
    nf = (len(a) - nfft) // hop
    rms = np.array([np.sqrt(np.mean(a[i * hop:i * hop + nfft] ** 2)) for i in range(nf)])
    thr = float(np.percentile(rms, active_pct))
    psd = np.zeros(nfft // 2 + 1)
    used = 0
    for i in range(nf):
        if rms[i] < thr or rms[i] <= 0:
            continue
        psd += np.abs(np.fft.rfft(a[i * hop:i * hop + nfft] * win)) ** 2
        used += 1
    if used == 0:
        return sr, None, 0
    psd /= used
    f = np.fft.rfftfreq(nfft, 1 / sr)
    ref_m = (f >= REF_BAND[0]) & (f < REF_BAND[1])
    ref = float(np.mean(psd[ref_m])) + 1e-30
    out = {}
    for lo, hi in BANDS:
        if hi > sr / 2:
            continue
        m = (f >= lo) & (f < hi)
        if m.any():
            out[hi] = 10 * np.log10((float(np.mean(psd[m])) + 1e-30) / ref)
    return sr, out, used


def content_ceiling_hz(levels):
    """有内容（> PRESENT_DB）的最高频带上界；都没有则 None。"""
    top = None
    for hi in sorted(levels):
        if levels[hi] > PRESENT_DB:
            top = hi
    return top


def slope_report(levels):
    """高频滚降报告 —— 真实人声是**渐变**滚降，不是砖墙，所以不该只报一个"截止点"。

    返回 dict：
      hf_8_10k   8-10kHz 相对参考带的 dB（None = 素材采样率到不了这一带）
      drop_6_10  从 6-8k 到 8-10k 的落差（越小越"空"）
      ceiling    按 PRESENT_DB 算出的"还有内容"的最高上界（供参考）
    """
    b8, b6 = levels.get(10000), levels.get(8000)
    return {
        "hf_8_10k": b8,
        "drop_6_10k": (b8 - b6) if (b6 is not None and b8 is not None) else None,
        "ceiling": content_ceiling_hz(levels),
    }


def audit(paths):
    """对每个 wav / 目录汇总，返回行列表（供打印与测试）。"""
    rows = []
    for p in paths:
        files = []
        if os.path.isdir(p):
            files = [os.path.join(p, f) for f in sorted(os.listdir(p))
                     if f.lower().endswith(".wav")]
        elif os.path.isfile(p):
            files = [p]
        if not files:
            rows.append({"path": p, "error": "没有 wav"})
            continue
        # 目录：抽样最多 8 条，累加功率谱会更稳，这里取均值即可
        acc, srs, n = {}, [], 0
        for f in files[:8]:
            sr, lv, used = band_levels(f)
            if lv is None:
                continue
            srs.append(sr)
            n += 1
            for hi, v in lv.items():
                acc.setdefault(hi, []).append(v)
        if not acc:
            rows.append({"path": p, "error": "全部太短或全静音"})
            continue
        mean_lv = {hi: float(np.mean(v)) for hi, v in acc.items()}
        rows.append({
            "path": p, "files": len(files), "sampled": n,
            "sr": sorted(set(srs))[0] if srs else None,
            "levels": mean_lv,
            "ceiling_hz": content_ceiling_hz(mean_lv),
        })
    return rows


def verdict(row):
    """给出「高频是否真空」的可读结论。

    ★ 用 **12-14kHz 带**判断，而不是 8-10kHz —— 这是标定后的关键选择。

    为什么不用 8-10k（2026-10-13 实测，见 tests/test_band_audit.py）：
    8-10k 带紧邻语音的主要能量区，Hann 窗主瓣会把它**抬到 -36dB**，
    而这个地板与高频里到底有没有内容**无关**：
      · 8k 以上完全清零（理想砖墙）→ 8-10k = -36.0dB，12-14k = -87.5dB
      · 8k 以上压到 -65dB（真实录音）→ 8-10k = -36.0dB，12-14k = -65.4dB
    ⇒ 8-10k 无法区分这两种情形；**12-14k 才能**（泄漏已降到 -87dB）。

    参考点（实测）：
      · 砖墙/真空：12-14k ≈ -87dB（纯窗泄漏）
      · 本 PPT 素材：12-14k ≈ -67dB  ← 高频空但非零（底噪级）
      · 全带宽白噪声：12-14k ≈ 0dB

    判"高频为空"的门限取 **-50dB**：比泄漏地板(-87dB)高 37dB、
    比 PPT 素材(-67dB)高 17dB，又能把"真宽带"(0dB)干净分开。
    """
    if "error" in row:
        return row["error"]
    lv = row["levels"]
    if not lv:
        return "测不到有效语音"
    # 选一个"远离语音主能量、泄漏已足够低"的高频带作为判据
    probe_hi = None
    for hi in (14000, 16000, 12000, 10000):
        if hi in lv:
            probe_hi = hi
            break
    if probe_hi is None:
        top = max(lv)
        return f"⚠ 采样率只到 {top // 1000}kHz，测不到可用高频带（无法判断 40k 适配性）"
    v = lv[probe_hi]
    lo = probe_hi - 2000
    if v > -20.0:
        return f"充足（{lo//1000}-{probe_hi//1000}k {v:+.0f}dB）→ 可喂 40k"
    if v > HF_EMPTY_DB:
        return f"有高频但偏弱（{lo//1000}-{probe_hi//1000}k {v:+.0f}dB）→ 可用"
    return (f"⚠ 高频为空：{lo//1000}-{probe_hi//1000}k 仅 {v:+.0f}dB（≈底噪级；"
            f"纯窗泄漏地板是 -87dB）→ 40k 高频全靠声码器重建，"
            f"加时长只会把该天花板练得更牢")


def main():
    ap = argparse.ArgumentParser(description="人声素材带宽体检（能否喂 40k RVC）")
    ap.add_argument("paths", nargs="+", help="wav 文件或目录")
    args = ap.parse_args()

    rows = audit(args.paths)
    print(f"参考带 {REF_BAND[0]}-{REF_BAND[1]}Hz；有内容门限 {PRESENT_DB:.0f}dB；"
          f"RVC 前端瓶颈 {RVC_FRONTEND_HZ}Hz\n")
    for r in rows:
        print(f"■ {r['path']}")
        if "error" in r:
            print(f"   {r['error']}")
            continue
        lv = r["levels"]
        cells = " ".join(f"{hi // 1000}k:{lv[hi]:+.0f}" for hi in sorted(lv))
        print(f"   sr={r['sr']}  取样 {r['sampled']}/{r['files']}")
        print(f"   {cells}")
        print(f"   → {verdict(r)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
