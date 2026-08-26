"""「袋鼠语音」一键能力：实时变声（RVC RealtimeVST）+ 训练触发 + 声卡自动切换/还原。

实时链路：
  麦克风阵列(真麦) → RVC 实时变声 → CABLE Input → CABLE Output → 微信/游戏等(录音= CABLE Output)
一键 start 会：
  1. 校验袋鼠模型就绪（logs/meituan_rat/meituan_rat.pth + index）
  2. 把 RVC 实时配置( configs/config.json )预填为袋鼠模型 + 麦克风阵列 → CABLE Input
  3. 调用 audio_config.ps1 apply：录音默认切到 CABLE Output（自动备份原设备）
  4. 后台拉起 realtime_gui.py（新控制台），并守护进程 —— 退出后自动还原声卡
"""
import json
import re
import subprocess
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

API_PREFIX = "/api"

ROOT = Path(__file__).resolve().parent.parent
RVC_ROOT = Path(r"D:\RVC")
CONFIG_JSON = RVC_ROOT / "configs" / "config.json"
REALTIME_PY = RVC_ROOT / "realtime_gui.py"
RUNTIME_PY = RVC_ROOT / "runtime" / "python.exe"
VENV_PY = RVC_ROOT / ".venv" / "Scripts" / "python.exe"
TRAIN_PY = RVC_ROOT / "train_meituan_rat.py"
LOG_DIR = RVC_ROOT / "logs" / "meituan_rat"
DATASET_DIR = RVC_ROOT / "dataset" / "meituan_rat"
AUDIO_PS1 = ROOT / "m2_server" / "audio_config.ps1"

# 真实麦克风与虚拟声卡（由 audio_config.ps1 list 探测确定）
INPUT_DEVICE = "麦克风阵列"
OUTPUT_DEVICE = "CABLE Input"

CREATE_NEW_CONSOLE = 0x00000010

_state = {
    "live": {"running": False, "pid": None, "audio_switched": False, "error": ""},
    "train": {"running": False, "pid": None},
}


def _audio(action: str):
    """调用 audio_config.ps1。apply 会首次自动备份原设备，restore 还原并删除备份。"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(AUDIO_PS1), "-action", action],
        capture_output=True, text=True, timeout=120,
    )


def _model_status() -> bool | str:
    pth = LOG_DIR / "meituan_rat.pth"
    idx = next(LOG_DIR.glob("added_*.index"), None) if LOG_DIR.exists() else None
    if not (LOG_DIR.exists() and pth.exists() and idx is not None):
        return "缺少 RVC 袋鼠模型（logs/meituan_rat/meituan_rat.pth 与 index），请先「训练袋鼠模型」"
    return True


def _kangaroo_config(keep_devices: bool) -> bool:
    """把 RVC 实时配置预填为袋鼠模型；keep_devices=True 时保留设备字段（首启校准后不再覆盖）。"""
    cfg = {}
    if CONFIG_JSON.exists():
        try:
            cfg = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    idx = next(LOG_DIR.glob("added_*.index"), None)
    if idx is None:
        return False
    cfg["pth_path"] = str(LOG_DIR / "meituan_rat.pth").replace("\\", "/")
    cfg["index_path"] = str(idx).replace("\\", "/")
    cfg["f0method"] = "rmvpe"
    cfg["sr_type"] = "sr_model"
    if not keep_devices or not cfg.get("sg_input_device"):
        cfg["sg_input_device"] = INPUT_DEVICE
    if not keep_devices or not cfg.get("sg_output_device"):
        cfg["sg_output_device"] = OUTPUT_DEVICE
    CONFIG_JSON.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return True


def _live_proc_alive() -> bool:
    pid = _state["live"]["pid"]
    if not pid:
        return False
    try:
        subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=30)
        # 简化为检查进程存在
        p = subprocess.run(["powershell", "-NoProfile", "-Command",
                            f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue) -ne $null"],
                           capture_output=True, text=True, timeout=30)
        return p.stdout.strip() == "True"
    except Exception:
        return False


def _live_waiter(proc: subprocess.Popen):
    proc.wait()
    try:
        _audio("restore")
    except Exception:
        pass
    _state["live"].update(running=False, pid=None, audio_switched=False)


router = APIRouter(prefix=API_PREFIX)


@router.get("/rvc/live/status")
def rvc_live_status():
    model = _model_status()
    idx = next(LOG_DIR.glob("added_*.index"), None) if LOG_DIR.exists() else None
    return {
        "ok": True,
        "model_ok": model is True,
        "model_detail": model if model is not True else None,
        "pth_exists": (LOG_DIR / "meituan_rat.pth").exists(),
        "index_exists": idx is not None,
        "dataset_count": len(list(DATASET_DIR.glob("*.wav"))) if DATASET_DIR.exists() else 0,
        "live_running": _live_proc_alive(),
        "audio_switched": _state["live"]["audio_switched"],
        "train_running": _state["train"]["running"],
        "output_device": OUTPUT_DEVICE,
    }


@router.post("/rvc/live/start")
def rvc_live_start():
    if _live_proc_alive():
        return JSONResponse({"ok": True, "already_running": True, "pid": _state["live"]["pid"]})
    model = _model_status()
    if model is not True:
        raise HTTPException(status_code=400, detail=model)
    if not RUNTIME_PY.exists():
        raise HTTPException(status_code=500, detail=f"未找到 RVC 运行时: {RUNTIME_PY}")
    if not _kangaroo_config(keep_devices=True):
        raise HTTPException(status_code=500, detail="袋鼠模型索引缺失")

    # 切声卡：录音默认 → CABLE Output（首次自动备份原设备）
    try:
        _audio("apply")
        _state["live"]["audio_switched"] = True
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"切换虚拟声卡失败: {e}")

    proc = subprocess.Popen(
        [str(RUNTIME_PY), "-I", str(REALTIME_PY)],
        cwd=str(RVC_ROOT), creationflags=CREATE_NEW_CONSOLE,
    )
    _state["live"].update(running=True, pid=proc.pid, error="")
    threading.Thread(target=_live_waiter, args=(proc,), daemon=True).start()
    return JSONResponse({
        "ok": True, "pid": proc.pid, "audio_switched": True,
        "output_device": OUTPUT_DEVICE,
        "hint": f"已把录音设备切到 {OUTPUT_DEVICE}，请在弹出的 RVC 窗口点「开始变声」；关闭窗口或点「停止变袋鼠音」会自动还原声卡",
    })


@router.post("/rvc/live/stop")
def rvc_live_stop():
    pid = _state["live"]["pid"]
    if pid:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
        except Exception:
            pass
        _state["live"].update(running=False, pid=None, audio_switched=False)
    try:
        _audio("restore")
        _state["live"]["audio_switched"] = False
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"还原声卡失败: {e}"})
    return JSONResponse({"ok": True, "restored": True})


@router.get("/rvc/train/status")
def rvc_train_status():
    model = _model_status()
    return {
        "ok": True,
        "model_ok": model is True,
        "model_detail": ("" if model is True else model),
        "train_running": _state["train"]["running"],
        "dataset_count": len(list(DATASET_DIR.glob("*.wav"))) if DATASET_DIR.exists() else 0,
        "log_dir": str(LOG_DIR),
    }


@router.post("/rvc/train/start")
def rvc_train_start():
    if _state["train"]["running"]:
        return JSONResponse({"ok": False, "already_running": True})
    if not VENV_PY.exists() or not TRAIN_PY.exists():
        raise HTTPException(status_code=500, detail="未找到 RVC 训练脚本")
    proc = subprocess.Popen(
        [str(VENV_PY), str(TRAIN_PY)],
        cwd=str(RVC_ROOT), creationflags=CREATE_NEW_CONSOLE,
    )

    def _waiter(p):
        p.wait()
        _state["train"].update(running=False, pid=None)

    _state["train"].update(running=True, pid=proc.pid)
    threading.Thread(target=_waiter, args=(proc,), daemon=True).start()
    return JSONResponse({"ok": True, "started": True, "pid": proc.pid,
                         "log_dir": str(LOG_DIR)})