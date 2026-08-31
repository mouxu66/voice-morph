# -*- coding: utf-8 -*-
"""一键部署：构建前端 -> 打 Windows 包 -> 静默覆盖安装 -> 启动应用。

纯 Python，全程不拉起 PowerShell（避免杀软对 .ps1 的启发式误报）。

用法：
    D:\\变声\\.venv\\Scripts\\python.exe D:\\变声\\web\\deploy.py
    D:\\变声\\.venv\\Scripts\\python.exe D:\\变声\\web\\deploy.py --no-launch  # 装完不自动启动
"""
import argparse
import json
import os
import subprocess
import sys
import time

try:
    import psutil
except ImportError:
    sys.exit("缺少 psutil，请先执行：.venv\\Scripts\\python.exe -m pip install psutil")

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(WEB_DIR)
APP_NAME = "变声工坊.exe"
DETACHED = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def setup_path():
    """安装包名带版本号（变声工坊 Setup 0.2.0.exe），从 package.json 读，避免版本升级后找不到"""
    with open(os.path.join(WEB_DIR, "package.json"), encoding="utf-8") as f:
        version = json.load(f)["version"]
    return os.path.join(WEB_DIR, "release2", "变声工坊 Setup %s.exe" % version)


def installed_path():
    """NSIS 会记住上次的安装目录；这里按可能性依次探测"""
    candidates = [
        os.path.join(ROOT, "voice-morph-desktop", APP_NAME),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "变声工坊", APP_NAME),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "变声工坊", APP_NAME),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def kill_app():
    """结束正在运行的应用实例，否则安装时文件被占用会失败"""
    killed = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] != APP_NAME:
                continue
            proc.terminate()
            killed.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if not killed:
        log("没有正在运行的应用实例")
        return
    deadline = time.time() + 8
    while time.time() < deadline:
        if not any(psutil.pid_exists(pid) for pid in killed):
            break
        time.sleep(0.5)
    for pid in killed:
        if psutil.pid_exists(pid):
            try:
                psutil.Process(pid).kill()
            except psutil.NoSuchProcess:
                pass
    log("已结束应用进程：%s" % killed)
    time.sleep(1)


def run(cmd, title):
    log("==> %s" % title)
    proc = subprocess.run(cmd, cwd=WEB_DIR, shell=True)
    if proc.returncode != 0:
        sys.exit("[失败] %s（退出码 %d）" % (title, proc.returncode))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-launch", action="store_true", help="装完不自动启动应用")
    args = parser.parse_args()

    kill_app()
    run("npm run build", "构建前端")
    run("npx electron-builder --win", "打包 Windows 安装包")

    setup = setup_path()
    if not os.path.exists(setup):
        sys.exit("[失败] 未找到安装包：%s" % setup)
    log("安装包：%s（%.1f MB）" % (setup, os.path.getsize(setup) / 1024 / 1024))

    log("==> 静默覆盖安装")
    proc = subprocess.run('"%s" /S' % setup, cwd=WEB_DIR, shell=True)
    if proc.returncode != 0:
        sys.exit("[失败] 安装退出码 %d（若提示文件被占用，请手动关闭应用后重试）" % proc.returncode)

    target = installed_path()
    if not target:
        log("安装完成，但未在预期路径找到可执行文件，请手动启动")
        return
    log("安装完成：%s（%s）" % (target, time.strftime("%H:%M:%S", time.localtime(os.path.getmtime(target)))))

    if args.no_launch:
        return
    log("启动应用")
    subprocess.Popen([target], cwd=os.path.dirname(target), creationflags=DETACHED)


if __name__ == "__main__":
    main()
