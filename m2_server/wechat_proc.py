"""微信进程与窗口的底层操作（「重启微信再录」链路专用）。

**为什么必须重启微信**（2026-09-11 实测，证据链见 `docs/犯错指南.md` §2.15）：
微信在**进程启动时**就把采集设备绑定好了，之后再改 Windows 默认麦克风对它
不热生效——它照旧用启动时的那个设备录音。所以「切默认麦到 CABLE Output」和
「微信开始录音」之间必须夹一次微信重启，它才会重新枚举到 CABLE Output。
顺序是硬约束：**杀微信 → 切卡 → 拉起微信 → 录音**；反过来切了也白切。

本模块刻意不 import `wechat_voice`（那会造成循环 import），只做四件事：
    进程/窗口枚举、主程序定位、杀进程、拉起并等主窗口就绪。
另外附带一个只读探针 `input_device_probe()`：从微信自己的遥测里读出
「它最近一次录音实际用了哪个输入设备」，用来判断要不要重启。

依赖 psutil 时用它；没装 psutil 时退回 ctypes（EnumProcesses +
QueryFullProcessImageNameW + TerminateProcess），保证分发到别的机器也不炸。
"""

import contextlib
import logging
import os
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# 微信进程名（4.x=Weixin.exe，3.x=WeChat.exe，小程序宿主 WeChatAppEx.exe 不算主进程）
PROC_NAMES = ("weixin.exe", "wechat.exe", "wechatapp.exe")

# 兜底安装路径（本机微信 4.x 默认在第一个）
DEFAULT_EXE_CANDIDATES = (
    Path(r"C:\Program Files\Tencent\Weixin\Weixin.exe"),
    Path(r"C:\Program Files (x86)\Tencent\Weixin\Weixin.exe"),
    Path(r"C:\Program Files\Tencent\WeChat\WeChat.exe"),
    Path(r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe"),
)

# 主窗口面积阈值（px²）：微信聊天窗口 ~1300x1600≈2.1M，登录二维码窗 ~300x420≈0.13M。
# 低于阈值 = 还在登录页，此时点话筒的坐标全是错的，必须报错而不是硬录。
MIN_CHAT_AREA = int(os.environ.get("VM_WECHAT_MIN_CHAT_AREA", "200000"))

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_DETACHED = 0x00000008 if os.name == "nt" else 0
_NEW_GROUP = 0x00000200 if os.name == "nt" else 0

_WM_CLOSE = 0x0010

try:  # 可选依赖：没有就走 ctypes
    import psutil
except Exception:  # pragma: no cover - 取决于运行环境
    psutil = None


# ---------------- 进程枚举 ----------------


def _name_ok(name: str) -> bool:
    return (name or "").lower() in PROC_NAMES


def list_wechat_processes() -> list[dict]:
    """列出所有微信主进程：``[{"pid", "name", "exe"}]``（按 pid 升序，稳定可断言）。

    空列表是**可信结论**（微信没运行），不要因为它是空的就去跑 ctypes 兜底。
    """
    if psutil is not None:
        try:
            out: list[dict] = []
            for p in psutil.process_iter(["pid", "name", "exe"]):
                info = p.info
                if not _name_ok(info.get("name") or ""):
                    continue
                out.append(
                    {
                        "pid": int(info["pid"]),
                        "name": info.get("name") or "",
                        "exe": info.get("exe") or "",
                    }
                )
            return sorted(out, key=lambda d: d["pid"])
        except Exception as e:  # pragma: no cover - 取决于运行环境
            logger.debug("[wechat_proc] psutil 枚举失败，退 ctypes: %s", e)
    return _list_wechat_processes_ctypes()


def _list_wechat_processes_ctypes() -> list[dict]:
    """无 psutil 时的退路。

    注意 `EnumProcesses` 在 **psapi.dll**（Win7+ 的 kernel32 只导出 `K32EnumProcesses`），
    本机实测 `windll.kernel32.EnumProcesses` 直接 AttributeError——别想当然。
    整段失败只告警返回 []，绝不把异常抛给调用方（调用方还要去判断"要不要重启微信"）。
    """
    import ctypes
    from ctypes import wintypes

    try:
        try:
            api = ctypes.WinDLL("psapi").EnumProcesses
        except (OSError, AttributeError):
            api = ctypes.windll.kernel32.K32EnumProcesses
        k32 = ctypes.windll.kernel32
        arr = (wintypes.DWORD * 4096)()
        need = wintypes.DWORD()
        if not api(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(need)):
            return []
        n = need.value // ctypes.sizeof(wintypes.DWORD)
        out: list[dict] = []
        for i in range(n):
            pid = int(arr[i])
            if pid <= 0:
                continue
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                continue
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(buf))
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    p = Path(buf.value)
                    if _name_ok(p.name):
                        out.append({"pid": pid, "name": p.name, "exe": str(p)})
            finally:
                k32.CloseHandle(h)
        return sorted(out, key=lambda d: d["pid"])
    except Exception as e:  # pragma: no cover - 取决于运行环境
        logger.warning("[wechat_proc] ctypes 枚举进程失败: %s", e)
        return []


# ---------------- 窗口枚举 ----------------


def enum_wechat_windows() -> list[dict]:
    """枚举微信的顶层可见窗口：``[{"hwnd","pid","area","exe"}]``，按面积从大到小。

    多个命中时面积最大的那个就是聊天主窗口（托盘气泡/登录小窗都很小）。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    hits: list[dict] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k32.OpenProcess(0x1000, False, pid.value)
        if h:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = Path(buf.value).name.lower()
                if name in PROC_NAMES:
                    r = wintypes.RECT()
                    area = 0
                    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                        area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                    hits.append(
                        {"hwnd": int(hwnd), "pid": int(pid.value), "area": area, "exe": buf.value}
                    )
            k32.CloseHandle(h)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return sorted(hits, key=lambda d: d["area"], reverse=True)


def _window_is_iconic(hwnd: int) -> bool:
    """窗口是否最小化（IsIconic）。

    最小化到任务栏（点一下就能还原）和收进托盘（根本没有主窗口）是两码事：
    前者 `_foreground_wechat` 的 SW_RESTORE 能自愈，不该在找窗口这一步拦死；
    后者没有任何东西可恢复，必须报错。
    """
    if os.name != "nt":
        return False
    import ctypes

    return bool(ctypes.windll.user32.IsIconic(hwnd))


def find_wechat_hwnd() -> int:
    """找微信主窗口句柄（面积最大的那个）。找不到时抛**分了因**的 RuntimeError。

    报错按「用户下一步该干什么」分三种，别再让用户对着一句含糊的
    「没找到微信窗口」猜（2026-09-18 实测：微信收进托盘后 enum 只剩一个
    ~9243px² 的残窗，旧判据 area<=0 直接放行，后续点击全部落空）：
        1. 进程都没有         → 微信没开：去打开并登录；
        2. 进程在、无可见窗   → 收进托盘了：点开聊天窗口；
        3. 最大窗 < MIN_CHAT_AREA 且非最小化 → 登录页或无关小窗：先登录/打开聊天窗。
    最小化（iconic）的小窗**放行**：SW_RESTORE 能拉回来，是历史可用路径。
    `wechat_voice._find_wechat_hwnd` 直接复用本函数与文案。
    """
    procs = list_wechat_processes()
    wins = enum_wechat_windows()
    if not procs:
        raise RuntimeError("微信没有在运行：请先打开微信并登录、进入聊天窗口，再重试发送")
    if not wins:
        raise RuntimeError(
            "微信在运行（pid: {}）但没有任何可见窗口——多半被关进了托盘："
            "请点开微信主窗口、进入聊天界面后重试".format(
                ", ".join(str(p["pid"]) for p in procs[:3])
            )
        )
    if wins[0]["area"] < MIN_CHAT_AREA and not _window_is_iconic(wins[0]["hwnd"]):
        raise RuntimeError(
            "微信主窗口没就绪（最大窗口 %dpx²，正常聊天窗口约 2,000,000px²）："
            "要么还停在登录页（先扫码登录），要么开着的是无关小窗——"
            "请把微信聊天窗口打开后重试" % wins[0]["area"]
        )
    return wins[0]["hwnd"]


# ---------------- 主程序定位 ----------------


def resolve_wechat_exe() -> Path | None:
    """定位微信主程序：环境变量 → 运行中的进程 → 注册表 App Paths → 常见安装目录。"""
    env = os.environ.get("VM_WECHAT_EXE", "").strip()
    if env and Path(env).exists():
        return Path(env)

    # 运行中的进程里挑主进程：优先「拥有最大窗口」的那个，退化则取最小 pid
    wins = enum_wechat_windows()
    if wins and wins[0].get("exe"):
        return Path(wins[0]["exe"])
    for p in list_wechat_processes():
        if p.get("exe"):
            return Path(p["exe"])

    reg = _registry_exe()
    if reg:
        return reg
    for c in DEFAULT_EXE_CANDIDATES:
        if c.exists():
            return c
    return None


def _registry_exe() -> Path | None:
    """注册表 App Paths 里登记的微信路径（读不到就 None）。"""
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return None
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for exe in ("Weixin.exe", "WeChat.exe"):
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, f"{sub}\\{exe}") as k:
                    val, _ = winreg.QueryValueEx(k, "")
            except OSError:
                continue
            if val and Path(val).exists():
                return Path(val)
    return None


# ---------------- 杀 / 拉起 / 等就绪 ----------------


def kill_wechat(grace_s: float = 2.5, poll: float = 0.25) -> dict:
    """关掉微信所有主进程。先在主窗口发 WM_CLOSE 给一次体面退出的机会，超时再强杀。

    注意：微信的关闭按钮默认是「收进托盘」，所以 WM_CLOSE 大概率不会真退出——
    grace 必须给得短（默认 2.5s），别指望它。
    返回 ``{"pids": [...], "graceful": bool, "forced": bool, "remaining": [...]}``。
    """
    procs = list_wechat_processes()
    pids = [p["pid"] for p in procs]
    if not pids:
        return {"pids": [], "graceful": False, "forced": False, "remaining": []}

    graceful = False
    wins = [w for w in enum_wechat_windows() if w["pid"] in pids]
    if wins:
        try:
            import ctypes

            ctypes.windll.user32.PostMessageW(wins[0]["hwnd"], _WM_CLOSE, 0, 0)
            graceful = True
        except Exception as e:  # pragma: no cover
            logger.debug("[wechat_proc] PostMessage WM_CLOSE 失败: %s", e)

    deadline = time.time() + max(0.0, grace_s)
    while time.time() < deadline:
        if not list_wechat_processes():
            return {"pids": pids, "graceful": graceful, "forced": False, "remaining": []}
        time.sleep(poll)

    _force_kill(pids)
    deadline = time.time() + 8.0
    while time.time() < deadline:
        left = [p["pid"] for p in list_wechat_processes()]
        if not left:
            break
        time.sleep(poll)
    remaining = [p["pid"] for p in list_wechat_processes()]
    if remaining:
        logger.warning("[wechat_proc] 强杀后仍在运行: %s", remaining)
    return {"pids": pids, "graceful": graceful, "forced": True, "remaining": remaining}


def _force_kill(pids: list[int]) -> None:
    if psutil is not None:
        for pid in pids:
            try:
                p = psutil.Process(pid)
                for ch in p.children(recursive=True):  # 先子后父，避免父先死导致子成孤儿
                    with contextlib.suppress(Exception):
                        ch.kill()
                p.kill()
            except Exception:
                pass
        return
    for pid in pids:  # 无 psutil：taskkill /T 连子孙一起
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=20,
                creationflags=_NO_WINDOW,
            )


def start_wechat(exe: Path | str, args: list[str] | None = None) -> int:
    """拉起微信，返回新进程 pid（fire-and-forget，不等窗口）。

    用 DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP：微信不该绑在调用方的控制台上，
    也不该跟着后台服务一起被信号带走。
    """
    exe = Path(exe)
    if not exe.exists():
        raise RuntimeError(f"微信主程序不存在: {exe}")
    try:
        proc = subprocess.Popen(
            [str(exe)] + list(args or []),
            cwd=str(exe.parent),
            close_fds=True,
            creationflags=_DETACHED | _NEW_GROUP,
        )
    except Exception as e:
        raise RuntimeError(f"拉起微信失败（{exe}）: {e}") from e
    logger.info("[wechat_proc] 已拉起微信: pid=%s exe=%s", proc.pid, exe)
    return int(proc.pid)


def wait_wechat_ready(
    timeout_s: float = 90.0, min_area: int | None = None, poll: float = 0.5
) -> dict:
    """等微信主窗口出现且面积达标（= 不在登录页）。

    返回 ``{"hwnd","pid","area","waited_s"}``；超时抛 RuntimeError 并区分两种情形：
    进程根本没起来 / 起来了但停在登录页（后者要用户先扫码登录，硬录只会录到错坐标）。
    """
    t0 = time.time()
    min_area = MIN_CHAT_AREA if min_area is None else int(min_area)
    best_area = 0
    while True:
        wins = enum_wechat_windows()
        if wins:
            best_area = max(best_area, wins[0]["area"])
            if wins[0]["area"] >= min_area:
                return {
                    "hwnd": wins[0]["hwnd"],
                    "pid": wins[0]["pid"],
                    "area": wins[0]["area"],
                    "waited_s": round(time.time() - t0, 1),
                }
        if time.time() - t0 >= timeout_s:
            if not list_wechat_processes():
                raise RuntimeError(f"微信进程没起来（等了 {timeout_s:.0f}s），请手动打开微信后重试")
            raise RuntimeError(
                f"微信窗口 {timeout_s:.0f}s 内未就绪（最大窗口 {best_area}px² < {min_area}，"
                "疑似停在登录页）；请先在微信里完成登录，再重试发送"
            )
        time.sleep(poll)


# ---------------- 遥测探针：微信最近一次录音用的输入设备 ----------------

_KVCOMM = Path(os.environ.get("APPDATA", "")) / "Tencent" / "xwechat" / "net" / "kvcomm"
# 设备行形如：`96,1,165,184,205,0,0,0,0,麦克风阵列 (Senary Audio),,,`
# 前 9 个字段是音频指标，第 10 个是设备名。设备名必带驱动括号后缀
# （「麦克风阵列 (Senary Audio)」「CABLE Output (VB-Audio Virtual Cable)」），
# 据此过滤掉同文件里其它 CSV 行 —— 实测单独用数字前缀会命中 37 条假阳性。
_DEV_LINE = re.compile(rb"(?:[0-9]{1,8},){9}([^,\x00-\x1f]{2,80})")


def input_device_probe() -> dict:
    """读微信遥测，返回它最近一次录音实际使用的输入设备名。

    返回 ``{"device": str|None, "file": str|None, "hits": int}``。
    只读，不改微信任何东西；读不到一律返回 device=None（调用方按"保守重启"处理）。
    """
    if not _KVCOMM.is_dir():
        return {"device": None, "file": None, "hits": 0}
    files = sorted(_KVCOMM.glob("*_input.statistic"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[:8]:
        try:
            blob = f.read_bytes()
        except OSError:
            continue
        name = _device_from_blob(blob)
        if name:
            return {"device": name, "file": f.name, "hits": 1}
    return {"device": None, "file": None, "hits": 0}


def _device_from_blob(blob: bytes) -> str | None:
    """从一份 input.statistic 里抠出设备名；定位在最后一次 start_record 附近。"""
    i = blob.rfind(b"start_record")
    if i < 0:
        return None
    j = blob.find(b"end_record", i)
    seg = blob[i : (j if j > i else i + 2048)]
    for h in _DEV_LINE.findall(seg):
        s = h.decode("utf-8", "replace").strip()
        if "(" in s and ")" in s:  # 设备名必带括号后缀，用它挡住假阳性
            return s
    return None


def last_input_device() -> str | None:
    return input_device_probe()["device"]
