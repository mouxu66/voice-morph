# -*- coding: utf-8 -*-
"""检查默认扬声器的总音量/静音、各应用会话音量。写 vol_out.txt。"""
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vol_out.txt")


def log(m):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(m + "\n")


open(OUT, "w").close()
try:
    from comtypes import CLSCTX_ALL, CoInitialize
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    CoInitialize()
    dev = AudioUtilities.GetSpeakers()
    iface = dev._dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    vol = iface.QueryInterface(IAudioEndpointVolume)
    log(f"[master] volume={round(vol.GetMasterVolumeLevelScalar() * 100)}% mute={bool(vol.GetMute())}")
    log("[sessions]")
    for s in AudioUtilities.GetAllSessions():
        nm = s.ProcessName if s.Process else "?"
        v = s.SimpleAudioVolume
        log(f"  {nm}: vol={round(v.GetMasterVolume() * 100)}% mute={bool(v.GetMute())}")
except Exception as e:
    import traceback
    log("[FAIL] " + repr(e))
    log(traceback.format_exc())
log("[end]")
print("done")
