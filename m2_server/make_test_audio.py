"""生成合成测试音频（无需真实素材也能验证链路）

生成两类音频：
    1. 目标音色参考（模拟"美团老鼠"音色）→ media/voicebank/test_voice/reference.wav
       注意：合成音色无真实辨识度，仅用于验证 模型加载→提取SE→转换 链路是否通畅。
       真实效果请用 pipeline.py + 真实素材。
    2. 内容语音（模拟你的录音）→ outputs/test_input.wav

用法：
    python m2_server/make_test_audio.py
"""
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
SR = 22050


def synth_tone(freqs, dur, sr=SR, amp=0.5):
    """用多个正弦波叠加模拟一段"人声"（无辨识度，仅验证链路）"""
    t = np.linspace(0, dur, int(sr * dur), endpoint=False)
    y = np.zeros_like(t)
    for f in freqs:
        y += amp / len(freqs) * np.sin(2 * np.pi * f * t)
    # 包络：淡入淡出，模拟音节感
    env = np.minimum(t / 0.05, 1.0) * np.minimum((dur - t) / 0.05, 1.0)
    return (y * env * 0.9).astype(np.float32)


def write_wav(path: Path, audio: np.ndarray):
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def main():
    # 1. 目标音色：一段"不同音高"的连续音节（模拟目标音色的参考素材）
    target_freqs = [180, 220, 260, 300, 340, 300, 260, 220]  # 低沉、有辨识倾向
    ref = []
    for f in target_freqs:
        ref.append(synth_tone([f, f * 1.5, f * 2.0], 0.4))  # 每个音节 0.4s
        ref.append(np.zeros(int(SR * 0.1), dtype=np.float32))  # 间隔 0.1s
    ref_audio = np.concatenate(ref)

    bank = ROOT / "media" / "voicebank" / "test_voice"
    bank.mkdir(parents=True, exist_ok=True)
    ref_path = bank / "reference.wav"
    write_wav(ref_path, ref_audio)
    print(f"[测试] 目标音色参考 -> {ref_path}  ({len(ref_audio)/SR:.1f}s)")

    # 2. 内容语音：较高音高，模拟"你的录音"
    content_freqs = [440, 494, 523, 587, 659, 587, 523]
    content = []
    for f in content_freqs:
        content.append(synth_tone([f, f * 1.25, f * 1.5], 0.3))
        content.append(np.zeros(int(SR * 0.08), dtype=np.float32))
    content_audio = np.concatenate(content)

    OUT = ROOT / "outputs"
    OUT.mkdir(exist_ok=True)
    in_path = OUT / "test_input.wav"
    write_wav(in_path, content_audio)
    print(f"[测试] 内容语音 -> {in_path}  ({len(content_audio)/SR:.1f}s)")

    print("\n现在可运行:")
    print("  python m2_server/demo_convert.py test_voice outputs/test_input.wav")
    print("  然后试听 outputs/result.wav（合成音色，仅供链路验证，无真实辨识度）")


if __name__ == "__main__":
    main()
