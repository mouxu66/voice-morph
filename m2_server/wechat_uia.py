"""微信 4.x UIA 结构化访问层（方案 A 的基础设施）。

**为什么需要**
微信 4.x 聊天区是自绘渲染，UIAutomation 默认只暴露一个 Qt 空壳窗口
（`Qt51514QWindowIcon`），拿不到任何控件。把 `Weixin.dll` 里 Qt accessibility
gate 的一个 active byte 从 0 改成 1（社区库 wechatauto-replica 的做法，本机
2026-09-10 在 4.1.13.12 上实测复现），UIA 树立即物化为 `mmui::MainWindow`，
于是能拿到**带精确矩形的结构化控件**：

    mmui::XButton        '发语音 ( 按住右 Alt )'   语音按钮本体
    mmui::ChatVoiceRecordView                     录音浮层（存在=正在录音）
      ├─ mmui::XButton       '取消'
      └─ mmui::XMouseEventView '发送语音'
    mmui::ChatVoiceItemView  '语音15"秒'           历史语音消息（带真实时长）

**用途边界（方案 A）**
只把「检测录音浮层」「发送结果校验」「浮层内按钮定位」三处交给 UIA，
像素链路（模板匹配 / 绿钮 HSV）完整保留为降级路径。任何 UIA 查询失败一律
返回 None / False，**绝不抛异常**，调用方据此回退像素法。

**风险与开关**
热激活会向微信进程写入 1 个字节（微信重启后失效，需重新激活）。
`VM_WECHAT_UIA=0` 可完全关闭本模块，退回纯像素链路。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import struct
import threading
import time
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 配置与常量

UIA_ENABLED = os.environ.get("VM_WECHAT_UIA", "1") == "1"
SEARCH_DEPTH = int(os.environ.get("VM_WECHAT_UIA_DEPTH", "25"))
SEARCH_TIMEOUT = float(os.environ.get("VM_WECHAT_UIA_TIMEOUT", "0.6"))

CLASS_MAIN = "mmui::MainWindow"
CLASS_INPUT = "mmui::ChatInputField"
CLASS_VOICE_BTN = "mmui::XButton"
NAME_VOICE_BTN = "发语音 ( 按住右 Alt )"
CLASS_OVERLAY = "mmui::ChatVoiceRecordView"
NAME_CANCEL = "取消"
NAME_SEND = "发送语音"
CLASS_VOICE_MSG = "mmui::ChatVoiceItemView"

# --- Qt accessibility gate 扫描（与 tools/uia_probe.py 同源） ---
QACCESSIBLE_CORE_STRING = b"qt.accessibility.core"
QACCESSIBLE_GATE_PATTERN = re.compile(
    rb"\x48\x85\xc9\x0f\x84....\x80\x3d(?P<disp>.{4})" rb"\x00\x0f\x84",
    re.DOTALL,
)
IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_WRITE = 0x80000000
MAX_XREF_DISTANCE = 0x20000

TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_MODULE_NAME32 = 255
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008

_lock = threading.Lock()
_state: dict = {"pid": None, "ready": False, "rva": None, "reason": "", "checked": 0.0}
_tls = threading.local()  # 每线程缓存 (hwnd, root)，UIA COM 对象不可跨线程
_rva_cache_path: Path | None = None


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("th32ModuleID", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("GlblcntUsage", ctypes.c_ulong),
        ("ProccntUsage", ctypes.c_ulong),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_ubyte)),
        ("modBaseSize", ctypes.c_ulong),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * (MAX_MODULE_NAME32 + 1)),
        ("szExePath", ctypes.c_wchar * 260),
    ]


# -------------------------------------------------------------- PE 解析 / 扫描


def _pe_sections(data: bytes) -> list[dict]:
    try:
        pe_off = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe_off : pe_off + 4] != b"PE\0\0":
            return []
        coff = pe_off + 4
        count = struct.unpack_from("<H", data, coff + 2)[0]
        opt_size = struct.unpack_from("<H", data, coff + 16)[0]
        sec_off = coff + 20 + opt_size
        out = []
        for i in range(count):
            off = sec_off + i * 40
            name = data[off : off + 8].split(b"\0", 1)[0].decode("ascii", "ignore")
            vsize, rva, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
            chars = struct.unpack_from("<I", data, off + 36)[0]
            out.append(
                {
                    "name": name,
                    "rva": rva,
                    "vsize": vsize,
                    "raw_size": raw_size,
                    "raw_ptr": raw_ptr,
                    "chars": chars,
                }
            )
        return out
    except Exception:
        return []


def _section_for_rva(sections, rva):
    for sec in sections:
        if sec["rva"] <= rva < sec["rva"] + max(sec["vsize"], sec["raw_size"]):
            return sec
    return None


def _offset_to_rva(sections, offset):
    for sec in sections:
        if sec["raw_ptr"] <= offset < sec["raw_ptr"] + sec["raw_size"]:
            return sec["rva"] + offset - sec["raw_ptr"]
    return None


def _rip_xrefs(data: bytes, sections, target_rva) -> list[int]:
    xrefs = []
    for sec in sections:
        if not (sec["chars"] & IMAGE_SCN_MEM_EXECUTE):
            continue
        start = sec["raw_ptr"]
        raw = data[start : min(len(data), start + sec["raw_size"])]
        for i in range(0, max(0, len(raw) - 7)):
            if 0x40 <= raw[i] <= 0x4F and raw[i + 1] == 0x8D and (raw[i + 2] & 0xC7) == 0x05:
                disp = struct.unpack_from("<i", raw, i + 3)[0]
                if sec["rva"] + i + 7 + disp == target_rva:
                    xrefs.append(sec["rva"] + i)
            if raw[i] == 0x8D and (raw[i + 1] & 0xC7) == 0x05:
                disp = struct.unpack_from("<i", raw, i + 2)[0]
                if sec["rva"] + i + 6 + disp == target_rva:
                    xrefs.append(sec["rva"] + i)
    return xrefs


def scan_gate_rva(dll_path: Path) -> tuple[int | None, list]:
    """扫描 Weixin.dll 找 Qt accessibility gate 的 active byte RVA。

    返回 (最佳候选 RVA, 候选列表)；找不到返回 (None, [])。只读文件，零风险。
    """
    try:
        data = dll_path.read_bytes()
    except Exception as e:
        logger.warning("[uia] 读取 %s 失败: %s", dll_path, e)
        return None, []
    sections = _pe_sections(data)
    if not sections:
        return None, []

    core_off = data.find(QACCESSIBLE_CORE_STRING)
    core_rva = _offset_to_rva(sections, core_off) if core_off >= 0 else None
    core_xrefs = _rip_xrefs(data, sections, core_rva) if core_rva is not None else []

    candidates = []
    for m in QACCESSIBLE_GATE_PATTERN.finditer(data):
        match_rva = _offset_to_rva(sections, m.start())
        disp_rva = _offset_to_rva(sections, m.start("disp"))
        if match_rva is None or disp_rva is None:
            continue
        match_sec = _section_for_rva(sections, match_rva)
        if not match_sec or not (match_sec["chars"] & IMAGE_SCN_MEM_EXECUTE):
            continue
        target_rva = (disp_rva - 2) + 7 + struct.unpack("<i", m.group("disp"))[0]
        target_sec = _section_for_rva(sections, target_rva)
        if not target_sec or not (target_sec["chars"] & IMAGE_SCN_MEM_WRITE):
            continue
        if core_xrefs:
            distance = min(abs(match_rva - x) for x in core_xrefs)
            if distance > MAX_XREF_DISTANCE:
                continue
        else:
            distance = 0x7FFFFFFF
        candidates.append((distance, target_rva))
    if not candidates:
        return None, []
    candidates.sort(key=lambda it: it[0])
    return candidates[0][1], candidates


# ------------------------------------------------------------ 进程 / 模块 / 内存


def _weixin_dll(pid: int):
    """在微信进程里找 Weixin.dll，返回 (基址, 大小, 路径)。"""
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    k32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return None
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not k32.Module32FirstW(snap, ctypes.byref(entry)):
            return None
        while True:
            if (entry.szModule or "").lower() == "weixin.dll":
                base = ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
                return base, int(entry.modBaseSize), entry.szExePath
            if not k32.Module32NextW(snap, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snap)
    return None


def _open_process(pid: int, write: bool):
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    access = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
    if write:
        access |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION
    return k32.OpenProcess(access, False, pid)


def read_byte(pid: int, address: int) -> int | None:
    k32 = ctypes.windll.kernel32
    h = _open_process(pid, write=False)
    if not h:
        return None
    try:
        buf = (ctypes.c_ubyte * 1)()
        read = ctypes.c_size_t(0)
        if k32.ReadProcessMemory(h, ctypes.c_void_p(address), buf, 1, ctypes.byref(read)):
            return int(buf[0])
        return None
    finally:
        k32.CloseHandle(h)


def write_byte(pid: int, address: int, value: int) -> bool:
    k32 = ctypes.windll.kernel32
    h = _open_process(pid, write=True)
    if not h:
        return False
    try:
        buf = (ctypes.c_ubyte * 1)(value & 0xFF)
        written = ctypes.c_size_t(0)
        ok = k32.WriteProcessMemory(h, ctypes.c_void_p(address), buf, 1, ctypes.byref(written))
        return bool(ok and written.value == 1)
    finally:
        k32.CloseHandle(h)


def _rva_cache_file() -> Path:
    global _rva_cache_path
    if _rva_cache_path is None:
        try:
            from config import OUTPUTS_DIR

            base = Path(OUTPUTS_DIR)
        except Exception:
            base = Path(__file__).resolve().parent.parent / "outputs"
        _rva_cache_path = base / ".wechat_uia_gate.json"
    return _rva_cache_path


def _load_cached_rva(dll_path: Path, size: int, mtime: float) -> int | None:
    try:
        blob = json.loads(_rva_cache_file().read_text(encoding="utf-8"))
    except Exception:
        return None
    item = blob.get(str(dll_path))
    if not item or item.get("size") != size or abs(item.get("mtime", 0) - mtime) > 1:
        return None
    return int(item["rva"])


def _save_cached_rva(dll_path: Path, size: int, mtime: float, rva: int) -> None:
    p = _rva_cache_file()
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(blob, dict):
            blob = {}
    except Exception:
        blob = {}
    blob[str(dll_path)] = {"size": size, "mtime": mtime, "rva": rva}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:
        logger.debug("[uia] RVA 缓存写入失败: %s", e)


def _dll_mtime(path: Path) -> tuple[int, float]:
    try:
        st = path.stat()
        return int(st.st_size), st.st_mtime
    except Exception:
        return 0, 0.0


# ---------------------------------------------------------------- 热激活


def _wechat_hwnd_pid():
    try:
        import wechat_voice as wv

        hwnd = wv._find_wechat_hwnd()
    except Exception:
        return None, None
    if not hwnd:
        return None, None
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd, pid.value


def _root_class(hwnd: int) -> str | None:
    """读 UIA 根控件类名——判据：`mmui::` 开头=已物化。"""
    try:
        import uiautomation as auto

        auto.InitializeUIAutomationInCurrentThread()
        auto.SetGlobalSearchTimeout(min(SEARCH_TIMEOUT, 1.0))
        ctrl = auto.ControlFromHandle(hwnd)
        return (ctrl.ClassName or "") if ctrl else None
    except Exception as e:
        logger.debug("[uia] 根控件读取失败: %s", e)
        return None


def ensure_active(force: bool = False) -> bool:
    """确保微信 UIA 树已物化（必要时写 1 个字节热激活）。失败返回 False。

    结果按微信 PID 缓存；微信重启后 PID 变化会自动重新激活。
    """
    if not UIA_ENABLED:
        return False
    with _lock:
        hwnd, pid = _wechat_hwnd_pid()
        if not pid:
            _state.update(pid=None, ready=False, reason="微信未运行")
            return False
        if not force and _state["pid"] == pid and _state["ready"]:
            return True
        # PID 未变但仍标记未就绪时，允许 3 秒一次的重新探测（避免高频扫盘）
        if (
            not force
            and _state["pid"] == pid
            and not _state["ready"]
            and time.time() - _state["checked"] < 3.0
        ):
            return False
        _state["checked"] = time.time()
        _state["pid"] = pid

        mod = _weixin_dll(pid)
        if not mod:
            _state.update(ready=False, reason="未找到 Weixin.dll（可能是 3.9 版）")
            logger.info("[uia] %s", _state["reason"])
            return False
        base, size, dll_path = mod
        dll = Path(dll_path)
        fsize, mtime = _dll_mtime(dll)

        rva = _load_cached_rva(dll, fsize, mtime)
        if rva is None:
            rva, cands = scan_gate_rva(dll)
            if rva is None:
                _state.update(ready=False, rva=None, reason="扫描不到 accessibility gate")
                logger.warning("[uia] %s", _state["reason"])
                return False
            _save_cached_rva(dll, fsize, mtime, rva)
            logger.info("[uia] 扫描到 gate RVA=0x%x（%d 个候选）", rva, len(cands))
        _state["rva"] = rva
        _state["dll"] = dll_path

        addr = base + rva
        val = read_byte(pid, addr)
        if val is None:
            _state.update(ready=False, reason="读进程内存失败（权限不足）")
            logger.warning("[uia] %s", _state["reason"])
            return False
        if val == 0:
            if not write_byte(pid, addr, 1):
                _state.update(ready=False, reason="写内存失败")
                logger.warning("[uia] %s", _state["reason"])
                return False
            logger.info("[uia] 已热激活 WeChat UIA（addr=0x%x）", addr)
            time.sleep(0.5)  # 等 Qt 物化控件树

        cls = _root_class(hwnd)
        ready = bool(cls and cls.startswith("mmui::"))
        _state.update(ready=ready, reason="" if ready else f"UIA 树未物化（root={cls}）")
        if not ready:
            logger.warning("[uia] %s", _state["reason"])
        return ready


def uia_ready() -> bool:
    """UIA 是否可用（热激活成功且树已物化）。结果带缓存。"""
    return ensure_active()


def reset_state() -> None:
    """清空激活缓存与线程级 root 缓存（测试与微信重启后调用）。"""
    _state.update(pid=None, ready=False, rva=None, reason="", checked=0.0)
    _tls.__dict__.clear()


# ---------------------------------------------------------------- UIA 查询


def _root():
    """当前线程的微信 UIA 根控件（每线程缓存一个 COM 对象）。"""
    if not UIA_ENABLED:
        return None
    hwnd, _ = _wechat_hwnd_pid()
    if not hwnd:
        return None
    cached = getattr(_tls, "root", None)
    if cached is not None and getattr(_tls, "hwnd", None) == hwnd:
        try:
            if cached.ClassName:
                return cached
        except Exception:
            pass
    try:
        import uiautomation as auto

        auto.InitializeUIAutomationInCurrentThread()
        auto.SetGlobalSearchTimeout(min(SEARCH_TIMEOUT, 1.0))
        root = auto.ControlFromHandle(hwnd)
    except Exception as e:
        logger.debug("[uia] ControlFromHandle 失败: %s", e)
        return None
    if root is None:
        return None
    _tls.root, _tls.hwnd = root, hwnd
    return root


def _find(timeout: float | None = None, **kw):
    """在微信窗口树里找一个控件；找不到/异常返回 None。

    timeout 越小轮询越轻量——控件不存在时 uiautomation 会一直找到超时为止，
    所以轮询判据（如浮层检测）要用短超时，否则每轮白等。
    """
    root = _root()
    if root is None:
        return None
    try:
        ctrl = root.Control(searchDepth=SEARCH_DEPTH, **kw)
        return ctrl if ctrl.Exists(SEARCH_TIMEOUT if timeout is None else timeout) else None
    except Exception as e:
        logger.debug("[uia] 查找 %s 失败: %s", kw, e)
        return None


def rect_of(ctrl) -> tuple[int, int, int, int] | None:
    try:
        r = ctrl.BoundingRectangle
        box = (int(r.left), int(r.top), int(r.right), int(r.bottom))
        return box if box[2] > box[0] and box[3] > box[1] else None
    except Exception:
        return None


def center_of(ctrl) -> tuple[int, int] | None:
    box = rect_of(ctrl)
    return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2) if box else None


def input_field_rect() -> tuple[int, int, int, int] | None:
    ctrl = _find(ClassName=CLASS_INPUT)
    return rect_of(ctrl) if ctrl else None


def voice_button_rect() -> tuple[int, int, int, int] | None:
    """语音按钮（'发语音 ( 按住右 Alt )'）矩形。"""
    ctrl = _find(ClassName=CLASS_VOICE_BTN, Name=NAME_VOICE_BTN)
    return rect_of(ctrl) if ctrl else None


def overlay_rect(timeout: float | None = None) -> tuple[int, int, int, int] | None:
    """录音浮层矩形；None = 当前没有浮层（或 UIA 不可用）。

    默认短超时：这个方法会被 _wait_record_overlay 每 0.25s 调一次。
    """
    ctrl = _find(timeout=0.25 if timeout is None else timeout, ClassName=CLASS_OVERLAY)
    return rect_of(ctrl) if ctrl else None


def overlay_exists() -> bool:
    return overlay_rect() is not None


def _overlay_child(name: str, class_name: str | None = None):
    """在浮层内部找按钮（限定在 ChatVoiceRecordView 子树，避免误命中输入条）。"""
    ov = _find(ClassName=CLASS_OVERLAY)
    if ov is None:
        return None
    try:
        kw = {"Name": name, "searchDepth": 8}
        if class_name:
            kw["ClassName"] = class_name
        ctrl = ov.Control(**kw)
        return ctrl if ctrl.Exists(SEARCH_TIMEOUT) else None
    except Exception:
        return None


def send_button_rect() -> tuple[int, int, int, int] | None:
    """浮层里的'发送语音'按钮矩形。"""
    ctrl = _overlay_child(NAME_SEND)
    return rect_of(ctrl) if ctrl else None


def cancel_button_rect() -> tuple[int, int, int, int] | None:
    """浮层里的'取消'按钮矩形。"""
    ctrl = _overlay_child(NAME_CANCEL)
    return rect_of(ctrl) if ctrl else None


def click_voice_button() -> bool:
    """用 UIA 直接点击语音按钮开始录音（不经输入队列，不受点击穿透影响）。"""
    ctrl = _find(ClassName=CLASS_VOICE_BTN, Name=NAME_VOICE_BTN)
    if ctrl is None:
        return False
    try:
        ctrl.Click()
        return True
    except Exception as e:
        logger.debug("[uia] 点击语音按钮失败: %s", e)
        return False


def voice_messages() -> list[str]:
    """聊天区里所有语音消息的名称（如 '语音15"秒'），按树序返回。"""
    root = _root()
    if root is None:
        return []
    out: list[str] = []
    seen = {"n": 0}

    def walk(ctrl, depth):
        if depth > SEARCH_DEPTH or seen["n"] > 3000:
            return
        seen["n"] += 1
        try:
            if (ctrl.ClassName or "") == CLASS_VOICE_MSG:
                out.append(ctrl.Name or "")
        except Exception:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for c in children:
            walk(c, depth + 1)

    try:
        walk(root, 0)
    except Exception as e:
        logger.debug("[uia] 遍历语音消息失败: %s", e)
    return out


def latest_voice_message() -> str | None:
    """最新一条语音消息的名称（聊天区最靠下的那条）。

    优先按屏幕矩形 top 最大者判定；万一控件读不到矩形（COM 抖动），回退到
    遍历顺序的最后一条——宁可降级也不要静默返回 None 让校验失效。
    """
    root = _root()
    if root is None:
        return None
    items: list[tuple[int | None, int, str]] = []
    seen = {"n": 0, "seq": 0}

    def walk(ctrl, depth):
        if depth > SEARCH_DEPTH or seen["n"] > 3000:
            return
        seen["n"] += 1
        try:
            if (ctrl.ClassName or "") == CLASS_VOICE_MSG:
                box = rect_of(ctrl)
                items.append((box[1] if box else None, seen["seq"], ctrl.Name or ""))
                seen["seq"] += 1
            children = ctrl.GetChildren()
        except Exception:
            return
        for c in children:
            walk(c, depth + 1)

    try:
        walk(root, 0)
    except Exception as e:
        logger.debug("[uia] 取最新语音消息失败: %s", e)
    if not items:
        return None
    with_top = [it for it in items if it[0] is not None]
    if with_top:
        return max(with_top, key=lambda it: it[0])[2]
    return items[-1][2]


def duration_from_message(name: str | None) -> float | None:
    """从 '语音15"秒' 里解析出秒数。"""
    if not name:
        return None
    m = re.search(r"(\d+)\s*[\"″]", str(name))
    return float(m.group(1)) if m else None


def status() -> dict:
    """诊断用：当前激活状态快照。"""
    return {
        "enabled": UIA_ENABLED,
        "ready": _state["ready"],
        "pid": _state["pid"],
        "rva": f"0x{_state['rva']:x}" if _state.get("rva") else None,
        "dll": _state.get("dll"),
        "reason": _state["reason"],
    }
