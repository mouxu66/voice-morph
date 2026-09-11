"""UIA 热激活可行性探测（只读为主，写入需显式 --activate）。

背景：微信 4.x 聊天区自绘渲染，UIAutomation 默认只能拿到 Qt 空壳窗口。
社区库 wechatauto-replica 的做法：把 Weixin.dll 里 Qt accessibility gate 的
一个 active byte 从 0 改成 1，UIA 树立刻物化为 mmui::MainWindow。

本脚本按「风险从低到高」分三步，默认只跑前两步：
  1) --scan-only  只读 Weixin.dll 文件，PE 解析 + 字节模式扫描，找候选 RVA（零风险）
  2) 默认         额外 OpenProcess(只读) 读该 byte 当前值，验证地址有效（低风险）
  3) --activate   把该 byte 写成 1（**会修改微信进程内存**，需显式传入）

用法：
  python tools/uia_probe.py                 # 扫描 + 只读验证
  python tools/uia_probe.py --scan-only     # 完全不碰微信进程
  python tools/uia_probe.py --activate      # 热激活（写内存，谨慎）
"""
from __future__ import annotations

import argparse
import ctypes
import re
import struct
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "m2_server"))

QACCESSIBLE_CORE_STRING = b"qt.accessibility.core"
QACCESSIBLE_GATE_PATTERN = re.compile(
    rb"\x48\x85\xc9\x0f\x84....\x80\x3d(?P<disp>.{4})"
    rb"\x00\x0f\x84",
    re.DOTALL,
)
IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_WRITE = 0x80000000
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_MODULE_NAME32 = 255

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008


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


# --------------------------------------------------------------------- PE 解析

def pe_sections(data: bytes):
    try:
        pe_off = struct.unpack_from("<I", data, 0x3C)[0]
        if data[pe_off:pe_off + 4] != b"PE\0\0":
            return []
        coff = pe_off + 4
        count = struct.unpack_from("<H", data, coff + 2)[0]
        opt_size = struct.unpack_from("<H", data, coff + 16)[0]
        sec_off = coff + 20 + opt_size
        out = []
        for i in range(count):
            off = sec_off + i * 40
            name = data[off:off + 8].split(b"\0", 1)[0].decode("ascii", "ignore")
            vsize, rva, raw_size, raw_ptr = struct.unpack_from("<IIII", data, off + 8)
            chars = struct.unpack_from("<I", data, off + 36)[0]
            out.append({"name": name, "rva": rva, "vsize": vsize,
                        "raw_size": raw_size, "raw_ptr": raw_ptr, "chars": chars})
        return out
    except Exception:
        return []


def section_for_rva(sections, rva):
    for sec in sections:
        size = max(sec["vsize"], sec["raw_size"])
        if sec["rva"] <= rva < sec["rva"] + size:
            return sec
    return None


def offset_to_rva(sections, offset):
    for sec in sections:
        if sec["raw_ptr"] <= offset < sec["raw_ptr"] + sec["raw_size"]:
            return sec["rva"] + offset - sec["raw_ptr"]
    return None


def rip_xrefs_to_rva(data: bytes, sections, target_rva):
    xrefs = []
    for sec in sections:
        if not (sec["chars"] & IMAGE_SCN_MEM_EXECUTE):
            continue
        start = sec["raw_ptr"]
        end = min(len(data), start + sec["raw_size"])
        raw = data[start:end]
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


def scan_gate_rva(dll_path: Path, verbose=True):
    """返回 (最佳候选 RVA, 候选列表[(距离, rva)])；扫描失败返回 (None, [])。"""
    data = dll_path.read_bytes()
    sections = pe_sections(data)
    if not sections:
        if verbose:
            print("  ! PE 解析失败")
        return None, []

    core_off = data.find(QACCESSIBLE_CORE_STRING)
    core_rva = offset_to_rva(sections, core_off) if core_off >= 0 else None
    core_xrefs = rip_xrefs_to_rva(data, sections, core_rva) if core_rva is not None else []
    if verbose:
        print(f"  qt.accessibility.core 字符串 @ file+0x{core_off:x}"
              f"  RVA={'0x%x' % core_rva if core_rva else 'None'}"
              f"  代码交叉引用 {len(core_xrefs)} 处")

    candidates = []
    for m in QACCESSIBLE_GATE_PATTERN.finditer(data):
        match_rva = offset_to_rva(sections, m.start())
        disp_rva = offset_to_rva(sections, m.start("disp"))
        if match_rva is None or disp_rva is None:
            continue
        match_sec = section_for_rva(sections, match_rva)
        if not match_sec or not (match_sec["chars"] & IMAGE_SCN_MEM_EXECUTE):
            continue
        cmp_rva = disp_rva - 2
        disp = struct.unpack("<i", m.group("disp"))[0]
        target_rva = cmp_rva + 7 + disp
        target_sec = section_for_rva(sections, target_rva)
        if not target_sec or not (target_sec["chars"] & IMAGE_SCN_MEM_WRITE):
            continue
        if core_xrefs:
            distance = min(abs(match_rva - x) for x in core_xrefs)
            if distance > 0x20000:
                continue
        else:
            distance = 0x7FFFFFFF
        candidates.append((distance, target_rva))

    if not candidates:
        return None, []
    candidates.sort(key=lambda it: it[0])
    return candidates[0][1], candidates


# ------------------------------------------------------------------ 进程与模块

def find_wechat_pid():
    """通过微信主窗口拿 PID。"""
    try:
        import wechat_voice as wv
        hwnd = wv._find_wechat_hwnd()
        if not hwnd:
            return None, None
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value, hwnd
    except Exception as e:
        print(f"  ! 找不到微信窗口: {e}")
        return None, None


def weixin_dll_module(pid):
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    k32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE or not snap:
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


def read_byte(pid, address):
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None, ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else -1
    try:
        buf = (ctypes.c_ubyte * 1)()
        read = ctypes.c_size_t(0)
        ok = k32.ReadProcessMemory(h, ctypes.c_void_p(address), buf, 1, ctypes.byref(read))
        return (int(buf[0]) if ok and read.value == 1 else None), 0
    finally:
        k32.CloseHandle(h)


def write_byte(pid, address, value):
    k32 = ctypes.windll.kernel32
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ |
                        PROCESS_VM_WRITE | PROCESS_VM_OPERATION, False, pid)
    if not h:
        return False
    try:
        buf = (ctypes.c_ubyte * 1)(value & 0xFF)
        written = ctypes.c_size_t(0)
        ok = k32.WriteProcessMemory(h, ctypes.c_void_p(address), buf, 1,
                                    ctypes.byref(written))
        return bool(ok and written.value == 1)
    finally:
        k32.CloseHandle(h)


def dump_uia_tree(hwnd, max_depth=4, max_nodes=500, with_rect=False):
    """用 UIA 从窗口句柄出发打印控件树（判断是否已物化为 mmui 树）。"""
    import uiautomation as auto
    try:
        auto.InitializeUIAutomationInCurrentThread()
    except Exception:
        pass
    try:
        auto.SetGlobalSearchTimeout(3.0)
    except Exception:
        pass
    counts = {"n": 0}

    def rect_of(ctrl):
        if not with_rect:
            return ""
        try:
            r = ctrl.BoundingRectangle
            return f"  [{r.left},{r.top},{r.right},{r.bottom}]"
        except Exception:
            return "  [rect?]"

    def walk(ctrl, depth):
        if depth > max_depth or counts["n"] > max_nodes:
            return
        try:
            info = f"{ctrl.ControlTypeName} | {ctrl.ClassName} | {ctrl.Name!r}"
        except Exception:
            return
        counts["n"] += 1
        print("    " * depth + "- " + info + rect_of(ctrl))
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for c in children:
            walk(c, depth + 1)

    root = auto.ControlFromHandle(hwnd)
    if root is None:
        print("  ControlFromHandle 返回 None（UIA 完全不可用）")
        return
    try:
        print(f"  root: {root.ControlTypeName} | {root.ClassName} | {root.Name!r}"
              + rect_of(root))
        for c in root.GetChildren():
            walk(c, 1)
    except Exception as e:
        print(f"  ! 遍历失败: {type(e).__name__}: {e}")
    print(f"  合计 {counts['n']} 个节点（max_depth={max_depth}）")


def find_controls(hwnd, keywords, max_depth=25):
    """全树搜索 ClassName/Name 含任一关键词的控件，打印路径 + 矩形。"""
    import uiautomation as auto
    try:
        auto.InitializeUIAutomationInCurrentThread()
    except Exception:
        pass
    try:
        auto.SetGlobalSearchTimeout(3.0)
    except Exception:
        pass
    hits = []
    seen = {"n": 0}
    kws = [k.lower() for k in keywords]

    def walk(ctrl, path, depth):
        if depth > max_depth or seen["n"] > 4000:
            return
        seen["n"] += 1
        try:
            cn = ctrl.ClassName or ""
            nm = ctrl.Name or ""
            ct = ctrl.ControlTypeName or ""
        except Exception:
            return
        blob = f"{cn} {nm} {ct}".lower()
        if any(k in blob for k in kws):
            try:
                r = ctrl.BoundingRectangle
                box = (r.left, r.top, r.right, r.bottom)
            except Exception:
                box = None
            hits.append((path, cn, nm, ct, box))
        try:
            children = ctrl.GetChildren()
        except Exception:
            children = []
        for c in children:
            walk(c, path + [f"{cn or ct}"], depth + 1)

    root = auto.ControlFromHandle(hwnd)
    if root is None:
        print("  ControlFromHandle 返回 None")
        return
    walk(root, [], 0)
    print(f"  遍历 {seen['n']} 个节点，命中 {len(hits)} 个：")
    for path, cn, nm, ct, box in hits:
        loc = f"[{box[0]},{box[1]},{box[2]},{box[3]}]" if box else "[无矩形]"
        depth_str = " > ".join(path[-4:])
        print(f"    - {ct} | {cn} | {nm!r}  {loc}")
        if depth_str:
            print(f"        路径: {depth_str}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan-only", action="store_true", help="只读 dll 文件，不碰微信进程")
    ap.add_argument("--activate", action="store_true", help="写内存热激活（谨慎）")
    ap.add_argument("--tree", nargs="?", type=int, const=4, default=None,
                    metavar="DEPTH", help="打印微信 UIA 控件树（可指定深度，默认 4）")
    ap.add_argument("--rect", action="store_true", help="树/搜索输出附带屏幕矩形")
    ap.add_argument("--find", default=None,
                    metavar="KW", help="全树搜索关键词（逗号分隔），如 voice,mic,input")
    a = ap.parse_args()

    print("=" * 68)
    print("UIA 热激活可行性探测")
    print("=" * 68)

    pid, hwnd = find_wechat_pid()
    print(f"\n[1] 微信进程：PID={pid} hwnd={hwnd}")
    if not pid:
        print("  微信未运行，退出。")
        return 1

    mod = weixin_dll_module(pid)
    if not mod:
        print("  ! 未在微信进程里找到 Weixin.dll —— 可能是 3.9 版（WeChat.exe）")
        return 1
    base, size, dll_path = mod
    print(f"  Weixin.dll 基址=0x{base:x}  大小={size/1024/1024:.1f}MB")
    print(f"  路径={dll_path}")

    ver_dir = Path(dll_path).parent.name
    print(f"  版本目录名={ver_dir}")

    print("\n[2] 扫描 Qt accessibility gate（只读 dll 文件）")
    rva, cands = scan_gate_rva(Path(dll_path))
    if rva is None:
        print("  ✗ 未找到候选 RVA —— 此微信版本不支持热激活（社区库也依赖版本匹配）")
        return 2
    print(f"  ✓ 最佳候选 RVA=0x{rva:x}（共 {len(cands)} 个候选）")
    for dist, r in cands[:5]:
        print(f"      RVA=0x{r:x}  与 qt.accessibility.core 代码距离=0x{dist:x}")

    if a.scan_only:
        print("\n[--scan-only] 到此为止，未接触微信进程。")
        return 0

    if a.tree is not None:
        print(f"\n[3b] UIA 控件树（激活前，深度 {a.tree}）")
        dump_uia_tree(hwnd, max_depth=a.tree, with_rect=a.rect)

    print(f"\n[3] 只读验证：目标地址 = 基址 + RVA = 0x{base + rva:x}")
    val, _ = read_byte(pid, base + rva)
    if val is None:
        print("  ✗ OpenProcess/ReadProcessMemory 失败——权限不足（需与微信同权限用户）")
        return 3
    print(f"  ✓ 当前 active byte = {val}  （0=UIA 只有 Qt 空壳，1=已激活）")

    if val == 1:
        print("  已经是 1（已激活），跳过写入。")
    elif not a.activate:
        print("\n[未写内存] 如需热激活请显式加 --activate")
        return 0
    else:
        print(f"\n[4] 热激活：写入 1 到 0x{base + rva:x}（仅 1 字节，Qt accessibility 开关）")
        ok = write_byte(pid, base + rva, 1)
        if not ok:
            print("  ✗ 写入失败")
            return 4
        val2, _ = read_byte(pid, base + rva)
        print(f"  ✓ 已写入，回读 = {val2}")

    if a.tree is not None:
        time.sleep(0.8)
        print(f"\n[5] UIA 控件树（激活后，深度 {a.tree}）")
        dump_uia_tree(hwnd, max_depth=a.tree, with_rect=a.rect)

    if a.find:
        kws = [k.strip() for k in a.find.split(",") if k.strip()]
        print(f"\n[6] 关键词搜索：{kws}")
        find_controls(hwnd, kws)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
