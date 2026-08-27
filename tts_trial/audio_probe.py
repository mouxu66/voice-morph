# -*- coding: utf-8 -*-
"""音频链路实测：播 1 秒测试音，同时读扬声器端点的峰值电平表。
peak > 0.01 => 扬声器有实际输出（链路通）。结果写 probe_out.txt。"""
import math
import os
import struct
import subprocess
import threading
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "probe_out.txt")
TEST_WAV = os.path.join(HERE, "test_tone.wav")


def log(m):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(m + "\n")


open(OUT, "w").close()

# 生成测试音
sr = 44100
frames = b"".join(
    struct.pack("<hh", int(20000 * math.sin(2 * math.pi * 440 * i / sr)),
                int(20000 * math.sin(2 * math.pi * 440 * i / sr)))
    for i in range(sr))
with wave.open(TEST_WAV, "wb") as w:
    w.setnchannels(2)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(frames)

try:
    from ctypes import POINTER, c_float
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

    def measure():
        try:
            import comtypes
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            dev = AudioUtilities.GetSpeakers()
            iface = dev._dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
            meter = iface.QueryInterface(IAudioMeterInformation)
            peaks = []
            t0 = time.time()
            while time.time() - t0 < 3.0:
                peaks.append(float(meter.GetPeakValue()))
                time.sleep(0.05)
            return peaks
        except Exception:
            import traceback
            log("[thread-fail] " + traceback.format_exc())
            return [0.0]

    # 先起播放（非阻塞），后在新线程里测峰值（MTA 套间）
    player = subprocess.Popen(
        ["powershell", "-NoProfile", "-Command",
         f"(New-Object Media.SoundPlayer '{TEST_WAV}').PlaySync()"])
    th = threading.Thread(target=lambda: _RESULT.update(peaks=measure()), daemon=True)
    class _R(dict):
        pass
    _RESULT = _R()
    th.start()
    th.join(10)
    peaks = _RESULT.get("peaks", [0.0])

    log(f"[meter] peak_max={max(peaks):.4f} samples={len(peaks)}")
    log("[verdict] " + ("链路通：扬声器有实际输出" if max(peaks) > 0.01 else "无输出：引擎/路由仍卡死"))
except Exception as e:
    import traceback
    log("[FAIL] " + repr(e))
    log(traceback.format_exc())
log("[end]")
print("done")
