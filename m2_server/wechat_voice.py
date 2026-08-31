"""微信语音消息发送（桌宠右键入口）。

思路（全程不 Hook、不注入微信，只模拟人手操作）：
    1. audio_config.ps1 apply  —— 系统默认麦克风切到 CABLE Output（自动备份原设备）
    2. 前台化微信聊天窗口，模拟 Ctrl+Win 触发微信 4.1.7+ 的官方「语音输入」开始录音
       （微信录音从系统默认麦克风采集，此刻即 CABLE Output）
    3. 用 RVC venv 的 sounddevice 把 TTS 产物 wav 直接播进 CABLE Input
       （微信同步从 CABLE Output 录到的就是这段声音），首尾各垫静音防掐头去尾
    4. 模拟 Enter 结束录音 → 微信把录到的内容作为语音消息发出
    5. audio_config.ps1 restore —— 还原原声卡

依赖：主环境零新增（ctypes + subprocess）；播放走 D:/RVC/.venv 的 sounddevice，
与 cascade_stream/offline_vc 同一约定。

已知不确定性（首次使用需实测）：
    - Ctrl+Win 快捷键须是微信里生效的「语音输入」热键（可在微信设置→快捷键改，代码用默认值）
    - 录音结束靠 Enter；若微信版本把结束/发送绑在其他控件上，调 VM_WECHAT_FINISH
"""

import json
import os
import subprocess
import threading
import time
import wave
from pathlib import Path

import config as cfg
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/wechat", tags=["wechat"])

AUDIO_PS1 = cfg.ROOT / "m2_server" / "audio_config.ps1"
RVC_VENV_PY = cfg.RVC_ROOT / ".venv" / "Scripts" / "python.exe"
# 输出目标（cascade_stream 同款关键词）：wav 播进 CABLE Input，微信从 CABLE Output 录
OUTPUT_DEVICE_KEYWORD = os.environ.get("VM_LIVE_OUTPUT_DEVICE", "CABLE Input")
# 录音结束方式：enter=回车结束并发送（微信 4.1.x 实测默认交互）
FINISH_KEY = os.environ.get("VM_WECHAT_FINISH", "enter")
# 触发语音输入的全局热键：Ctrl+Win（微信 4.1.7+ 默认，可在微信设置里改）
LEAD_S = float(os.environ.get("VM_WECHAT_LEAD_S", "0.35"))   # 录音触发后等待
TAIL_S = float(os.environ.get("VM_WECHAT_TAIL_S", "0.5"))    # 播完后的尾巴静音

_send_lock = threading.Lock()


# ---------------- audio_config.ps1（复用 rvc_live 的调用方式） ----------------

def _run_audio(action: str) -> dict:
    if not AUDIO_PS1.exists():
        raise RuntimeError(f"缺 audio_config.ps1: {AUDIO_PS1}")
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(AUDIO_PS1), "-action", action],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"audio_config {action} 执行超时(120s)")
    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip()
        raise RuntimeError(f"audio_config {action} 无输出: {err[:1500]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"audio_config {action} 输出不是 JSON: {out[:300]}")
    if not data.get("ok"):
        raise RuntimeError(f"audio_config {action} 失败: {json.dumps(data, ensure_ascii=False)[:500]}")
    return data


# ---------------- 播放 wav → CABLE Input（RVC venv 子进程，唯一有 sounddevice） ----------------

_PLAY_SCRIPT = r'''
import sys
import numpy as np
import sounddevice as sd
import soundfile as sf

keyword = sys.argv[2].lower()
apis = sd.query_hostapis()
mme = next((i for i, a in enumerate(apis) if a["name"] == "MME"), None)
idx = None
for i, d in enumerate(sd.query_devices()):
    if d["hostapi"] == mme and d["max_output_channels"] > 0 and keyword in d["name"].lower():
        idx = i
        break
if idx is None:
    raise SystemExit(f"device_not_found:{keyword}")
data, sr = sf.read(sys.argv[1], dtype="float32")
if data.ndim > 1:
    data = data.mean(axis=1)
lead = np.zeros(int(sr * float(sys.argv[3])), dtype="float32")
tail = np.zeros(int(sr * float(sys.argv[4])), dtype="float32")
sd.play(np.concatenate([lead, data, tail]), sr, device=idx)
sd.wait()
'''


def _play_to_cable(wav: Path, duration_s: float) -> None:
    if not RVC_VENV_PY.exists():
        raise RuntimeError(f"找不到 RVC venv 解释器: {RVC_VENV_PY}（sounddevice 在该环境）")
    try:
        proc = subprocess.run(
            [str(RVC_VENV_PY), "-c", _PLAY_SCRIPT, str(wav), OUTPUT_DEVICE_KEYWORD,
             str(LEAD_S), str(TAIL_S)],
            capture_output=True, text=True, timeout=duration_s + 30,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("播放到 CABLE Input 超时")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(f"播放失败: {err[-1][:300] if err else proc.returncode}")


# ---------------- 微信窗口定位与按键模拟（纯 ctypes，零依赖） ----------------

_vk_control, _vk_menu, _vk_lwin, _vk_return = 0x11, 0x12, 0x5B, 0x0D
_keyup = 0x0002


def _key(vk: int, up: bool = False) -> None:
    import ctypes
    ctypes.windll.user32.keybd_event(vk, 0, _keyup if up else 0, 0)


def _find_wechat_hwnd() -> int:
    """枚举顶层可见窗口，按进程名找微信主窗口（Weixin.exe=4.x / WeChat.exe=3.x）。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hits: list[int] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = Path(buf.value).name.lower()
                if name in ("weixin.exe", "wechat.exe", "wechatapp.exe"):
                    hits.append(hwnd)
            kernel32.CloseHandle(h)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    if not hits:
        raise RuntimeError("没找到微信窗口（请确认微信已登录并打开了聊天）")
    return hits[0]


def _foreground_wechat() -> None:
    """把微信拉到前台。SetForegroundWindow 有系统限制，用 ALT 抖动绕过。"""
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = _find_wechat_hwnd()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)
    _key(_vk_menu)              # ALT down/up 解锁前台切换限制
    _key(_vk_menu, up=True)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    if user32.GetForegroundWindow() != hwnd:
        raise RuntimeError("无法把微信窗口切到前台，请保持微信窗口打开后重试")


def _trigger_record() -> None:
    """模拟 Ctrl+Win 触发微信官方语音输入。"""
    _key(_vk_control)
    _key(_vk_lwin)
    time.sleep(0.08)
    _key(_vk_lwin, up=True)
    _key(_vk_control, up=True)


def _finish_record() -> None:
    """结束录音并发送（当前策略：回车）。"""
    if FINISH_KEY == "enter":
        _key(_vk_return)
        time.sleep(0.08)
        _key(_vk_return, up=True)


# ---------------- wav 时长（stdlib wave；合成产物是标准 PCM wav） ----------------

def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / max(1, w.getframerate())
    except Exception:
        return 0.0


# ---------------- 主流程 ----------------

class SendVoiceReq(BaseModel):
    wav: str | None = None   # outputs/ 下的文件名；缺省=最近一次 TTS 合成产物


@router.post("/send_voice")
def send_voice(req: SendVoiceReq):
    """把一段合成语音以「微信语音消息」的形式录进当前打开的微信聊天窗口。

    前置条件：微信已打开目标聊天窗口；TTS 已合成（否则带 wav 文件名调用 /api/tts 后再发）。
    注意：执行期间（约 wav 时长 + 3s）不要动键鼠，微信会被抢前台。
    """
    if not _send_lock.acquire(blocking=False):
        raise HTTPException(409, "已有一次微信语音发送在进行中，请等它结束")
    try:
        return _do_send(req)
    finally:
        _send_lock.release()


def _do_send(req: SendVoiceReq):
    steps: list[str] = []

    # 1) 定位 wav：指定名 → outputs 下精确匹配；否则最近的 tts_*.wav
    if req.wav:
        wav = (cfg.OUTPUTS_DIR / req.wav) if not Path(req.wav).is_absolute() else Path(req.wav)
        if not wav.exists():
            return JSONResponse(status_code=404, content={"ok": False, "error": f"找不到音频 {req.wav}"})
    else:
        cands = sorted(cfg.OUTPUTS_DIR.glob("tts_*.wav"), key=lambda p: p.stat().st_mtime)
        if not cands:
            return JSONResponse(status_code=404, content={
                "ok": False, "error": "outputs/ 下没有 TTS 产物，先在网页上合成一条语音"})
        wav = cands[-1]
    duration = _wav_duration(wav)
    steps.append(f"音频: {wav.name}（{duration:.1f}s）")

    restored = False
    try:
        # 2) 默认麦克风 → CABLE Output（微信从这里录；apply 自动备份原设备）
        _run_audio("apply")
        steps.append("麦克风已切到 CABLE Output")

        # 3) 微信前台 → 触发官方语音输入录音
        _foreground_wechat()
        _trigger_record()
        steps.append("已触发微信录音（Ctrl+Win）")
        time.sleep(LEAD_S + 0.25)   # 等录音真正开始，静音头由播放端再垫一层

        # 4) 把 wav 播进 CABLE Input
        _play_to_cable(wav, duration)
        steps.append(f"已播放 {duration:.1f}s 到微信录音")

        # 5) 结束录音 → 微信自动/确认发送
        time.sleep(TAIL_S)
        _finish_record()
        steps.append("已发送结束指令（Enter）")
        time.sleep(0.6)
    except Exception as exc:
        # 失败也要还原声卡
        try:
            _run_audio("restore")
            restored = True
        except Exception:
            pass
        return JSONResponse(status_code=500, content={
            "ok": False, "error": str(exc), "steps": steps, "restored": restored})

    # 6) 还原原声卡
    restore_err = ""
    try:
        _run_audio("restore")
        restored = True
        steps.append("声卡已还原")
    except Exception as exc:
        restore_err = str(exc)

    return {"ok": True, "wav": wav.name, "duration_s": round(duration, 1),
            "steps": steps, "restored": restored, "restore_error": restore_err,
            "_history": _append_history(wav, duration)}


# ---------------- 发送历史（桌宠「最近发送」用） ----------------

HISTORY_FILE = cfg.OUTPUTS_DIR / "wechat_send_history.json"
HISTORY_MAX = 20


def _append_history(wav: Path, duration_s: float) -> bool:
    """把一次成功发送记进历史（最多 HISTORY_MAX 条，覆盖写）。"""
    try:
        hist = []
        if HISTORY_FILE.exists():
            hist = json.loads(HISTORY_FILE.read_text("utf-8"))
        hist.append({"wav": wav.name, "duration_s": round(duration_s, 1), "ts": int(time.time())})
        HISTORY_FILE.write_text(json.dumps(hist[-HISTORY_MAX:], ensure_ascii=False), "utf-8")
        return True
    except Exception:
        return False


@router.get("/history")
def send_history():
    """最近发送的微信语音列表（供桌宠面板展示，点一条可重发）。"""
    hist = []
    if HISTORY_FILE.exists():
        try:
            hist = json.loads(HISTORY_FILE.read_text("utf-8"))
        except Exception:
            hist = []
    return {"ok": True, "items": hist}


@router.get("/send_voice/last")
def last_send():
    """给桌宠/前端查询最近一次合成产物（发送前预览用）。"""
    cands = sorted(cfg.OUTPUTS_DIR.glob("tts_*.wav"), key=lambda p: p.stat().st_mtime)
    if not cands:
        return {"ok": False, "error": "还没有 TTS 产物"}
    wav = cands[-1]
    return {"ok": True, "wav": wav.name, "duration_s": round(_wav_duration(wav), 1),
            "url": f"/api/media/outputs/{wav.name}"}
