# -*- coding: utf-8 -*-
"""训练集「见过」哪些音高档位 vs《唯一》要求哪些 —— 直接回答"补录能不能修电音"。"""
import glob
import sys

import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

F0_MIN, F0_MAX = 50.0, 1100.0
MMIN = 1127 * np.log(1 + F0_MIN / 700)
MMAX = 1127 * np.log(1 + F0_MAX / 700)


def hz2bin(f):
    return (1127 * np.log(1 + f / 700) - MMIN) * 254 / (MMAX - MMIN) + 1


def bin2hz(b):
    return 700 * (np.exp(((b - 1) * (MMAX - MMIN) / 254 + MMIN) / 1127) - 1)


def train_f0(exp):
    fs = sorted(glob.glob(f"D:/RVC/logs/{exp}/2b-f0nsf/*.npy"))
    v = np.concatenate([np.asarray(np.load(f, allow_pickle=True),
                                   dtype=np.float64).ravel() for f in fs])
    return v[np.isfinite(v)]


print("=" * 86)
print("① 模型实际「练过」的音高档位（emb_pitch 的 256 个槽位）")
print("=" * 86)
for exp in ("myvoice_tilt_t03", "kangaroo_v2"):
    v = train_f0(exp)
    # f0_min 是 50Hz，低于它的帧会落到档位 0 或负数（本机实测 min=41.8Hz）
    # —— bincount 不接受负下标，先夹到合法范围
    b = np.clip(np.rint(hz2bin(v)).astype(int), 1, 255)
    print(f"\n{exp}：{len(v):,} 个浊音帧")
    for thr in (1, 10, 50, 100, 500):
        used = np.nonzero(np.bincount(b, minlength=256) >= thr)[0]
        print(f"  至少 {thr:>3} 帧的档位：{used.min():>3} .. {used.max():>3}"
              f"   = {bin2hz(used.min()):6.1f} .. {bin2hz(used.max()):6.1f} Hz")
    h = np.bincount(b, minlength=256)
    print(f"  —— 高于 250 Hz（档位 {hz2bin(250):.0f}）的帧：{int(h[int(hz2bin(250)):].sum())} 帧")
    print(f"  —— 高于 300 Hz（档位 {hz2bin(300):.0f}）的帧：{int(h[int(hz2bin(300)):].sum())} 帧")

print()
print("=" * 86)
print("② 《唯一》要求哪些档位（中位/分位）")
print("=" * 86)
for lab, f in (("中位 275.5 Hz", 275.5), ("p90 415.1 Hz", 415.1),
               ("最高 1187.8 Hz", 1187.8), ("+12 半音后的中位 551 Hz", 551.0)):
    print(f"  {lab:28s} → 档位 {hz2bin(f):6.1f}"
          f"{'   ★ 超出 256 上限，被 pipeline.py:137 夹到 255' if hz2bin(f) > 255 else ''}")

print()
print("=" * 86)
print("③ 微调到底动了多少？（emb_pitch 每档范数：预训练底模 vs 你的模型）")
print("=" * 86)
import torch  # noqa: E402


def emb(p):
    ck = torch.load(p, map_location="cpu", weights_only=True)
    w = ck.get("weight") or ck.get("model") or ck
    return w["enc_p.emb_pitch.weight"].float().numpy()


base = emb("D:/RVC/assets/pretrained_v2/f0G40k.pth")
mine = emb("D:/RVC/assets/weights/myvoice_tilt_t03.pth")
d = np.abs(mine - base).mean(axis=1)
print(f"  逐档平均绝对改动：全体 max={d.max():.5f}  p50={np.median(d):.5f}")
lo = int(hz2bin(80)), int(hz2bin(200))     # 训练覆盖区
hi = int(hz2bin(250)), int(hz2bin(450))    # 歌要求、训练基本没覆盖
print(f"  训练覆盖区（{bin2hz(lo[0]):.0f}-{bin2hz(lo[1]):.0f}Hz，档位 {lo[0]}-{lo[1]}）："
      f"改动中位 {np.median(d[lo[0]:lo[1]+1]):.5f}")
print(f"  歌的高音区（{bin2hz(hi[0]):.0f}-{bin2hz(hi[1]):.0f}Hz，档位 {hi[0]}-{hi[1]}）："
      f"改动中位 {np.median(d[hi[0]:hi[1]+1]):.5f}")
print("  ⇒ 没数据的地方，微调几乎没动它（那些槽位仍是底模从别人歌声里学来的）")
