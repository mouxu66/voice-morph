# -*- coding: utf-8 -*-
"""重启主服务(8000)，结果写入 restart_out.txt。

纯 Python 实现：用 psutil 查端口占用、收尾旧进程，全程不拉起 PowerShell。
（早期版本靠 `powershell -Command "Get-NetTCPConnection ..."` 查 PID，
 这类调用容易触发杀软对 PowerShell 脚本行为的启发式告警。）
"""
import os
import subprocess
import sys
import time
import urllib.request

try:
    import psutil
except ImportError:
    sys.exit("缺少 psutil，请先执行：.venv\\Scripts\\python.exe -m pip install psutil")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 主服务必须用 .venv（demucs/流水线在这里）；Qwen3-TTS worker 由服务自己拉起 venv312
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
OUT = os.path.join(HERE, "restart_out.txt")
PORT = 8000
HEALTH = "http://127.0.0.1:%d/api/health" % PORT


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def pids_listening_on(port):
    """占用该端口且处于 LISTEN 的进程 PID 列表（纯 psutil，不调 PowerShell）"""
    found = []
    for conn in psutil.net_connections(kind="inet"):
        if not conn.laddr or conn.laddr.port != port:
            continue
        if conn.status != psutil.CONN_LISTEN:
            continue
        if conn.pid and conn.pid not in found:
            found.append(conn.pid)
    return found


def kill_pid(pid):
    """先优雅退出，超时再强杀"""
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    name = proc.name()
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (psutil.TimeoutExpired, psutil.AccessDenied, psutil.NoSuchProcess):
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass
    log("已结束 PID %d (%s)" % (pid, name))


open(OUT, "w", encoding="utf-8").close()

if not os.path.exists(PY):
    sys.exit("找不到解释器：%s" % PY)

# 1) 结束占用 8000 的旧进程
old = pids_listening_on(PORT)
log("端口 %d 上的旧进程：%s" % (PORT, old or "无"))
for pid in old:
    kill_pid(pid)
if old:
    time.sleep(2)

# 2) 后台拉起新服务
DETACHED = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
proc = subprocess.Popen(
    [PY, os.path.join(ROOT, "m2_server", "server.py")],
    cwd=ROOT,
    creationflags=DETACHED,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
log("新服务 PID：%d" % proc.pid)

# 3) 等服务就绪（模型加载可能较慢，最多等 60s）
ok = False
last = ""
for _ in range(30):
    time.sleep(2)
    try:
        with urllib.request.urlopen(HEALTH, timeout=5) as resp:
            log("health 200：%s" % resp.read().decode()[:120])
            ok = True
            break
    except Exception as exc:  # noqa: BLE001 - 启动探测阶段统一吞掉，最后一次单独记录
        last = repr(exc)

log("启动成功" if ok else "启动失败，最后一次错误：%s" % last)
log("[end]")
sys.exit(0 if ok else 1)
