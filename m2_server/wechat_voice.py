"""微信语音消息发送（桌宠右键入口）。

思路（全程不 Hook、不注入微信，只模拟人手操作）：
    1. audio_config.ps1 apply  —— 系统默认麦克风切到 CABLE Output（自动备份原设备）
    2. 前台化微信聊天窗口，触发微信 4.1.9+ 的官方「发送语音消息」开始录音：
       - RECORD_METHOD=mic（默认）：鼠标移到聊天输入框右下角话筒图标，按住左键
         （官方交互：长按说话、松开自动发送；鼠标注入接受度高于键盘）
       - RECORD_METHOD=alt：按住 Alt 键（SendInput 带扫描码，比旧 keybd_event 更真实；
         但实测部分微信版本会忽略软件注入的键盘输入）
       微信从系统默认麦克风采集，此刻即 CABLE Output
    3. 用 RVC venv 的 sounddevice 把 TTS 产物 wav 直接播进 CABLE Input
       （微信同步从 CABLE Output 录到的就是这段声音），首尾各垫静音防掐头去尾
    4. 松开鼠标左键 / Alt → 微信结束录音并自动发出（单条最长 60s）
    5. audio_config.ps1 restore —— 还原原声卡

依赖：主环境零新增（ctypes + subprocess）；播放走 D:/RVC/.venv 的 sounddevice，
与 cascade_stream/offline_vc 同一约定。

重要更正（实测踩坑）：
    - 微信 4.1.7 的 Ctrl+Win 是「语音转文字」：说话实时转文字、不会自己停，
      且发出的是文字不是语音 —— 不是我们要的，已弃用。
    - 微信 4.1.9+ 才上线「发送语音消息」：按住 Alt 说话、松开发送（或点输入框
      右下角话筒）。本模块默认走话筒鼠标路径（mic）；想强制走键盘改
      VM_WECHAT_RECORD_METHOD=alt。若微信里改过快捷键，调 VM_WECHAT_RECORD_KEY。
    - 2026-09-09 调研：微信 4.x 聊天区是 Qt Quick 自绘（UIA 全黑盒），
      社区共识 = 视觉/坐标 + 鼠标模拟；「鼠标长按话筒」是官方交互，
      自动发送成功率高于模拟键盘 Alt。
"""

import json
import os
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

import config as cfg
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/wechat", tags=["wechat"])

AUDIO_PS1 = cfg.ROOT / "m2_server" / "audio_config.ps1"
RVC_VENV_PY = cfg.RVC_ROOT / ".venv" / "Scripts" / "python.exe"
# 输出目标（cascade_stream 同款关键词）：wav 播进 CABLE Input，微信从 CABLE Output 录
OUTPUT_DEVICE_KEYWORD = os.environ.get("VM_LIVE_OUTPUT_DEVICE", "CABLE Input")
# 发语音方式：mic=鼠标长按输入框右下角话筒图标（默认，官方交互）
#             alt=按住键盘快捷键（微信默认 Alt；VM_WECHAT_RECORD_KEY 可改键）
RECORD_METHOD = os.environ.get("VM_WECHAT_RECORD_METHOD", "mic").strip().lower()
if RECORD_METHOD not in ("mic", "alt"):
    raise RuntimeError(f"VM_WECHAT_RECORD_METHOD 只支持 mic/alt，当前: {RECORD_METHOD}")
# 话筒图标相对微信聊天窗口右下角的偏移（物理像素；不同窗口尺寸/主题可微调）
MIC_OFFSET_X = int(os.environ.get("VM_WECHAT_MIC_OFFSET_X", "-58"))
MIC_OFFSET_Y = int(os.environ.get("VM_WECHAT_MIC_OFFSET_Y", "-30"))
# 发语音按键：RECORD_METHOD=alt 时生效。默认 alt；微信里改过快捷键就用 VM_WECHAT_RECORD_KEY 指定。
RECORD_KEY = os.environ.get("VM_WECHAT_RECORD_KEY", "alt")
LEAD_S = float(os.environ.get("VM_WECHAT_LEAD_S", "0.35"))   # 按住后等待录音开始
TAIL_S = float(os.environ.get("VM_WECHAT_TAIL_S", "0.3"))    # 播完后的尾巴静音（松开前）
# 自动按键流程失败时是否自动降级为「引导式手动发送」（播放到 CABLE + 用户自己按住说话）
AUTO_FALLBACK = os.environ.get("VM_WECHAT_AUTO_FALLBACK", "1") == "1"

_send_lock = threading.Lock()
_play_proc = None   # 正在向 CABLE 播放的子进程，供 /stop_play 中止


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


# ---------------- 微信窗口定位与输入模拟（纯 ctypes，零依赖） ----------------

# 键位表：VK 用左键变体（比 VK_MENU 0x12 更精确），scan 是对应硬件扫描码
# （SendInput 同时带上 wVk+wScan，Qt/RawInput 层的应用能读到更完整的输入事件）
_KEYMAP = {
    "alt":   (0xA4, 0x38),   # VK_LMENU
    "menu":  (0xA4, 0x38),
    "ctrl":  (0xA2, 0x1D),   # VK_LCONTROL
    "control": (0xA2, 0x1D),
    "shift": (0xA0, 0x2A),   # VK_LSHIFT
    "win":   (0x5B, 0x5B),   # VK_LWIN
    "lwin":  (0x5B, 0x5B),
}
_fkey_up = 0x0002


def _record_key_code() -> tuple[int, int]:
    """把 RECORD_KEY 映射成 (VK, scan)。默认 alt（微信发语音）；支持常见别名。"""
    key = RECORD_KEY.strip().lower()
    if key in _KEYMAP:
        return _KEYMAP[key]
    raise RuntimeError(f"不支持的 VM_WECHAT_RECORD_KEY: {RECORD_KEY}（支持 alt/ctrl/shift/win）")


def _user32():
    import ctypes
    return ctypes.windll.user32


# SendInput 输入结构（模块级定义一次；union 尺寸按 MOUSEINPUT 对齐）
import ctypes as _ctypes


class _KBDINPUT(_ctypes.Structure):
    _fields_ = [("wVk", _ctypes.c_ushort), ("wScan", _ctypes.c_ushort),
                ("dwFlags", _ctypes.c_ulong), ("time", _ctypes.c_ulong),
                ("dwExtraInfo", _ctypes.c_void_p)]


class _MOUSEINPUT(_ctypes.Structure):
    _fields_ = [("dx", _ctypes.c_long), ("dy", _ctypes.c_long),
                ("mouseData", _ctypes.c_ulong), ("dwFlags", _ctypes.c_ulong),
                ("time", _ctypes.c_ulong), ("dwExtraInfo", _ctypes.c_void_p)]


class _INPUT(_ctypes.Structure):
    class _U(_ctypes.Union):
        _fields_ = [("ki", _KBDINPUT), ("mi", _MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", _ctypes.c_ulong), ("u", _U)]


_INPUT_KEYBOARD, _INPUT_MOUSE = 1, 0


def _send_input_kb(vk: int, scan: int, up: bool) -> None:
    """SendInput 模拟键盘事件（比 keybd_event 新、支持注入完整 wVk+wScan）。"""
    inp = _INPUT()
    inp.type = _INPUT_KEYBOARD
    inp.ki = _KBDINPUT(vk, scan, _fkey_up if up else 0, 0, None)
    _user32().SendInput(1, _ctypes.byref(inp), _ctypes.sizeof(inp))


def _key(vk: int, up: bool = False, scan: int = 0) -> None:
    _send_input_kb(vk, scan, up)


def _send_input_mouse(flags: int, dx: int = 0, dy: int = 0) -> None:
    inp = _INPUT()
    inp.type = _INPUT_MOUSE
    inp.mi = _MOUSEINPUT(dx, dy, 0, flags, 0, None)
    _user32().SendInput(1, _ctypes.byref(inp), _ctypes.sizeof(inp))


# 进程内统一 DPI 坐标系（幂等）：GetWindowRect / GetSystemMetrics / ImageGrab 全部物理像素
try:
    _user32().SetProcessDPIAware()
except Exception:
    pass

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000


def _mouse_move_abs(x: int, y: int) -> None:
    """绝对坐标移动鼠标（物理像素 → 虚拟屏幕 0-65535 归一化，多屏安全）。"""
    u = _user32()
    sx, sy = u.GetSystemMetrics(SM_XVIRTUALSCREEN), u.GetSystemMetrics(SM_YVIRTUALSCREEN)
    sw, sh = u.GetSystemMetrics(SM_CXVIRTUALSCREEN), u.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if sw <= 1 or sh <= 1:
        raise RuntimeError("虚拟屏幕尺寸异常，无法移动鼠标")
    nx = round((x - sx) * 65535 / (sw - 1))
    ny = round((y - sy) * 65535 / (sh - 1))
    nx = max(0, min(65535, nx))
    ny = max(0, min(65535, ny))
    _send_input_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)


def _mouse_left(down: bool) -> None:
    _send_input_mouse(MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP)


def _mic_point(rect: tuple[int, int, int, int],
               offset_x: int = MIC_OFFSET_X, offset_y: int = MIC_OFFSET_Y) -> tuple[int, int]:
    """由窗口 rect 计算话筒图标坐标（输入框右下角、发送按钮左侧，可配置偏移）。"""
    left, top, right, bottom = rect
    return (right + offset_x, bottom + offset_y)


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    import ctypes
    from ctypes import wintypes
    rect = wintypes.RECT()
    if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect 失败")
    return rect.left, rect.top, rect.right, rect.bottom


def _find_wechat_hwnd() -> int:
    """枚举顶层可见窗口，按进程名找微信主窗口（Weixin.exe=4.x / WeChat.exe=3.x）。

    多个命中时取面积最大的（聊天主窗口；托盘气泡/小弹窗都很小）。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hits: list[tuple[int, int]] = []   # (hwnd, area)

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
                    r = wintypes.RECT()
                    area = 0
                    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
                        area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                    hits.append((hwnd, area))
            kernel32.CloseHandle(h)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    if not hits:
        raise RuntimeError("没找到微信窗口（请确认微信已登录并打开了聊天）")
    hits.sort(key=lambda t: t[1], reverse=True)
    if hits[0][1] <= 0:
        raise RuntimeError("找到微信进程但窗口尺寸异常，请把微信聊天窗口打开后重试")
    return hits[0][0]


def _foreground_wechat() -> int:
    """把微信拉到前台，返回其窗口句柄。SetForegroundWindow 有系统限制，用 ALT 抖动绕过。"""
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = _find_wechat_hwnd()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)
    vk, scan = _KEYMAP["alt"]
    _send_input_kb(vk, scan, False)   # ALT down/up 解锁前台切换限制
    _send_input_kb(vk, scan, True)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.25)
    if user32.GetForegroundWindow() != hwnd:
        raise RuntimeError("无法把微信窗口切到前台，请保持微信窗口打开后重试")
    return hwnd


def _trigger_record() -> None:
    """开始录制语音消息：mic=鼠标按住话筒；alt=按住快捷键。"""
    if RECORD_METHOD == "mic":
        hwnd = _foreground_wechat()
        rect = _window_rect(hwnd)
        x, y = _mic_point(rect)
        _mouse_move_abs(x, y)
        time.sleep(0.12)          # 微信对 hover 有响应延迟
        _mouse_left(True)
    else:
        _foreground_wechat()
        vk, scan = _record_key_code()
        _send_input_kb(vk, scan, False)


def _finish_record() -> None:
    """结束录制并发送语音消息：mic=松开左键（官方交互：松开自动发送）；alt=松开快捷键。"""
    if RECORD_METHOD == "mic":
        _mouse_left(False)
    else:
        vk, scan = _record_key_code()
        _send_input_kb(vk, scan, True)


# ---------------- wav 时长（stdlib wave；合成产物是标准 PCM wav） ----------------

def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / max(1, w.getframerate())
    except Exception:
        return 0.0


def _trim_edges(path: Path, keep_head_s: float = 0.12, keep_tail_s: float = 0.12,
                thresh: float = 0.015) -> Path:
    """裁掉 wav 首尾静音，只保留人声段前后一点点缓冲。

    背景：TTS 产物经常首尾带 0.5~1s 静音，直接播放会让微信录出的语音
    前后大片空白。这里用 RMS 能量找首尾有声边界，裁掉静音段。
    """
    try:
        d, sr = sf.read(str(path), dtype="float32", always_2d=False)
        if d.ndim > 1:
            d = d.mean(axis=1)
        if len(d) == 0:
            return path
        win = max(int(sr * 0.02), 1)          # 20ms 滑动窗
        n = len(d) // win
        rms = np.sqrt((d[: n * win] ** 2).reshape(n, win).mean(axis=1))
        idx = np.nonzero(rms > thresh)[0]
        if len(idx) == 0:                      # 整段都静音，不裁（可能本就是纯静音测试）
            return path
        s = max(int((idx[0] - keep_head_s * 50) * win), 0)
        e = min(int((idx[-1] + 1 + keep_tail_s * 50) * win), len(d))
        if e - s < int(sr * 0.3):              # 裁后太短（<0.3s），保持原样
            return path
        tmp = path.with_name(f"{path.stem}_trim{path.suffix}")
        sf.write(tmp, d[s:e], sr, format="WAV")
        return tmp
    except Exception:
        return path


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


@router.post("/manual_send")
def manual_send():
    """引导式手动发变声语音：把微信录音设备切到 CABLE Output，并确保 RVC 实时变声在运行，
    然后由用户自己在微信里按住 Alt（或点话筒）说话、说完松开发送。
    不做自动按键、不播放 TTS —— 完全用户手动掌控，绕开「自动录音停不下来」的坑。

    注意：不开实时变声时别用这个方式发语音（微信会录到 CABLE 的空转噪音=电音）。
    """
    steps: list[str] = []
    try:
        from rvc_live import _live_proc_alive, rvc_live_start
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            "ok": False, "error": f"加载实时变声模块失败: {exc}", "steps": steps})
    try:
        if _live_proc_alive():
            steps.append("实时变声已在运行")
        else:
            resp = rvc_live_start(None)
            try:
                body = json.loads(resp.body)
            except Exception:
                body = {}
            if not body.get("ok"):
                detail = body.get("detail") or body.get("error") or "未知错误"
                return JSONResponse(status_code=500, content={
                    "ok": False, "error": f"启动实时变声失败: {detail}", "steps": steps})
            steps.append("已启动实时变声（模型加载中，稍等片刻）")
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            "ok": False, "error": f"启动实时变声失败: {exc}", "steps": steps})
    return {
        "ok": True,
        "steps": steps,
        "hint": "微信录音已切到 CABLE Output，变声运行中",
        "hint2": "去微信按住 Alt 说话，说完松开即发送",
        "warn": "若录到原声/电音，请把系统录音设备改成 CABLE Output",
    }


class PlayToCableReq(BaseModel):
    wav: str | None = None     # outputs/ 下的文件名；缺省=最近 TTS
    lead_s: float | None = None  # 静音头长度（秒），给用户时间按 Alt，默认 2.0


@router.post("/play_to_cable")
def play_to_cable(req: PlayToCableReq):
    """把音频播放到 CABLE Input —— 不模拟 Alt 键，由用户自己在微信里按 Alt 录。

    替代 send_voice 的「自动按住 Alt 播放再松开」流程：那个流程在你的微信上不工作
    （软件模拟的 Alt 不触发录音），但后端会静默返回成功，造成"软件骗人"。

    本接口：apply 切录音到 CABLE Output → 播 wav 到 CABLE Input（带静音头）→ restore。
    静音头给用户 2 秒时间到微信按 Alt 说话，录到这段音频后松开发送。
    """
    if not _send_lock.acquire(blocking=False):
        raise HTTPException(409, "已有一次微信语音发送在进行中，请等它结束")
    try:
        return _do_play_to_cable(req)
    finally:
        _send_lock.release()


def _do_play_to_cable(req: PlayToCableReq):
    steps: list[str] = []
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
    trimmed = _trim_edges(wav)
    if trimmed != wav:
        steps.append(f"已裁掉首尾静音: {wav.name} -> {trimmed.name}")
        wav = trimmed
    duration = _wav_duration(wav)
    lead = float(req.lead_s) if req.lead_s and req.lead_s > 0 else 2.0
    steps.append(f"音频: {wav.name}（{duration:.1f}s）")
    try:
        _run_audio("apply")
        steps.append("麦克风已切到 CABLE Output")
        global _play_proc
        proc = subprocess.Popen(
            [str(RVC_VENV_PY), "-c", _PLAY_SCRIPT, str(wav), OUTPUT_DEVICE_KEYWORD,
             str(lead), str(TAIL_S)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        _play_proc = proc
        try:
            _, perr = proc.communicate(timeout=duration + 60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RuntimeError("播放到 CABLE Input 超时")
        finally:
            _play_proc = None
        if proc.returncode != 0:
            err = (perr or "").strip().splitlines()
            raise RuntimeError(f"播放失败: {err[-1][:300] if err else proc.returncode}")
        steps.append(f"已播放 {duration:.1f}s 到 CABLE Input（含 {lead}s 静音头）")
        _run_audio("restore")
        steps.append("声卡已还原")
    except Exception as exc:
        try:
            _run_audio("restore")
        except Exception:
            pass
        return JSONResponse(status_code=500, content={
            "ok": False, "error": str(exc), "steps": steps})
    return {
        "ok": True,
        "wav": wav.name,
        "duration_s": round(duration, 1),
        "lead_s": lead,
        "steps": steps,
        "hint": (f"音频已播放到 CABLE（{duration:.1f}s，含 {lead}s 静音头）。"
                 f"请在这 2 秒静音头内到微信按住 Alt 开始说话，"
                 f"录到这段音频后松开 Alt 发送。"),
    }


@router.post("/stop_play")
def stop_play():
    """中止正在进行的 play_to_cable 播放（用户误触后按 Esc 退出录制时使用）。

    仅杀掉向 CABLE Input 播放的子进程；play_to_cable 请求线程的 finally
    会负责把声卡还原回原设备，无需此处再 restore。
    """
    proc = _play_proc
    if proc is None or proc.poll() is not None:
        return {"ok": True, "stopped": False, "detail": "当前没有正在播放的微信语音"}
    try:
        proc.kill()
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            "ok": False, "error": f"停止播放失败: {exc}"})
    return {"ok": True, "stopped": True,
            "detail": "已停止向 CABLE 播放，声卡将自动还原"}


def _safe_restore() -> tuple[bool, str]:
    """还原声卡：restore 失败走 reset 兜底。返回 (ok, error)。"""
    try:
        _run_audio("restore")
        return True, ""
    except Exception as e:
        try:
            _run_audio("reset")
            return True, f"restore 失败({e})，已用 reset 兜底恢复"
        except Exception as e2:
            return False, f"restore 失败({e})；reset 兜底也失败({e2})"


def _do_send(req: SendVoiceReq):
    steps: list[str] = []

    # 1) 定位 wav：指定名 → outputs 下精确匹配；否则最近的 tts_*.wav
    if req.wav:
        wav = (cfg.OUTPUTS_DIR / req.wav) if not Path(req.wav).is_absolute() else Path(req.wav)
        if not wav.exists():
            return JSONResponse(status_code=404, content={
                "ok": False, "outcome": "failed", "error": f"找不到音频 {req.wav}"})
    else:
        cands = sorted(cfg.OUTPUTS_DIR.glob("tts_*.wav"), key=lambda p: p.stat().st_mtime)
        if not cands:
            return JSONResponse(status_code=404, content={
                "ok": False, "outcome": "failed",
                "error": "outputs/ 下没有 TTS 产物，先在网页上合成一条语音"})
        wav = cands[-1]
    duration = _wav_duration(wav)
    steps.append(f"音频: {wav.name}（{duration:.1f}s）")

    restored = False
    try:
        # 2) 默认麦克风 → CABLE Output（微信从这里录；apply 自动备份原设备）
        _run_audio("apply")
        steps.append("麦克风已切到 CABLE Output")

        # 3) 微信前台 → 按住话筒（鼠标）/快捷键（键盘）开始录制语音消息
        _trigger_record()
        hold_name = "话筒图标" if RECORD_METHOD == "mic" else RECORD_KEY.upper()
        steps.append(f"已按住{hold_name}开始录音")
        time.sleep(LEAD_S + 0.25)   # 等录音真正开始，静音头由播放端再垫一层

        # 4) 把 wav 播进 CABLE Input
        _play_to_cable(wav, duration)
        steps.append(f"已播放 {duration:.1f}s 到微信录音")

        # 5) 松开 → 结束录制并自动发送
        time.sleep(TAIL_S)
        _finish_record()
        steps.append(f"已松开{hold_name}，语音已发送")
        time.sleep(0.6)
    except Exception as exc:
        # 失败也要：①松开录音键/鼠标（防止按住不放卡死）②还原声卡（reset 兜底）
        try:
            _finish_record()
        except Exception:
            pass
        restored, restore_err = _safe_restore()
        # 自动降级：模拟按键/播放失败 → 引导式手动发送（播放到 CABLE，用户自己按 Alt）
        if AUTO_FALLBACK:
            fb = _guided_fallback(wav, duration, steps)
            return {"ok": True, "outcome": "manual_fallback", "wav": wav.name,
                    "duration_s": round(duration, 1),
                    "steps": steps + fb["steps"],
                    "restored": restored, "restore_error": restore_err,
                    "auto_error": str(exc),
                    "fallback": fb,
                    "_history": _append_history(wav, duration, "manual_fallback")}
        return JSONResponse(status_code=500, content={
            "ok": False, "outcome": "failed", "error": str(exc),
            "steps": steps, "restored": restored, "restore_error": restore_err})

    # 6) 还原原声卡（reset 兜底）
    restored, restore_err = _safe_restore()
    if restore_err:
        steps.append(f"声卡还原：{restore_err}")
    else:
        steps.append("声卡已还原")

    return {"ok": True, "outcome": "ok", "method": RECORD_METHOD, "wav": wav.name, "duration_s": round(duration, 1),
            "steps": steps, "restored": restored, "restore_error": restore_err,
            "_history": _append_history(wav, duration, "ok")}


def _guided_fallback(wav: Path, duration: float, steps: list[str]) -> dict:
    """降级引导：把 wav 播到 CABLE（带 2s 静音头），提示用户手动按 Alt。返回引导结果。"""
    try:
        _run_audio("apply")
        _play_to_cable(wav, duration)
        _safe_restore()
        return {"steps": ["已降级：音频已播到 CABLE，请到微信手动录完松开发送"],
                "hint": "到微信按住 Alt（或长按输入框右下角话筒图标）说话，录到这段音频后松开发送",
                "lead_s": 2.0}
    except Exception as e:
        return {"steps": ["降级播放失败"],
                "hint": f"自动降级也失败（{e}），请改用「手动发送」",
                "lead_s": 2.0}


# ---------------- 发送历史（桌宠「最近发送」用） ----------------

HISTORY_FILE = cfg.OUTPUTS_DIR / "wechat_send_history.json"
HISTORY_MAX = 20


def _append_history(wav: Path, duration_s: float, outcome: str = "ok") -> bool:
    """把一次发送记进历史（最多 HISTORY_MAX 条，覆盖写）。outcome: ok/manual_fallback/failed。"""
    try:
        hist = []
        if HISTORY_FILE.exists():
            hist = json.loads(HISTORY_FILE.read_text("utf-8"))
        hist.append({"wav": wav.name, "duration_s": round(duration_s, 1),
                     "ts": int(time.time()), "outcome": outcome})
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
    # 向后兼容：旧记录没有 outcome 字段，补默认 ok
    for it in hist:
        it.setdefault("outcome", "ok")
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
