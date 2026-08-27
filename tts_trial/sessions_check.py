# -*- coding: utf-8 -*-
"""列出当前所有音频会话及所属进程 + 枚举 Senary 音频控制器 PnP 设备实例。写 sess_out.txt。"""
import os

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sess_out.txt")


def log(m):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(m + "\n")


open(OUT, "w").close()


def main():
    import comtypes
    from comtypes import CLSCTX_ALL, GUID
    from ctypes import POINTER
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    from pycaw.api.audiopolicy import IAudioSessionControl2
    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)

    log("[sessions]")
    for s in AudioUtilities.GetAllSessions():
        pid = None
        nm = "?"
        try:
            ctl2 = s._ctl.QueryInterface(IAudioSessionControl2)
            pid = ctl2.GetProcessId()
        except Exception as e:
            nm = f"pid_err:{e}"
        if pid:
            try:
                import psutil
            except ImportError:
                psutil = None
            try:
                import subprocess as sp
                q = sp.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
                           capture_output=True, text=True).stdout.splitlines()
                nm = q[-1] if len(q) > 3 else "?"
            except Exception:
                pass
        vol = s.SimpleAudioVolume
        log(f"  pid={pid} name={nm} vol={round(vol.GetMasterVolume()*100)}% mute={bool(vol.GetMute())}")


try:
    import threading

    def _wrap(fn):
        try:
            fn()
        except Exception as e:
            import traceback
            log("[thread-FAIL] " + repr(e))
            log(traceback.format_exc())

    th = threading.Thread(target=lambda: _wrap(main), daemon=True)
    th.start()
    th.join(20)
except Exception as e:
    import traceback
    log("[FAIL] " + repr(e))
    log(traceback.format_exc())

# PnP 设备部分不涉及 COM
import subprocess
r = subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Get-PnpDevice | Where-Object { $_.FriendlyName -match 'Senary' } | "
                    "Format-Table Status,Class,FriendlyName,InstanceId -AutoSize"],
                   capture_output=True, text=True, timeout=60)
log("[pnp-senary]\n" + (r.stdout or "").strip())
log("[end]")
print("done")
