"""真声直讲（真声直通 CABLE）—— 让老师**用真声讲课**的同时，能按热键插一句 AI 音。

场景（`docs/真声直讲设计.md`）
------------------------------
老师用电脑播 PPT、用电脑麦克风讲课：**平时是本人真声**，讲累了按一下
`Ctrl+Alt+1~5`，AI 音色替他说一句 —— 学生那头听到的是**连续混音**，
不切换、不打断。

现状缺口：真声没有任何"原样进 CABLE"的通路
------------------------------------------
    sound.rvc-live  真麦 → RVC → CABLE   ← 真声被**替换**成变声音色
    cascade         真麦 → ASR → TTS → CABLE ← 同样替换，且要等 1.5~2s
    热键            只播那一下，真声不在里面

全项目搜过 passthrough / dry / wet / 直通 / 原声 —— **一个实现都没有**。
所以本模块补的就是这条通路。

分工
----
    passthrough_worker.py  子进程：真麦 → OutputStream → CABLE（含插话叠加）
    本模块                 生命周期（spawn/ensure/command/stop）+ 路由

和 `soundboard.py` 的关系
------------------------
**不 import `soundboard`**（同 `soundboard.py:9` 的理由：保持叶子、不互相拖坑）。
设备关键字与 RVC venv 解释器各自读同一组环境变量，语义一致而无导入依赖。

但**热键的分流要经过两边**：见 `voice_phrases.play_phrase` —— 直通开着时走本模块
（叠加），没开时走 soundboard（现状）。这样不开直通时行为**零变化**。

★ 为什么必须自持 OutputStream（不是 sd.play）
--------------------------------------------
`sd.play` 是全局单例，起第二路会 stop 掉第一路 —— 那样一按热键就会把老师的
真声**整个掐断**。详见 `passthrough_worker.py` 文件头。

互斥
----
本模块与 `rvc_live` / `cascade` **互斥**：三者都要占 CABLE 出口。
照既有口径（`cascade.py:145`）：启动时检查，冲突返回 409。

环境开关
--------
`VM_PASSTHROUGH=0` → 拒启（测试隔离惯例，与 `VM_SOUNDBOARD=0` 同套，conftest 会设）。
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import threading
from pathlib import Path

import config as cfg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/passthrough", tags=["passthrough"])

RVC_VENV_PY = cfg.RVC_ROOT / ".venv" / "Scripts" / "python.exe"
PASSTHROUGH_WORKER = Path(__file__).resolve().parent / "passthrough_worker.py"

#: 与 wechat_voice / soundboard / board_worker 同源同义（见文件头：不 import 它们）
DEVICE_KEYWORD = os.environ.get("VM_LIVE_OUTPUT_DEVICE", "VB-Audio Virtual Cable|CABLE Input")

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_LOCK = threading.Lock()  # 起/停 worker
_CMD_LOCK = threading.Lock()  # 命令串行（行协议：一条命令一行响应）
_proc: subprocess.Popen | None = None

#: worker 回传的设备名（status 里用，让界面能显示"声音正送往哪"）
_DEVICES: dict[str, str] = {}


def _default_input_name() -> str:
    """系统当前默认录音设备名。

    在**启动那一刻**现读，这样老师插了耳机 / 换了麦，重新开一次就能跟上
    （同 `rvc_live._system_default_input` 的既有做法）。
    读不到就返回空串 —— worker 会退回自己的默认设备解析，不让整条链路起不来。
    """
    try:
        out = subprocess.run(
            [
                str(RVC_VENV_PY),
                "-c",
                "import sounddevice as sd, json\n"
                "print(json.dumps(sd.query_devices(kind='input')['name']))",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(cfg.RVC_ROOT),
        ).stdout
        return json.loads(out.strip().splitlines()[-1])
    except Exception:
        return ""


def _spawn_worker() -> subprocess.Popen:
    """启动常驻直通 worker（懒启动，成功后复用）。

    行协议与生命周期与 `soundboard._spawn_worker` 同构 —— 那套已经踩过
    "启动失败要回收进程""ready 前的行不能当响应"两个坑，照抄不重复踩。
    """
    if os.environ.get("VM_PASSTHROUGH", "1") == "0":
        raise RuntimeError("真声直讲已被 VM_PASSTHROUGH=0 禁用（测试环境）")
    if not RVC_VENV_PY.exists():
        raise RuntimeError(f"找不到 RVC venv 解释器: {RVC_VENV_PY}（sounddevice 在该环境）")
    if not PASSTHROUGH_WORKER.exists():
        raise RuntimeError(f"找不到直通 worker: {PASSTHROUGH_WORKER}")
    proc = subprocess.Popen(
        [
            str(RVC_VENV_PY),
            str(PASSTHROUGH_WORKER),
            DEVICE_KEYWORD,
            _default_input_name(),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
        creationflags=_NO_WINDOW,  # 弹控制台会遮挡 PPT / 通话窗口
    )
    line = proc.stdout.readline() if proc.stdout else ""
    try:
        msg = json.loads(line or "{}")
    except Exception:
        msg = {}
    if msg.get("type") != "ready":
        detail = msg.get("msg") or (line or "").strip()[:200] or "无输出"
        with contextlib.suppress(Exception):
            proc.kill()
        raise RuntimeError(f"真声直通未就绪：{detail}")
    _DEVICES["out"] = str(msg.get("out_device") or "")
    _DEVICES["in"] = str(msg.get("in_device") or "")
    return proc


def _ensure_worker() -> subprocess.Popen:
    global _proc
    with _LOCK:
        if _proc is not None and _proc.poll() is None:
            return _proc
        if _proc is not None:  # 死掉的残留：关掉管道再重开
            with contextlib.suppress(Exception):
                _proc.kill()
        _proc = _spawn_worker()
        return _proc


def _command(payload: dict) -> dict:
    """发一条命令并读**恰好一行**响应（行协议不变量：一命令一响应）。

    读不到行（空串）= worker 已退出（管道关闭）—— 与 soundboard._command 同判据：
    **不要把"管道关了"和"响应内容不对"混成一类**（前者要重启，后者是逻辑问题）。
    """
    with _CMD_LOCK:
        proc = _ensure_worker()
        try:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline() if proc.stdout else ""
        except Exception as e:
            raise RuntimeError(f"真声直通命令发送失败：{e}") from e
    if not line:
        raise RuntimeError("真声直通 worker 已退出（管道关闭）")
    try:
        msg = json.loads(line)
    except Exception:
        raise RuntimeError(f"真声直通响应无法解析：{line.strip()[:200]}")
    if msg.get("type") == "error":
        raise RuntimeError(str(msg.get("msg") or "未知错误"))
    return msg


def _stop_worker() -> None:
    global _proc
    with _LOCK:
        proc, _proc = _proc, None
    _DEVICES.clear()
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()  # 触发 worker 的 finally：关流、释放设备
        proc.kill()
    except Exception:
        pass


def is_running() -> bool:
    """直通是否正在跑（供热键分流与互斥检查用）。"""
    with _LOCK:
        return _proc is not None and _proc.poll() is None


def play_overlay(wav_path: str, clip_id: str, gain: float = 0.9) -> dict:
    """把一条 wav **叠加**到正在直通的真声上（热键插话用）。

    与 `soundboard._command` 的 `sd.play` 路径的区别：这条**不会掐断真声**。
    """
    return _command({"wav": str(wav_path), "id": clip_id, "gain": gain})


# --------------------------------------------------------------- 路由

#: 与 rvc_live / cascade 的互斥提示（三者都占 CABLE 出口）
_CONFLICT_HINT = "两者都占虚拟声卡（CABLE），请先停止另一个再开这个"


class StartBody(BaseModel):
    """启动参数。目前无必填项（设备由 worker 自己按系统默认解析）。"""


@router.post("/start")
def start(_: StartBody | None = None):
    """开启真声直讲：麦克风原样 → CABLE。"""
    if is_running():
        return {"ok": True, "already_running": True, "device": _DEVICES.get("out", "")}

    # 互斥：rvc_live / cascade 任一在跑就不能开（反向检查在它们各自的 start 里）
    from rvc_live import _live_proc_alive

    if _live_proc_alive():
        raise HTTPException(status_code=409, detail=f"实时变声正在运行，{_CONFLICT_HINT}")
    try:
        import cascade

        if cascade._cascade_alive():
            raise HTTPException(status_code=409, detail=f"级联变声正在运行，{_CONFLICT_HINT}")
    except ImportError:
        pass

    try:
        _ensure_worker()
    except (RuntimeError, OSError) as e:
        # 与声板 /play 同口径：Popen 失败与"设备找不到"都是"现在开不了"
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"ok": True, "running": True, "device": _DEVICES.get("out", "")}


@router.post("/stop")
def stop():
    """关闭真声直讲（worker 退出时自动关流、释放设备）。"""
    _stop_worker()
    return {"ok": True, "running": False}


@router.get("/status")
def status():
    """运行状态 + 设备名（界面用来显示"真声正送往哪"）。"""
    running = is_running()
    return {
        "ok": True,
        "running": running,
        "device": _DEVICES.get("out", "") if running else "",
        "input_device": _DEVICES.get("in", "") if running else "",
    }
