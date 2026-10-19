# -*- coding: utf-8 -*-
"""补录要唱到多高才够 —— 把「歌要求的档位」与「模型练过的档位」对上。

《唯一》要在 pitch 0 出成品，训练的**素材本身**必须含有那么高的 F0：
    `emb_pitch` 的条件是绝对音高档位（`pipeline.py` 用 `pitchf` = 源 F0 × 2^(pitch/12)），
    变调只改**请求哪个档位**，不会让没练过的档位变得练过。
所以本脚本回答：按不同档位出成品时，训练素材各自需要覆盖到多少 Hz。
"""
import glob
import sys

import librosa
import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

F0_MIN, F0_MAX = 50.0, 1100.0
MMIN = 1127 * np.log(1 + F0_MIN / 700)
MMAX = 1127 * np.log(1 + F0_MAX / 700)


def hz2bin(f):
    return (1127 * np.log(1 + f / 700) - MMIN) * 254 / (MMAX - MMIN) + 1


def bin2hz(b):
    return 700 * (np.exp(((b - 1) * (MMAX - MMIN) / 254 + MMIN) / 1127) - 1)


# ---- 训练素材实际覆盖到哪 ----
fs = sorted(glob.glob("D:/RVC/logs/myvoice_tilt_t03/2b-f0nsf/*.npy"))
tv = np.concatenate([np.asarray(np.load(f, allow_pickle=True),
                                dtype=np.float64).ravel() for f in fs])
tv = tv[np.isfinite(tv)]
tb = np.clip(np.rint(hz2bin(tv)).astype(int), 1, 255)
hist = np.bincount(tb, minlength=256)
ceil10 = int(np.nonzero(hist >= 10)[0].max())      # ≥10 帧的最高档位
ceil50 = int(np.nonzero(hist >= 50)[0].max())
print(f"现有训练集（12 分钟说话）：{len(tv):,} 帧")
print(f"  ≥10 帧的档位到 {ceil10} = {bin2hz(ceil10):.0f} Hz")
print(f"  ≥50 帧的档位到 {ceil50} = {bin2hz(ceil50):.0f} Hz")

# ---- 歌要求哪些 F0 ----
y, sr = librosa.load("media/covers/song2/01_vocals.wav", sr=16000, mono=True)
f0, _vf, _vp = librosa.pyin(y, fmin=65, fmax=1400, sr=sr, hop_length=160)
f0 = np.nan_to_num(np.asarray(f0, dtype=np.float64), nan=0.0)
sv = f0[f0 > 0]
print(f"\n《唯一》人声：{len(sv):,} 个浊音帧")
for q in (10, 25, 50, 75, 90, 99, 100):
    print(f"  p{q:<3d} = {np.percentile(sv, q):7.1f} Hz")

print("\n" + "=" * 78)
print("按不同档位出成品时：训练素材需要覆盖到多高")
print("=" * 78)
print(f"{'出成品档位':<14}{'请求的 F0 中位':>16}{'请求的 F0 p90':>16}"
      f"{'已覆盖帧占比':>14}")
for p in (0, -5, -12, -16):
    req = sv * 2 ** (p / 12)
    rb = np.clip(np.rint(hz2bin(req)).astype(int), 1, 255)
    # 「已覆盖」= 该档位在现有训练集里至少有 10 帧
    covered = float((hist[rb] >= 10).mean())
    print(f"{p:+d} 半音{'':<7}{np.median(req):>14.1f}Hz{np.percentile(req,90):>14.1f}Hz"
          f"{covered*100:>13.1f}%")

print("\n" + "=" * 78)
print("★ 补录唱歌要唱到多高（F0 上限），才能让某个档位「整首」进覆盖区")
print("=" * 78)
for p in (0, -5, -12):
    req = sv * 2 ** (p / 12)
    need = float(np.percentile(req, 90))     # 覆盖到 p90 就算"够用"
    print(f"  pitch {p:+3d}：素材需含 F0 到约 {need:6.1f} Hz"
          f"（档位 {hz2bin(need):5.1f}）"
          f"{'  ← 靠真声很难，需要头声/假声' if need > 330 else ''}")

print("\n对照：C4=261.6Hz  E4=329.6Hz  G4=392.0Hz  A4=440Hz  C5=523.3Hz")
