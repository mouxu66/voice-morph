"""音频设备接口：一键最优配置 / 恢复 / 诊断看板 + 残留自动巡检（FRD F4）。

自 server.py 拆出（行为不变）；app 装配见 server.py。

背景：浏览器/前端无法直接修改 Windows 默认播放/录音设备，必须由后端走 PowerShell
调用 Core Audio (IPolicyConfig) 完成。脚本：m2_server/audio_config.ps1
  -action status  查看当前默认设备
  -action apply   一键设为变声最优配置（备份原始配置）
  -action restore 恢复用户原始默认设备（删除备份；无备份时回退到 reset）
  -action reset   强制恢复为真实扬声器/麦克风（兜底，无论有无备份都生效）
  -action diag    枚举全部音频端点（含状态/角色，排查用）
备份文件位于 %LOCALAPPDATA%/rvc_audio_backup.txt，首次 apply 时写入。
巡检只处理「有备份但无变声进程」这一种残留（绝不擅改用户手动设置）。
"""
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter

from runtime import API_PREFIX, ROOT

router = APIRouter(prefix=API_PREFIX)

_AUDIO_PS1 = ROOT / "m2_server" / "audio_config.ps1"


def _find_powershell() -> str | None:
    """优先用 pwsh（PowerShell 7），回退到 powershell（Windows 自带）。"""
    for name in ("pwsh", "powershell"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _run_audio_config(action: str) -> dict:
    exe = _find_powershell()
    if not exe:
        return {"ok": False, "error": "未找到 PowerShell，无法调整音频设备"}
    try:
        proc = subprocess.run(
            [exe, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(_AUDIO_PS1), "-action", action],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "音频配置脚本执行超时（30s）"}
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if not out:
        return {"ok": False, "error": err or "脚本无输出"}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": err or out}
    # 脚本明确失败（含逐项 errors）时，把明细合并到 error 便于前端展示
    if not data.get("ok"):
        errs = data.get("errors")
        if isinstance(errs, list) and errs:
            data["error"] = ", ".join(str(e) for e in errs)
    return data


@router.get("/audio/status")
def audio_status():
    """查看当前默认播放/录音设备（三个角色：0=Console 1=Multimedia 2=Communications）。"""
    return _run_audio_config("status")


@router.post("/audio/apply")
def audio_apply():
    """一键把音频设备调到变声最优：播放=真实扬声器，录音=CABLE Output。
    首次调用会备份用户原始默认设备，供后续 restore 使用。"""
    return _run_audio_config("apply")


@router.post("/audio/restore")
def audio_restore():
    """用完之后恢复用户原来的默认设备（删除备份）。无备份时为空操作。"""
    return _run_audio_config("restore")


# ---------------- 音频设备诊断还原看板（FRD F4） ----------------
# 实时/级联变声启动前的前置校验 + 异常残留的自动巡检还原。

_AUDIO_BACKUP = Path(os.environ.get("LOCALAPPDATA", "")) / "rvc_audio_backup.txt"
AUDIO_AUDIT_INTERVAL_S = float(os.environ.get("VM_AUDIO_AUDIT_S", "30"))

_AUDIO_AUDIT = {"auto_restored": [], "last_error": ""}
_audit_lock = threading.Lock()
_audit_started = False


# ---------------- 发送链路自检（A1，2026-09-09） ----------------
# 只读检查：虚拟声卡是否安装、默认录音/播放设备是否就位、有无备份残留。
# 不动任何设备；修复动作（一键最优/恢复默认）复用上面的 apply/restore。


def _name_has(name: object, kw: str) -> bool:
    return kw in str(name or "").lower()


def build_send_chain_report(diag: dict, stale: bool) -> dict:
    """把 audio_config.ps1 diag 的端点清单转成发送链路检查项（纯函数，可单测）。

    diag 形如 {"ok": true, "devices": [{"flow":0|1,"name":...,"state":...,"roles":[..]}]}
    flow 0=播放(render) 1=录音(capture)；roles 含 0 表示该设备是 Console 默认设备。
    返回 {"ok","all_ok","stale","items"}，items 与 /diagnose 同构（key/ok/warn/label/detail/hint）。
    """
    items: list[dict] = []
    devices = diag.get("devices") if isinstance(diag, dict) else None
    if not (isinstance(diag, dict) and diag.get("ok")) or devices is None:
        return {
            "ok": False, "all_ok": False, "stale": stale,
            "items": [{
                "key": "diag", "ok": False, "label": "音频设备枚举",
                "detail": str((diag or {}).get("error") or "未能枚举音频端点"),
                "hint": "确认本机 PowerShell 可用（pwsh 或 powershell），重启本应用后再试。",
            }],
        }

    renders = [d for d in devices if d.get("flow") == 0]
    captures = [d for d in devices if d.get("flow") == 1]
    has_cable_in = any(_name_has(d.get("name"), "cable input") for d in renders)
    has_cable_out = any(_name_has(d.get("name"), "cable output") for d in captures)

    # 1) 虚拟声卡是否安装（变声链路的物理前提）
    if has_cable_in and has_cable_out:
        items.append({
            "key": "cable", "ok": True, "label": "虚拟声卡（VB-CABLE）",
            "detail": "已安装：CABLE Input（播放侧）+ CABLE Output（录音侧）", "hint": "",
        })
    else:
        miss = []
        if not has_cable_in:
            miss.append("播放端 CABLE Input")
        if not has_cable_out:
            miss.append("录音端 CABLE Output")
        items.append({
            "key": "cable", "ok": False, "label": "虚拟声卡（VB-CABLE）",
            "detail": "未检测到 " + "、".join(miss),
            "hint": "到 VB-Audio 官网免费下载安装 Virtual Cable，装完重启本应用再检测。",
        })

    # 2) 默认录音设备是否已是变声声卡（微信/QQ/游戏「选哪个麦克风」的关键）
    default_cap = next((d for d in captures if 0 in (d.get("roles") or [])), None)
    if default_cap is None:
        items.append({
            "key": "default_capture", "ok": False, "label": "默认录音设备",
            "detail": "未找到当前默认录音设备",
            "hint": "检查 Windows「声音设置 → 输入」里是否选择了设备。",
        })
    elif has_cable_out and _name_has(default_cap.get("name"), "cable output"):
        items.append({
            "key": "default_capture", "ok": True, "label": "默认录音设备",
            "detail": f"{default_cap.get('name')}（= 变声声卡，默认已就位）", "hint": "",
        })
    else:
        items.append({
            "key": "default_capture", "ok": False, "label": "默认录音设备",
            "detail": f"当前默认录音是「{default_cap.get('name')}」，还不是变声声卡",
            "hint": "点「一键最优」把默认录音切到 CABLE Output；或在微信/QQ/游戏的麦克风设置里手动选「CABLE Output」。",
        })

    # 3) 默认播放设备（防"开了变声本机就听不到声音"）
    default_ren = next((d for d in renders if 0 in (d.get("roles") or [])), None)
    if default_ren is None:
        items.append({
            "key": "default_render", "ok": False, "warn": True, "label": "默认播放设备",
            "detail": "未找到当前默认播放设备",
            "hint": "检查 Windows「声音设置 → 输出」里是否选择了设备。",
        })
    elif has_cable_in and _name_has(default_ren.get("name"), "cable input"):
        items.append({
            "key": "default_render", "ok": False, "warn": True, "label": "默认播放设备",
            "detail": f"当前默认播放是「{default_ren.get('name')}」（虚拟声卡），本机听不到声音",
            "hint": "点「恢复默认设备」或手动把播放切回真实扬声器/耳机；实时变声时建议戴耳机防回声。",
        })
    else:
        items.append({
            "key": "default_render", "ok": True, "label": "默认播放设备",
            "detail": f"{default_ren.get('name')}（真实扬声器/耳机，正常）", "hint": "",
        })

    # 4) 备份残留（上次变声异常退出的告警，不阻断）
    if stale:
        items.append({
            "key": "stale_backup", "ok": False, "warn": True, "label": "设备配置残留",
            "detail": "上次变声的设备备份尚未还原（可能异常退出）",
            "hint": "点「恢复默认设备」一键还原；不处理也不影响下次变声（下次会重新备份）。",
        })

    return {"ok": True, "all_ok": all(i["ok"] for i in items), "stale": stale, "items": items}


@router.get("/audio/send_chain")
def audio_send_chain():
    """发送链路自检（只读）：枚举音频端点并判断变声发送链路是否就位。

    修复动作不复用本接口：一键最优 = POST /audio/apply，恢复默认 = POST /audio/restore。
    """
    diag = _run_audio_config("diag")
    return build_send_chain_report(diag if isinstance(diag, dict) else {}, _audio_stale())


def _backup_exists() -> bool:
    return _AUDIO_BACKUP.exists()


def _any_voice_alive() -> bool:
    from cascade import _cascade_alive
    from rvc_live import _live_proc_alive
    return bool(_cascade_alive() or _live_proc_alive())


def _audio_stale() -> bool:
    """有备份残留但无变声进程 = 异常残留，需要自动还原。"""
    return _backup_exists() and not _any_voice_alive()


@router.get("/audio/dashboard")
def audio_dashboard():
    """音频设备诊断看板：当前设备 + 是否残留异常 + 自动还原事件列表。"""
    status = _run_audio_config("status")
    with _audit_lock:
        return {
            "ok": True,
            "devices": status if isinstance(status, dict) else {},
            "backup_exists": _backup_exists(),
            "stale": _audio_stale(),
            "auto_restored": list(_AUDIO_AUDIT["auto_restored"]),
            "last_error": _AUDIO_AUDIT["last_error"],
        }


def _audit_once():
    """执行一次巡检：有残留则还原（restore 失败走 reset 兜底）。返回事件 dict 或 None。"""
    if not _audio_stale():
        return None
    data = _run_audio_config("restore")
    ok = bool(data.get("ok"))
    action = "restore"
    if not ok:
        data2 = _run_audio_config("reset")
        ok = bool(data2.get("ok"))
        action = "reset"
    event = {"ts": int(time.time()), "action": action, "result": "ok" if ok else "fail"}
    with _audit_lock:
        _AUDIO_AUDIT["auto_restored"].append(event)
        _AUDIO_AUDIT["auto_restored"] = _AUDIO_AUDIT["auto_restored"][-20:]
        _AUDIO_AUDIT["last_error"] = "" if ok else str(data.get("error") or "还原失败")
    return event


def _audio_audit_loop():
    """后台巡检：有备份残留且无变声进程时自动还原。"""
    while True:
        time.sleep(AUDIO_AUDIT_INTERVAL_S)
        try:
            _audit_once()
        except Exception as e:
            with _audit_lock:
                _AUDIO_AUDIT["last_error"] = str(e)


def _start_audio_audit():
    global _audit_started
    if _audit_started:
        return
    _audit_started = True
    threading.Thread(target=_audio_audit_loop, daemon=True).start()
