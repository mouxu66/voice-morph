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

import config as cfg
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
