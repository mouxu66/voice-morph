# -*- coding: utf-8 -*-
"""补录素材到底覆盖到哪个音高 —— 决定「电音能不能靠这次补录修掉」。

这是本次回答的核心数字：RVC 的 `emb_pitch` 条件是**绝对音高档位**，
所以「素材里有没有那个音高」直接决定那个档位是不是被训练过。
"""
import sys

import librosa
import numpy as np
import soundfile as sf

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

F0_MIN, F0_MAX = 50.0, 1100.0
MMIN = 1127 * np.log(1 + F0_MIN / 700)
MMAX = 1127 * np.log(1 + F0_MAX / 700)
SR = 16000


def hz2bin(f):
    return (1127 * np.log(1 + f / 700) - MMIN) * 254 / (MMAX - MMIN) + 1


def bin2hz(b):
    return 700 * (np.exp(((b - 1) * (MMAX - MMIN) / 254 + MMIN) / 1127) - 1)


def f0_of(path, label):
    y, _ = librosa.load(str(path), sr=SR, mono=True)
    f0, _vf, _vp = librosa.pyin(y, fmin=65, fmax=1400, sr=SR, hop_length=160)
    v = np.nan_to_num(np.asarray(f0, dtype=np.float64), nan=0.0)
    v = v[v > 0]
    if len(v) < 50:
        print(f"{label:22s} 有效帧太少（{len(v)}）")
        return None
    print(f"{label:22s} 帧{len(v):>6}  中位{np.median(v):7.1f}  "
          f"p75={np.percentile(v,75):7.1f}  p90={np.percentile(v,90):7.1f}  "
          f"p99={np.percentile(v,99):7.1f}  最高{v.max():7.1f} Hz")
    return v


BASE = "media/raw_recordings/2026-10-supp/_qc_wav"
print("=" * 100)
print("① 新录素材的音高覆盖")
print("=" * 100)
new = {}
for fn, lab in (("词语_44k.wav", "新·词语"), ("句子_44k.wav", "新·句子"),
                ("长音和滑音_44k.wav", "新·长音滑音"), ("唱歌_44k.wav", "新·唱歌")):
    v = f0_of(f"{BASE}/{fn}", lab)
    if v is not None:
        new[lab] = v

print()
print("=" * 100)
print("② 对比：旧的 12 分钟说话素材（就是它练出的现役模型）")
print("=" * 100)
old = f0_of("D:/RVC/logs/myvoice_tilt_t03/2b-f0nsf/ppt12_0001.wav.npy".replace(
    "2b-f0nsf/ppt12_0001.wav.npy", "2a_f0/ppt12_0001.wav.npy"), "（占位，见下方汇总）")

import glob  # noqa: E402

fs = sorted(glob.glob("D:/RVC/logs/myvoice_tilt_t03/2b-f0nsf/*.npy"))
tv = np.concatenate([np.asarray(np.load(f, allow_pickle=True),
                                dtype=np.float64).ravel() for f in fs])
tv = tv[np.isfinite(tv) & (tv > 0)]
print(f"{'旧·12分钟说话素材':22s} 帧{len(tv):>6}  中位{np.median(tv):7.1f}  "
      f"p75={np.percentile(tv,75):7.1f}  p90={np.percentile(tv,90):7.1f}  "
      f"p99={np.percentile(tv,99):7.1f}  最高{tv.max():7.1f} Hz")

print()
print("=" * 100)
print("③ ★ 关键：按档位算「覆盖帧数」—— 新素材把覆盖上限推到了哪")
print("=" * 100)
for lab, v in (("旧(12min说话)", tv), *new.items()):
    b = np.clip(np.rint(hz2bin(v)).astype(int), 1, 255)
    h = np.bincount(b, minlength=256)
    c10 = int(np.nonzero(h >= 10)[0].max())
    c50 = int(np.nonzero(h >= 50)[0].max())
    print(f"  {lab:16s} ≥10帧到档位{c10:>4} ({bin2hz(c10):>7.1f}Hz)"
          f"   ≥50帧到档位{c50:>4} ({bin2hz(c50):>7.1f}Hz)")

print()
print("=" * 100)
print("④ 新的唱歌素材 + 旧说话混在一起，能覆盖《唯一》的哪个档位")
print("=" * 100)
mixed = np.concatenate([tv] + [new[k] for k in new if "唱" in k or "长音" in k])
b = np.clip(np.rint(hz2bin(mixed)).astype(int), 1, 255)
h = np.bincount(b, minlength=256)
c10 = int(np.nonzero(h >= 10)[0].max())
print(f"  混训后 ≥10帧覆盖到档位 {c10} = {bin2hz(c10):.1f} Hz")

sv = None
y, _ = librosa.load("media/covers/song2/01_vocals.wav", sr=SR, mono=True)
f0, _a, _b = librosa.pyin(y, fmin=65, fmax=1400, sr=SR, hop_length=160)
sv = np.nan_to_num(np.asarray(f0, dtype=np.float64), nan=0.0)
sv = sv[sv > 0]
print(f"\n  {'出成品档位':<12}{'请求F0中位':>12}{'请求F0 p90':>12}{'混训后覆盖率':>14}")
for p in (0, -5, -12):
    req = sv * 2 ** (p / 12)
    rb = np.clip(np.rint(hz2bin(req)).astype(int), 1, 255)
    cov = float((h[rb] >= 10).mean())
    print(f"  {p:+d} 半音{'':<5}{np.median(req):>10.1f}Hz{np.percentile(req,90):>10.1f}Hz"
          f"{cov*100:>13.1f}%")

print("\n参考：C4=261.6  E4=329.6  G4=392.0  A4=440.0  C5=523.3 Hz")
