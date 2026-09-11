# -*- coding: utf-8 -*-
"""
Core Audio 默认设备探针 —— 回答一个问题:

    "此刻 Windows 三个角色上的默认录音设备, 到底是不是 CABLE Output?"

不用 Add-Type(沙箱禁), 改用 comtypes 直连 MMDeviceEnumerator + IPolicyConfig,
与 audio_config.ps1 走的是同一套未文档化接口, 因此可以复现它的实际行为。

用法:
    python tools/cable_watch.py --status          # 只读当前默认设备(无副作用)
    python tools/cable_watch.py --watch 30        # 只读监控 30 秒, 打印每次变化
    python tools/cable_watch.py --apply 30        # 先把三角色切到 CABLE Output, 再监控 30 秒,
                                                  # 结束后自动还原(危险操作, 默认需加 --yes)
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
import winreg
from ctypes import POINTER, byref, c_int, c_void_p, c_wchar_p

import comtypes
from comtypes import COMMETHOD, GUID, HRESULT, STDMETHOD, CoClass, IUnknown, CoCreateInstance

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ---------------- COM 接口 ----------------
CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
CLSID_PolicyConfigClient = GUID("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}")


class IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = (
        STDMETHOD(HRESULT, "Activate", [POINTER(GUID), c_int, c_void_p, POINTER(c_void_p)]),
        STDMETHOD(HRESULT, "OpenPropertyStore", [c_int, POINTER(c_void_p)]),
        STDMETHOD(HRESULT, "GetId", [POINTER(c_wchar_p)]),
        STDMETHOD(HRESULT, "GetState", [POINTER(c_int)]),
    )


class IMMDeviceCollection(IUnknown):
    _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
    _methods_ = (
        STDMETHOD(HRESULT, "GetCount", [POINTER(c_int)]),
        STDMETHOD(HRESULT, "Item", [c_int, POINTER(POINTER(IMMDevice))]),
    )


class IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = (
        COMMETHOD([], HRESULT, "EnumAudioEndpoints",
                  (["in"], c_int, "dataFlow"), (["in"], c_int, "stateMask"),
                  (["out"], POINTER(POINTER(IMMDeviceCollection)), "pp")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                  (["in"], c_int, "dataFlow"), (["in"], c_int, "role"),
                  (["out"], POINTER(POINTER(IMMDevice)), "pp")),
        COMMETHOD([], HRESULT, "GetDevice",
                  (["in"], c_wchar_p, "id"),
                  (["out"], POINTER(POINTER(IMMDevice)), "pp")),
        STDMETHOD(HRESULT, "RegisterEndpointNotificationCallback", [c_void_p]),
        STDMETHOD(HRESULT, "UnregisterEndpointNotificationCallback", [c_void_p]),
    )


class MMDeviceEnumerator(CoClass):
    _reg_clsid_ = CLSID_MMDeviceEnumerator
    _idlflags_ = []
    _com_interfaces_ = [IMMDeviceEnumerator]


class IPolicyConfig(IUnknown):
    _iid_ = GUID("{F8679F50-850A-41CF-9C72-430F290290C8}")
    _methods_ = (
        STDMETHOD(HRESULT, "GetMixFormat", [c_wchar_p, c_void_p]),
        STDMETHOD(HRESULT, "GetDeviceFormat", [c_wchar_p, c_void_p]),
        STDMETHOD(HRESULT, "ResetDeviceFormat", [c_wchar_p]),
        STDMETHOD(HRESULT, "SetDeviceFormat", [c_wchar_p, c_void_p]),
        STDMETHOD(HRESULT, "GetProcessingPeriod", [c_wchar_p, c_void_p, c_void_p]),
        STDMETHOD(HRESULT, "SetProcessingPeriod", [c_wchar_p, c_void_p]),
        STDMETHOD(HRESULT, "GetShareMode", [c_wchar_p, c_void_p]),
        STDMETHOD(HRESULT, "SetShareMode", [c_wchar_p, c_void_p]),
        STDMETHOD(HRESULT, "GetPropertyValue", [c_wchar_p, c_void_p, c_void_p]),
        STDMETHOD(HRESULT, "SetPropertyValue", [c_wchar_p, c_void_p, c_void_p]),
        STDMETHOD(HRESULT, "SetDefaultEndpoint", [c_wchar_p, c_int]),
    )


class PolicyConfigClient(CoClass):
    _reg_clsid_ = CLSID_PolicyConfigClient
    _idlflags_ = []
    _com_interfaces_ = [IPolicyConfig]


# flow: 0=render 1=capture ;  role: 0=Console 1=Multimedia 2=Communications
ROLE_NAMES = {0: "Console", 1: "Multimedia", 2: "Communications"}
PKEY_DESC = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
PKEY_PROV = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"


def _enum(role: int = -1) -> object:
    comtypes.CoInitialize()
    return CoCreateInstance(CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator)


def _id_of(dev: POINTER(IMMDevice)) -> str:
    p = c_wchar_p()
    dev.GetId(byref(p))      # STDMETHOD: out 参数需显式传
    s = p.value
    ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(p, ctypes.c_void_p))
    return s


def name_of(device_id: str) -> str | None:
    """从注册表还原设备显示名, 与声音面板一致 (描述 + 提供方)。"""
    if not device_id:
        return None
    i = device_id.find("{", 1)
    if i < 0:
        return None
    guid = device_id[i:]
    flow_key = "Capture" if ".0.1." in device_id else "Render"
    path = (f"SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\MMDevices\\"
            f"Audio\\{flow_key}\\{guid}\\Properties")
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            desc = winreg.QueryValueEx(k, PKEY_DESC)[0]
            try:
                prov = winreg.QueryValueEx(k, PKEY_PROV)[0]
            except OSError:
                prov = ""
            if prov and prov not in desc:
                return f"{desc} ({prov.rstrip()})"
            return desc
    except OSError:
        return None


def snapshot(en: IMMDeviceEnumerator) -> dict[str, str | None]:
    out = {}
    for flow, tag in ((0, "render"), (1, "capture")):
        for role in (0, 1, 2):
            try:
                dev = en.GetDefaultAudioEndpoint(flow, role)
                out[f"{tag}{role}"] = name_of(_id_of(dev)) if dev else "<null>"
            except Exception as e:
                out[f"{tag}{role}"] = f"<err {type(e).__name__}: {e}>"
    return out


def find_endpoint(en: IMMDeviceEnumerator, flow: int, keyword: str) -> str | None:
    coll = en.EnumAudioEndpoints(flow, 1)      # COMMETHOD: 返回 collection
    cnt = c_int()
    coll.GetCount(byref(cnt))                  # STDMETHOD: 显式传
    for i in range(cnt.value):
        dev = POINTER(IMMDevice)()
        coll.Item(i, byref(dev))               # STDMETHOD: 显式传
        did = _id_of(dev)
        nm = name_of(did) or ""
        if keyword.lower() in nm.lower():
            return did
    return None


def set_default(device_id: str, roles=(0, 1, 2)) -> list[str]:
    pc = CoCreateInstance(CLSID_PolicyConfigClient, interface=IPolicyConfig)
    errs = []
    for r in roles:
        hr = pc.SetDefaultEndpoint(device_id, r)
        if hr < 0:
            errs.append(f"role{r}:hr={hr}")
    return errs


def print_snapshot(sn: dict, label: str = "") -> str:
    key = f"{label}|" if label else ""
    cap = " | ".join(f"{ROLE_NAMES[r]}={sn[f'capture{r}']}" for r in (0, 1, 2))
    line = f"{key}CAPTURE {cap}"
    print(line)
    return line


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--watch", type=float, default=0)
    ap.add_argument("--apply", type=float, default=0, help="切到 CABLE Output 并监控 N 秒")
    ap.add_argument("--yes", action="store_true", help="确认允许修改系统默认设备")
    args = ap.parse_args()

    en = _enum()
    if not (args.watch or args.apply):
        args.status = True
    if args.status:
        print_snapshot(snapshot(en), "T+0.0s")

    total = args.watch or args.apply
    if args.apply:
        target = find_endpoint(en, 1, "CABLE Output")
        if not target:
            print("找不到 CABLE Output 采集端点")
            return 1
        print(f"\n目标: {name_of(target)}")
        if not args.yes:
            print("这是修改系统默认设备的操作。加 --yes 才会执行。")
            return 0
        errs = set_default(target)
        print(f"apply errors: {errs or 'none'}")
        try:
            watch(en, total, target)
        finally:
            # 还原到真实设备
            real = None
            for kw in ("Senary", "麦克风阵列"):
                real = find_endpoint(en, 1, kw)
                if real:
                    break
            if real:
                set_default(real)
                time.sleep(0.3)
                print(f"\n已还原默认录音设备 -> {name_of(real)}")
            print_snapshot(snapshot(en), "final")
    elif total:
        watch(en, total, None)
    return 0


def watch(en: IMMDeviceEnumerator, seconds: float, target: str | None) -> None:
    print(f"\n监控 {seconds:.0f} 秒, 只在变化时打印…\n")
    t0 = time.time()
    prev = None
    while time.time() - t0 < seconds:
        sn = snapshot(en)
        if sn != prev:
            print_snapshot(sn, f"T+{time.time()-t0:.1f}s")
            prev = sn
        time.sleep(0.5)
    print()


if __name__ == "__main__":
    sys.exit(main())
