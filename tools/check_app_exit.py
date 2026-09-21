"""验证「关掉主窗后应用是否真的退干净」。

背景（2026-09-17 用户实测踩到）：Electron 的 `window-all-closed` 要"一个窗口都不剩"
才触发。只要漏掉任何一个辅助窗口（哪怕是 hide 掉的置顶横幅），`app.quit()` 就永不执行
→ 主窗关了、桌宠也销毁了、任务栏也没图标，进程却留在后台，**只能开任务管理器杀**。
修复见 commit af4851c（alt-hint 窗口改为真 destroy + before-quit 兜底 + 显式 quit）。

这个脚本做端到端验证：向主窗口发 WM_CLOSE（**等同用户点右上角 X**，不是强杀），
然后采样进程数 / 窗口数 / 后端端口，看它们是否都归零。

用法（**应用必须正在运行**）：

    py tools/check_app_exit.py            # 关掉主窗并等待退出
    py tools/check_app_exit.py --no-close # 只看当前状态，不关窗口

退出码：0 = 全部退干净；1 = 仍有残留；2 = 应用没在跑 / 找不到主窗口。

为什么用 Python ctypes 而不是 PowerShell：
    PowerShell 的 `Add-Type` 在本机被安全策略拦掉（"compiles and loads .NET code
    at runtime"），没法调 Win32 API。ctypes 直调 user32 可以绕开，且无额外依赖。

坑：`Get-Process.MainWindowTitle` **一个进程只返回一个窗口** —— 主窗「变声工坊」
和「桌宠」同属一个 PID 时，它只报「桌宠」，会让人误以为主窗已经没了。
要列全窗口必须用 EnumWindows（本脚本就是这么做的）。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time

APP_NAME = "变声工坊"
MAIN_TITLE_HINT = "变声工坊"   # 主窗口标题前缀（完整是「变声工坊 · Voice Morph Studio」）
BACKEND_PORT = 8000
WM_CLOSE = 0x0010
TIMEOUT_S = 25.0
SAMPLE_S = 2.5

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_PostMessage = _user32.PostMessageW
_EnumWindows = _user32.EnumWindows
_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
_GetWindowText = _user32.GetWindowTextW
_GetWindowTextLength = _user32.GetWindowTextLengthW
_IsWindowVisible = _user32.IsWindowVisible
_GetWindowThreadProcessId = _user32.GetWindowThreadProcessId

_PS = ["powershell", "-NoProfile", "-Command"]


def _ps(cmd: str) -> str:
    r = subprocess.run(_PS + [cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="ignore")
    return r.stdout or ""


def app_pids() -> list[int]:
    out = _ps(f'Get-Process -Name "{APP_NAME}" -ErrorAction SilentlyContinue'
              " | ForEach-Object { $_.Id }")
    return sorted({int(x) for x in out.split() if x.strip().isdigit()})


def app_windows() -> list[tuple[int, str, int]]:
    """列出该应用所有**可见**窗口：(pid, 标题, hwnd)。"""
    pids = set(app_pids())
    rows: list[tuple[int, str, int]] = []

    def cb(hwnd: wt.HWND, _l: wt.LPARAM) -> bool:
        if not _IsWindowVisible(hwnd):
            return True
        n = _GetWindowTextLength(hwnd)
        if not n:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _GetWindowText(hwnd, buf, n + 1)
        if buf.value:
            pid = wt.DWORD()
            _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                rows.append((pid.value, buf.value, int(hwnd)))
        return True

    _EnumWindows(_EnumWindowsProc(cb), 0)
    return rows


def port_listening(port: int) -> bool:
    out = _ps(f'$c = Get-NetTCPConnection -LocalPort {port} -State Listen'
              " -ErrorAction SilentlyContinue; if ($c) { 'YES' }")
    return "YES" in out


def main() -> int:
    # 输出编码不是装饰：Windows 下 stdout 被重定向（管道/文件）时按 ANSI(cp936) 编码，
    # 而本文件要打印的 `✓`/`✗` 在 GBK 之外 → UnicodeEncodeError + 报告断掉。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

    if "--no-close" in sys.argv:
        pids, wins = app_pids(), app_windows()
        print(f"进程 {len(pids)} 个: {pids}")
        print(f"可见窗口 {len(wins)} 个: {[w[1] for w in wins]}")
        print(f"{BACKEND_PORT} 端口: {'占用' if port_listening(BACKEND_PORT) else '空闲'}")
        return 0

    pids0, wins0 = app_pids(), app_windows()
    if not pids0:
        print(f"{APP_NAME} 没在运行。请先启动应用再跑本脚本。")
        return 2
    print(f"[关闭前] 进程 {len(pids0)} 个  窗口 {len(wins0)} 个: {[w[1] for w in wins0]}")

    mains = [w for w in wins0 if MAIN_TITLE_HINT in w[1]]
    if not mains:
        print("找不到主窗口（应用可能还在启动，或主窗已被关掉）。")
        return 2

    hwnd, title = mains[0][2], mains[0][1]
    print(f"\n>>> 向主窗 [{title}] (0x{hwnd:X}) 发 WM_CLOSE —— 等同用户点右上角 X")
    if not _PostMessage(wt.HWND(hwnd), WM_CLOSE, 0, 0):
        print("PostMessage 失败")
        return 2

    print(f"\n最多等 {TIMEOUT_S:.0f}s，每 {SAMPLE_S}s 采样：")
    elapsed = 0.0
    while elapsed < TIMEOUT_S:
        time.sleep(SAMPLE_S)
        elapsed += SAMPLE_S
        pids, wins = app_pids(), app_windows()
        extra = f"  残留窗口={[w[1] for w in wins]}" if wins else ""
        print(f"  t={elapsed:>5.1f}s  进程 {len(pids):>2}  窗口 {len(wins):>2}{extra}")
        if not pids:
            break

    print()
    if app_pids():
        print(f"❌ {TIMEOUT_S:.0f}s 后仍有 {len(app_pids())} 个进程存活：{app_pids()}")
        print(f"   残留窗口：{[w[1] for w in app_windows()]}")
        return 1

    port_busy = port_listening(BACKEND_PORT)
    print(f"✅ 通过：{elapsed:.1f}s 内进程全部退出（关主窗 = 真的关掉应用）")
    print(f"   后端 {BACKEND_PORT} 端口：{'仍占用 ⚠' if port_busy else '已释放 ✓'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
