"""8–12k 那一层是「跟着嗓音走」还是「一层不动的嘶」—— 判 Seed-VC 全带宽参照能不能直接产品化。

为什么需要这条：

m1u 实测：Seed-VC 只换参照音，8–12k 从 −93.1dB 跳到 −60.5dB（+32.6dB）。但"高频回来了"
和"高频糊了一层嘶"在频带电平表上长得一模一样，后续动作却相反：

  · 跟着嗓音走 → 只需重录 60–90s 全带宽参照，不用重训；
  · 不跟着走   → 换架构只是把"纯音电音"换成"嘶声"，仍要回 P0 重录训练集。

★ 先说一条走过的弯路（保留证据，免得下次再踩）：

第一版做的是"谐波性"——按 f0 铺梳状窗，算 excess HNR（谐波/噪声比减白噪声期望）。
在合成稳态谐波上完全正常，换到真人就全线归零：

    纯谐波 320Hz       8–12k excess HNR = +28.3dB
    同一条 + 3% 颤音   = +0.4dB
    源人声（赵雷）      = −0.1dB

原因不是数据，是物理：8–12k 处第 30 根谐波，f0 抖 3% 就是 290Hz 位移，而 93ms 窗
只有 10.8Hz 分辨率——颤音必然把谐波抹成一片。真人唱歌全有颤音，所以那把尺子测的是
"颤得厉害不厉害"，不是"有没有嘶声"。**凡是靠 f0 铺梳状窗的高频谐波度量，在歌唱上都不能用。**

改用不依赖 f0 的三条：

1. `r_env` —— 8–12k 能量与 200–3000 能量（对数域）的相关。嗓音驱动的高频会跟着走。
2. `slope` —— 前者对后者的回归斜率。真嗓音 ≈1，静态嘶声 ≈0。
3. `gap_drop` —— 最安静的 10% 帧相对有声帧，高频掉多少 dB。嘶声不跟着掉，就接近 0。

这三条有已知答案的对照臂可以自检：D1（UniverSR）是已经确认"气口被塞纯音"的坏样本，
它必须被判坏；A 臂（不开 BWE）是"不电"的对照。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments"))

import cover_clip_attrib as M  # noqa: E402

OUT = ROOT / "media" / "covers" / "midsong_20261018" / "seedvc_probe"
FULL = ROOT / "media" / "covers" / "midsong_20261018" / "chengdu_bwe_full"
#: 和 m1u 探针同一段：用户判"最电 + 最夹嗓子"的 C 段高音区
SEG = (285.0, 20.0)
WIN_S, HOP_S = 0.064, 0.010
LOW = (200.0, 3000.0)
HF = (8000.0, 12000.0)
#: 响度对齐目标：有声帧 200–3000 的中位电平。不对齐的话跨文件比绝对电平没意义
ALIGN_DB = -20.0


def frame_db(y: np.ndarray, sr: int) -> dict[str, np.ndarray]:
    """逐帧算三条频带的绝对电平（dB，未对齐）。"""
    n, h = int(WIN_S * sr), int(HOP_S * sr)
    win = np.hanning(n)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    masks = {k: (freqs >= a) & (freqs < b) for k, (a, b) in {"low": LOW, "hf": HF}.items()}
    rows = {k: [] for k in masks}
    for s in range(0, len(y) - n, h):
        seg = np.asarray(y[s : s + n], dtype=np.float64)
        p = np.abs(np.fft.rfft((seg - seg.mean()) * win)) ** 2
        for k, m in masks.items():
            rows[k].append(10 * np.log10(max(float(p[m].sum()), 1e-20)))
    return {k: np.asarray(v) for k, v in rows.items()}


def tracking(y: np.ndarray, sr: int) -> dict:
    """高频是否跟随嗓音。全部在响度对齐之后算，跨文件可比。"""
    e = frame_db(y, sr)
    lo, hf = e["low"], e["hf"]
    if lo.size < 20:
        raise SystemExit(f"帧数太少（{lo.size}），无法判定")
    # 有声帧 = 低频能量前 50%：和 m1u 探针同口径（绝对阈值一换素材就会把"在唱"判成静音）
    active = lo >= np.percentile(lo, 50)
    gain = ALIGN_DB - float(np.median(lo[active]))
    lo, hf = lo + gain, hf + gain

    a_lo, a_hf = lo[active], hf[active]
    if a_lo.std() < 1e-6:
        raise SystemExit("低频电平几乎不动，判不出跟随关系")
    if a_hf.std() < 1e-9:
        # 高频电平纹丝不动 —— 这正是"一层静态嘶声"的样子，必须照实报，不能崩
        r_env, slope = 0.0, 0.0
    else:
        r_env = float(np.corrcoef(a_lo, a_hf)[0, 1])
        slope = float(np.polyfit(a_lo, a_hf, 1)[0])
    # 最安静的 10% 当"气口代理"。这段切片不一定有真静音，用分位数才总有定义
    quiet = lo <= np.percentile(lo, 10)
    return {
        "frames": int(lo.size),
        "hf_active_db": round(float(np.median(a_hf)), 1),
        "r_env": round(r_env, 3),
        "slope": round(slope, 2),
        "gap_drop_db": round(float(np.median(hf[quiet]) - np.median(a_hf)), 1),
        "hf_spread_db": round(float(np.percentile(hf, 90) - np.percentile(hf, 10)), 1),
    }


def verdict(r: dict, src_hf: float) -> str:
    """先看电平再看待形。

    只看"跟不跟随"会被骗：一段静态噪声底如果整体被 EQ 抬起来，它的气口也不掉，
    但它本来就没有嗓音信息。所以先用**和源人声的电平差**判"有没有、过不过量"，
    再判"是不是跟着嗓音动"。
    """
    d = r["hf_active_db"] - src_hf
    if d < -30:
        return "几乎没有高频"
    if d > 8:
        return "偏亮偏硬（过量的那一侧）"
    if r["slope"] < 0.35 or r["gap_drop_db"] > -8:
        return "噪声底，不跟嗓音"
    if r["r_env"] < 0.5:
        return "弱跟随"
    return "跟随嗓音（可用）"


def slice_of(y: np.ndarray, sr: int) -> np.ndarray:
    a = int(SEG[0] * sr)
    b = a + int(SEG[1] * sr)
    if b > len(y):
        raise SystemExit(f"切片越界：文件只有 {len(y) / sr:.1f}s")
    return y[a:b]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=str(ROOT / "media/voicebank/myvoice_supp2026/reference.wav"))
    args = ap.parse_args()

    files = [
        ("源人声(赵雷)", OUT / "c_tail_source.wav"),
        ("参照音(你的22k)", Path(args.ref)),
        ("RVC 干声", OUT / "c_tail_rvc_dry.wav"),
        ("Seed-VC+你的参照", OUT / "c_tail_seedvc.wav"),
        ("Seed-VC+全带宽参照", OUT / "c_tail_seedvc_fullband.wav"),
        ("A 臂 无BWE(已知不电)", FULL / "A_full_post.wav"),
        ("D1 臂 UniverSR(已知气口纯音)", FULL / "D1_bwe_post.wav"),
    ]
    rows = []
    print(
        f"\n=== 8–12k 是否跟随嗓音（{SEG[0]:.0f}–{SEG[0] + SEG[1]:.0f}s，响度对齐到 {ALIGN_DB:g}dB）==="
    )
    src_hf = None
    print(
        f"{'':<28}{'HF电平':>9}{'差源':>7}{'r_env':>8}{'slope':>7}{'气口掉':>8}{'HF摆动':>8}  判读"
    )
    for name, p in files:
        if not p.exists():
            print(f"{name:<28}{'缺文件':>9}")
            continue
        y, sr = M.read(p)
        r = tracking(slice_of(y, sr), sr) if "臂" in name else tracking(y, sr)
        r["name"] = name
        if src_hf is None:
            src_hf = r["hf_active_db"]
        r["delta_vs_src_db"] = round(r["hf_active_db"] - src_hf, 1)
        rows.append(r)
        print(
            f"{name:<28}{r['hf_active_db']:>9.1f}{r['delta_vs_src_db']:>+7.1f}"
            f"{r['r_env']:>8.2f}{r['slope']:>7.2f}"
            f"{r['gap_drop_db']:>8.1f}{r['hf_spread_db']:>8.1f}  {verdict(r, src_hf)}"
        )

    with open(OUT / "hf_tracking.json", "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(
        "\n自检：D1（已确认气口塞纯音）必须落到「偏亮偏硬」，A 臂（treble+6dB 抬噪声底）"
        "必须落到「噪声底」。两个都对，才看 Seed-VC 那两行。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
