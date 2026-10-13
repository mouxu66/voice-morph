# -*- coding: utf-8 -*-
"""验证「自然度尺子」（NatScore）在**本批素材**上还有没有分辨力 —— 先验尺子，再下结论。

为什么需要这个探针（2026-10-14）
--------------------------------
`docs/大创申报-2026-10/千问微调vsRVC-裁决/scores.json` 里出现了一个反常识信号：

    真人参考音 ref_24k.wav        nats = -2.303
    真人原音 → RVC（音色上限）     nats = -3.033 / -3.218
    Qwen 零样本 / Qwen 微调 / RVC  nats = +0.63 ~ +4.10

**尺子把真人排在所有合成之下**（差 3~7 分）。这有两种可能：
  ① 素材真的怪（ref 是从 PPT 视频音轨抽的，AAC 有损 + 可能去过噪）；
  ② 尺子在这类素材上失效（域外）。
不区分这两者就用 nats 报「谁更自然」＝拿一把没验过的尺子下判决。

做法：拿同一段真人录音做**递进劣化**（一个只该变差的变化序列），
看它是否单调。单调 ⇒ 尺子在本素材上有序；乱序 ⇒ 尺子失效（如实记「未测」）。

用法（项目 .venv）：
    .venv\\Scripts\\python.exe tts_trial\\probe_natscore_domain.py
"""

import os
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.stdout.reconfigure(encoding="utf-8")


def rng_noise(wav: np.ndarray, snr_db: float, seed: int = 20261014) -> np.ndarray:
    """按 SNR 加白噪声（对语音活动段算功率，全零段不参与）。"""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 1.0, size=wav.shape).astype(np.float32)
    p_sig = float(np.mean(wav**2))
    p_noise = float(np.mean(noise**2))
    if p_sig <= 0:
        return wav
    g = np.sqrt(p_sig / (p_noise * (10 ** (snr_db / 10.0))))
    out = wav + g * noise
    m = float(np.max(np.abs(out))) or 1.0
    return (out / m * float(np.max(np.abs(wav)))).astype(np.float32)


def lowpass_brickwall(wav: np.ndarray, sr: int, cutoff: float) -> np.ndarray:
    """理想砖墙低通（FFT 置零）—— 只会去掉内容，不会加上新东西。"""
    spec = np.fft.rfft(wav)
    freqs = np.fft.rfftfreq(len(wav), 1.0 / sr)
    spec[freqs > cutoff] = 0
    return np.fft.irfft(spec, n=len(wav)).astype(np.float32)


def is_monotone_degradation(scores: list[float]) -> bool:
    """判「越劣化、分越低」。scores = [原样, +25dB, +15dB, +5dB, …, 正弦]。

    严格要求单调（不加容差）—— 加容差就成了"往想要的结论上调"。
    实测两段素材一段过一段不过，**不过的那段就是结论**：尺子在小幅劣化上没有分辨力。
    """
    clean, n25, n15, n5 = scores[0], scores[1], scores[2], scores[3]
    return clean >= n25 >= n15 >= n5 and clean > scores[-1]


def main() -> int:
    from natscore_local import load_local

    ckpt = ROOT / "models" / "natscore" / "final.pt"
    if not ckpt.exists():
        print(f"[FATAL] NatScore 权重缺失：{ckpt}")
        return 1
    scorer = load_local(str(ckpt))

    cases = {
        "ppt12/ref_24k.wav（真人参考音）": ROOT / "media/ft_work/ppt12/ref_24k.wav",
        "voicebank/kangaroo/reference.wav（真人自录）": ROOT
        / "media/voicebank/kangaroo/reference.wav",
    }

    print(f"{'素材 / 变体':<52}{'nats':>9}")
    print("-" * 62)
    report = {}
    for label, path in cases.items():
        if not path.exists():
            print(f"{label:<52}{'(缺文件)':>9}")
            continue
        wav, sr = sf.read(str(path), dtype="float32")
        if wav.ndim > 1:
            wav = wav.mean(axis=1)
        # 统一裁到 ≤6s：whisper-small 编码器吃整段，长音频只增加耗时
        wav = wav[: sr * 6]
        peak = float(np.max(np.abs(wav))) or 1.0
        wav = wav / peak * 0.9

        variants = {
            "① 原样（应最高）": wav,
            "② +25dB 白噪声": rng_noise(wav, 25),
            "③ +15dB 白噪声": rng_noise(wav, 15),
            "④ +5dB 白噪声": rng_noise(wav, 5),
            f"⑤ 砖墙低通 4kHz（丢高频）": lowpass_brickwall(wav, sr, 4000),
            "⑥ 纯 220Hz 正弦（肯定不自然）": (
                0.9 * np.sin(2 * np.pi * 220 * np.arange(len(wav)) / sr)
            ).astype(np.float32),
        }

        tmp = ROOT / "outputs" / "_natscore_probe"
        tmp.mkdir(parents=True, exist_ok=True)
        print(f"\n{label}")
        seq = []
        for name, sig in variants.items():
            p = tmp / f"{abs(hash(name)) % 10**6:06d}.wav"
            sf.write(str(p), sig, sr)
            s = float(scorer.score(str(p)))
            seq.append(s)
            report[f"{label} | {name}"] = round(s, 3)
            print(f"  {name:<50}{s:>9.3f}")

        mono = is_monotone_degradation(seq)
        print(
            f"  ⇒ 单调性（原样 ≥ +25 ≥ +15 ≥ +5dB，且原样 > 正弦）："
            f"{'✅ 成立' if mono else '❌ 不成立 —— 尺子在本素材上失效'}"
        )
    print()
    print(f"明细已写：outputs/_natscore_probe/（{len(report)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
