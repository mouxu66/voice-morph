"""WASAPI loopback 系统内录：直接抓默认扬声器正在播出的声音，不借助麦克风。

用途：刷抖音/看视频时点桌宠「录制当前声音」，把系统播出的音频存为素材，
自动走现有流水线（demucs 去 BGM → 静音切片）与音色挖掘。
内录抓的是声卡数字信号，质量与原音源一致（无房间混响/噪声损失）。
"""
import wave
from pathlib import Path

import pyaudiowpatch as pyaudio


class LoopbackError(Exception):
    """内录失败（无可用输出设备 / WASAPI 异常等）"""


def default_loopback_device(p: "pyaudio.Pyaudio") -> dict:
    """找到默认输出设备对应的 loopback（内录）设备。"""
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError as e:
        raise LoopbackError(f"WASAPI 不可用: {e}") from e
    default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
    if default_out.get("isLoopbackDevice"):
        return default_out
    for lb in p.get_loopback_device_info_generator():
        if default_out["name"] in lb["name"]:
            return lb
    raise LoopbackError("找不到默认扬声器的 loopback 内录设备")


def record_loopback(seconds: float, out_path: Path) -> Path:
    """录默认扬声器输出 seconds 秒，16bit wav 落盘（保留原始采样率与声道）。"""
    seconds = max(1.0, min(float(seconds), 180.0))
    p = pyaudio.PyAudio()
    try:
        dev = default_loopback_device(p)
        rate = int(dev["defaultSampleRate"])
        channels = max(1, min(2, int(dev["maxInputChannels"]) or 2))
        chunk = 512
        frames: list[bytes] = []
        with p.open(
            format=pyaudio.paInt16, channels=channels, rate=rate,
            frames_per_buffer=chunk, input=True, input_device_index=dev["index"],
        ) as stream:
            total = int(rate / chunk * seconds)
            for _ in range(total):
                frames.append(stream.read(chunk, exception_on_overflow=False))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(b"".join(frames))
        return out_path
    finally:
        p.terminate()
