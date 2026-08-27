# -*- coding: utf-8 -*-
"""重启主服务(8000)，结果写入 restart_out.txt。"""
import json
import os
import subprocess
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 主服务必须用 .venv（demucs/流水线在这里）；Qwen3-TTS worker 由服务自己拉起 venv312
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
OUT = os.path.join(HERE, "restart_out.txt")


def log(m):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(m + "\n")


open(OUT, "w").close()
# 杀掉 8000 上的旧进程
subprocess.run(["taskkill", "/F", "/FI", "WINDOWTITLE eq none"], capture_output=True)
try:
    import psutil
    for p in psutil.process_iter(["pid"]):
        pass
except ImportError:
    pass
r = subprocess.run(
    ["powershell", "-NoProfile", "-Command",
     "(Get-NetTCPConnection -LocalPort 8000 -State Listen).OwningProcess"],
    capture_output=True, text=True, shell=True)
pid = r.stdout.strip()
log(f"old pid: {pid!r}")
if pid:
    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
    time.sleep(2)
# 后台启动新进程
DETACHED = 0x00000008 | 0x08000000
proc = subprocess.Popen([PY, os.path.join(ROOT, "m2_server", "server.py")],
                        cwd=ROOT, creationflags=DETACHED,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
log(f"new pid: {proc.pid}")
ok = False
for _ in range(20):
    time.sleep(3)
    try:
        h = urllib.request.urlopen("http://127.0.0.1:8000/api/health", timeout=5)
        log(f"health: {h.read().decode()}")
        ok = True
        break
    except Exception as e:
        last = repr(e)
if not ok:
    log(f"health FAIL: {last}")
log("[end]")
