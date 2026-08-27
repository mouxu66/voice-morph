# -*- coding: utf-8 -*-
"""音频故障诊断：默认设备、全部端点状态(含禁用)、备份文件、音频服务。写 diag_out.txt。"""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
PS1 = os.path.join(os.path.dirname(HERE), "m2_server", "audio_config.ps1")
OUT = os.path.join(HERE, "diag_out.txt")


def log(m):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(m + "\n")


open(OUT, "w").close()


def run_ps(args):
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", PS1] + args, capture_output=True, text=True,
                       timeout=60, encoding="utf-8", errors="replace")
    return (r.stdout or "") + ("\n[stderr]" + r.stderr if r.stderr.strip() else "")


try:
    log("[status] " + run_ps(["-action", "status"]).strip())
    log("[diag] " + run_ps(["-action", "diag"]).strip())
    bk = os.path.join(os.environ.get("LOCALAPPDATA", ""), "rvc_audio_backup.txt")
    if os.path.exists(bk):
        log("[backup] " + open(bk, encoding="utf-8", errors="replace").read().replace("\n", " | "))
    else:
        log("[backup] NOT_EXISTS")
    svcs = subprocess.run(["powershell", "-NoProfile", "-Command",
                           "Get-Service Audiosrv,AudioEndpointBuilder | Format-Table Name,Status -AutoSize"],
                          capture_output=True, text=True, timeout=30)
    log("[services]\n" + (svcs.stdout or "").strip())
except Exception as e:
    import traceback
    log("[FAIL] " + repr(e))
    log(traceback.format_exc())
log("[end]")
print("done")
