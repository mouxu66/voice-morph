# -*- coding: utf-8 -*-
"""只读核实：RVC 的"高频空"到底卡在哪一环 —— 是用户录不到，还是我们前端砍的？

用户的两个质疑（都成立，先记下来）：
  ① 「你说得太复杂了」—— 我上一轮提了三步闸门 + 分级阈值，属于过度设计。
  ② 「用户可能无法录制高频音色，对用户体验也不好」—— **这条更关键**：
     如果把"录到 8k 以上"变成用户义务，等于把工程问题推给用户。

所以在设计任何"拦用户"的东西之前，先回答一个更基础的问题：
**高频缺失，是用户设备录不到（不可控），还是我们自己的链路砍掉了（可控）？**

本脚本只做静态核查 + 已有产物复算，不训练、不推理：
  1. 读 RVC 前端采样率常量（代码事实）；
  2. 读训练集与源视频的真实带宽（已有产物）；
  3. 按 16k / 22.05k / 32k / 40k / 48k 底模反推"各自能保住多少带宽"。
"""
import os
import sys

import numpy as np
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8")

PPT = r"D:\变声\media\ft_work\ppt12"


def band_db(path, bands, ref=(400, 800), nfft=4096, active_pct=60.0):
    """逐频带相对 dB（与 tts_trial/band_audit.py 同口径）。"""
    a, sr = sf.read(str(path), dtype="float32", always_2d=True)
    a = a.mean(axis=1)
    if a.size < nfft * 3:
        return sr, None
    hop = nfft // 4
    win = np.hanning(nfft).astype(np.float32)
    frames = [a[i:i + nfft] * win for i in range(0, a.size - nfft, hop)]
    if not frames:
        return sr, None
    X = np.fft.rfft(np.stack(frames), axis=1)
    P = (np.abs(X) ** 2).mean(axis=0)
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
    # 只统计活跃帧（≥60 分位 RMS）
    rms = np.sqrt((np.stack(frames) ** 2).mean(axis=1))
    keep = rms >= np.percentile(rms, active_pct)
    Xk = np.fft.rfft(np.stack(frames)[keep], axis=1)
    P = (np.abs(Xk) ** 2).mean(axis=0)

    def lv(lo, hi):
        m = (freqs >= lo) & (freqs < hi)
        return float(P[m].sum()) if m.any() else 0.0

    base = lv(*ref) + 1e-30
    return sr, {f"{lo}-{hi}": 10 * np.log10((lv(lo, hi) + 1e-30) / base) for lo, hi in bands}


def main():
    print("=" * 82)
    print("① 代码事实：RVC 前端把输入降到多少")
    print("=" * 82)
    src = open(r"D:\变声\m2_server\offline_vc_infer.py", encoding="utf-8").read()
    for i, l in enumerate(src.splitlines(), 1):
        if "16000" in l or "load_audio" in l or "change_rms" in l:
            print(f"  offline_vc_infer.py:{i}: {l.strip()[:100]}")
    print()
    print("  ⇒ 前端统一 load_audio(src, 16000) ⇒ **特征侧信息止于 8kHz**")
    print("     （与用户录什么无关 —— 是这一行决定的）")

    print()
    print("=" * 82)
    print("② 真实素材的带宽（已有产物，复算）")
    print("=" * 82)
    bands = [(6400, 8000), (8000, 10000), (10000, 12000), (12000, 14000), (14000, 16000)]
    for name, path in [
        ("源视频 24k 抽音", os.path.join(PPT, "source_24k.wav")),
        ("训练集切片 24k", os.path.join(PPT, "clips24k", sorted(os.listdir(os.path.join(PPT, "clips24k")))[0])
         if os.path.isdir(os.path.join(PPT, "clips24k")) else ""),
        ("参考音 ref_24k", os.path.join(PPT, "ref_24k.wav")),
    ]:
        if not path or not os.path.exists(path):
            print(f"  [skip] {name}")
            continue
        sr, res = band_db(path, bands)
        if res is None:
            print(f"  [skip] {name}（太短）")
            continue
        print(f"\n  {name}  (sr={sr})")
        for k, v in res.items():
            flag = ""
            if k == "12000-14000":
                flag = "  ← 判据带（干净）" + ("  ⚠ 高频为空" if v < -50 else "  ✅ 有内容")
            print(f"    {k:>12} Hz : {v:>8.1f} dB{flag}")

    print()
    print("=" * 82)
    print("③ 底模采样率 → 能保住的带宽上限")
    print("=" * 82)
    print(f"  {'底模':<10}{'Nyquist':>10}{'实际可用':>12}   说明")
    for sr, note in [(16000, "RVC 前端特征（所有底模共用）"),
                     (32000, "32k 底模输出"),
                     (40000, "40k 底模输出 ← 本项目现用"),
                     (48000, "48k 底模输出")]:
        print(f"  {sr/1000:<8.0f}k{sr/2:>9.0f}Hz{sr/2:>10.0f}Hz   {note}")

    print()
    print("=" * 82)
    print("结论")
    print("=" * 82)
    print("""
· **前端 16kHz 是硬编码，与底模无关**：不管用 32k/40k/48k 底模，
  送进 HuBERT 的特征永远止于 8kHz。⇒ **换更高采样率的底模不会突破这个瓶颈**。

· 所以"高频重建得像不像"取决于**训练集里 8k 以上有什么**。
  而这恰恰是**用户设备决定的**，不是我们能替他解决的：
  手机麦克风、蓝牙耳机、会议软件降噪，通常都限带。

⇒ 用户质疑②成立：**不能把"录到高频"变成用户义务**。
   正确方向不是"教用户录高频"，而是：
     (a) 用用户给的素材，把**能保住的带宽**做到最好（别自己再砍一刀）；
     (b) 若素材本就限带，就**别用 40k 底模**（用 32k 更匹配，见 (a) 的推论）；
     (c) 若真要提音色，靠**其它维度**（参考音质量、TTS 源码、index_rate），
         而不是逼用户换设备。
""")


if __name__ == "__main__":
    main()
