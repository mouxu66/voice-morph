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
import logging
import os
import subprocess
import threading
import time
import wave
from pathlib import Path

logger = logging.getLogger(__name__)

import numpy as np
import soundfile as sf

import config as cfg
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/api/wechat", tags=["wechat"])

AUDIO_PS1 = cfg.ROOT / "m2_server" / "audio_config.ps1"
RVC_VENV_PY = cfg.RVC_ROOT / ".venv" / "Scripts" / "python.exe"

# 子进程一律隐藏控制台窗口（Windows CREATE_NO_WINDOW）。
# 弹出的 PowerShell/Python 蓝窗会短暂遮挡微信右下角，让 _find_green_send
# 截图截到控制台 → 浮层检测误判 → 自动发送整体降级（2026-09-09 实测事故）。
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# 输出目标（cascade_stream 同款关键词）：wav 播进 CABLE Input，微信从 CABLE Output 录
OUTPUT_DEVICE_KEYWORD = os.environ.get("VM_LIVE_OUTPUT_DEVICE", "CABLE Input")
# 发语音方式：mic=鼠标长按输入框右下角话筒图标（默认，官方交互）
#             alt=按住键盘快捷键（微信默认 Alt；VM_WECHAT_RECORD_KEY 可改键）
RECORD_METHOD = os.environ.get("VM_WECHAT_RECORD_METHOD", "mic").strip().lower()
if RECORD_METHOD not in ("mic", "alt"):
    raise RuntimeError(f"VM_WECHAT_RECORD_METHOD 只支持 mic/alt，当前: {RECORD_METHOD}")
# 话筒图标相对微信聊天窗口右下角的偏移（物理像素；2026-09-09 本机 1299x1609 窗口实测校准）
MIC_OFFSET_X = int(os.environ.get("VM_WECHAT_MIC_OFFSET_X", "-157"))
MIC_OFFSET_Y = int(os.environ.get("VM_WECHAT_MIC_OFFSET_Y", "-63"))
# 发语音按键：RECORD_METHOD=alt 时生效。默认 alt；微信里改过快捷键就用 VM_WECHAT_RECORD_KEY 指定。
RECORD_KEY = os.environ.get("VM_WECHAT_RECORD_KEY", "alt")
LEAD_S = float(os.environ.get("VM_WECHAT_LEAD_S", "0.35"))   # 按住后等待录音开始
TAIL_S = float(os.environ.get("VM_WECHAT_TAIL_S", "0.3"))    # 播完后的尾巴静音（松开前）
# 自动发送专用静音头：开播后先垫这段再出人声，用来盖住「点按钮→微信真正开始录」的延迟。
# 太小会削掉开头第一个字，太大会在语音前留下空白。0.8s 是实测折中。
PLAY_LEAD_S = float(os.environ.get("VM_WECHAT_PLAY_LEAD_S", "0.8"))
# 自动按键流程失败时是否自动降级为「引导式手动发送」（播放到 CABLE + 用户自己按住说话）
AUTO_FALLBACK = os.environ.get("VM_WECHAT_AUTO_FALLBACK", "1") == "1"

_send_lock = threading.Lock()
_play_proc = None   # 正在向 CABLE 播放的子进程，供 /stop_play 中止
PLAY_WORKER_ENABLED = os.environ.get("VM_WECHAT_PLAY_WORKER", "1") == "1"
PLAY_WORKER_SCRIPT = cfg.ROOT / "m2_server" / "play_worker.py"
_play_worker_proc = None   # 常驻播放 worker（play_worker.py），复用同进程省冷导入


class _PlayWorkerHandle:
    """对常驻播放 worker 的一次播放会话，伪装成 subprocess.Popen 供 _wait_play_start/_wait_play_done 复用。

    发送已序列化（_send_lock），所以 worker 的 stdout 行协议按命令顺序消费即可：
    本 handle 负责读自己这条命令的 playing / done（或 error）。
    """

    def __init__(self, proc: "subprocess.Popen"):
        self.proc = proc

    @property
    def stdout(self):
        return self.proc.stdout

    def poll(self):
        return self.proc.poll()

    def kill(self):
        # 错误路径：杀掉整个常驻 worker（下次发送自动重启，会再付一次冷导入，可接受）。
        _stop_play_worker()

    def wait(self, timeout: float | None = None):
        deadline = time.time() + (timeout or 60.0)
        while time.time() < deadline:
            line = self.proc.stdout.readline() if self.proc.stdout else ""
            if not line:
                raise RuntimeError("播放 worker 已退出（stdout 关闭）")
            if '"done"' in line:
                return 0
            if '"error"' in line:
                msg = ""
                try:
                    msg = json.loads(line).get("msg", "")
                except Exception:
                    pass
                raise RuntimeError(f"播放 worker 失败: {msg}")
        raise RuntimeError("播放 worker 等待 done 超时")


def _get_play_worker() -> "subprocess.Popen | None":
    """拿到常驻播放 worker（懒启动 + 复用）；失败返回 None（调用方退回一次性子进程）。"""
    global _play_worker_proc
    if _play_worker_proc is not None and _play_worker_proc.poll() is None:
        return _play_worker_proc
    try:
        if not RVC_VENV_PY.exists():
            raise RuntimeError(f"找不到 RVC venv 解释器: {RVC_VENV_PY}")
        if not PLAY_WORKER_SCRIPT.exists():
            raise RuntimeError(f"找不到 play_worker.py: {PLAY_WORKER_SCRIPT}")
        proc = subprocess.Popen(
            [str(RVC_VENV_PY), str(PLAY_WORKER_SCRIPT), OUTPUT_DEVICE_KEYWORD],
            stdout=subprocess.PIPE, stdin=subprocess.PIPE, text=True, bufsize=1,
            creationflags=_NO_WINDOW,
        )
        line = proc.stdout.readline() if proc.stdout else ""
        if not line or '"ready"' not in line:
            try:
                proc.kill()
            except Exception:
                pass
            raise RuntimeError(f"play worker 未就绪: {line[:200]!r}")
        _play_worker_proc = proc
        logger.info("[wechat] 播放 worker 已就绪（常驻，省冷导入）")
        return proc
    except Exception as e:
        logger.warning("[wechat] 播放 worker 启动失败，回退一次性播放: %s", e)
        return None


def _stop_play_worker() -> None:
    """杀掉常驻播放 worker（错误路径 / 重启用）。"""
    global _play_worker_proc
    proc = _play_worker_proc
    _play_worker_proc = None
    if proc is None:
        return
    try:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.kill()
    except Exception:
        pass


def _persist_verify(verify_steps: list[str]) -> None:
    """把后台 UIA 校验结果写回发送历史最后一条（异步线程调用，不阻塞返回）。

    发送已序列化，最后一条必是本次刚落的历史；发完才起本线程，故无竞态。
    """
    try:
        if not HISTORY_FILE.exists():
            return
        hist = json.loads(HISTORY_FILE.read_text("utf-8"))
        if hist:
            hist[-1].setdefault("verify", []).extend(verify_steps)
            HISTORY_FILE.write_text(json.dumps(hist, ensure_ascii=False), "utf-8")
    except Exception as e:
        logger.debug("[wechat] 写回校验结果失败: %s", e)


def _restore_async() -> None:
    """后台线程还原声卡并写回发送历史最后一条，不阻塞 _do_send 返回（省 ~3s 同步等待）。

    与下一步 _run_audio('apply') 通过 _restore_lock 互斥，避免同时改默认音频设备抢设备。
    发送已落库（_append_history 在起本线程前完成），最后一条必是本次，无竞态。
    """
    try:
        with _restore_lock:
            restored, restore_err = _safe_restore()
        with _history_lock:
            try:
                if HISTORY_FILE.exists():
                    hist = json.loads(HISTORY_FILE.read_text("utf-8"))
                    if hist:
                        hist[-1]["restored"] = restored
                        hist[-1]["restore_error"] = restore_err
                        HISTORY_FILE.write_text(
                            json.dumps(hist, ensure_ascii=False), "utf-8")
            except Exception as e:
                logger.debug("[wechat] 写回还原结果失败: %s", e)
    except Exception as e:
        logger.debug("[wechat] 后台还原失败: %s", e)


# -------- UIA 结构化访问（方案 A，2026-09-10）--------
# 微信 4.x 聊天区自绘，UIA 默认只有 Qt 空壳；热激活后能拿到带矩形的结构化控件。
# 这里只把「检测录音浮层 / 定位浮层按钮 / 校验发送结果」交给 UIA，像素链路
# （模板匹配 + 绿钮 HSV）完整保留为降级路径。禁用：VM_WECHAT_UIA=0。
try:
    import wechat_uia as _uia
except Exception as _e:      # uiautomation 未安装 / 非 Windows → 纯像素链路
    _uia = None
    logger.info("[wechat] UIA 模块不可用，使用纯像素链路: %s", _e)


def _uia_ready() -> bool:
    """UIA 是否可用（热激活成功且控件树已物化）。任何失败都返回 False。"""
    if _uia is None:
        return False
    try:
        return _uia.uia_ready()
    except Exception as e:
        logger.debug("[wechat] UIA 探测失败: %s", e)
        return False


def _uia_center(box: tuple[int, int, int, int] | None) -> tuple[int, int] | None:
    return (((box[0] + box[2]) // 2), ((box[1] + box[3]) // 2)) if box else None


# ---------------- audio_config.ps1（复用 rvc_live 的调用方式） ----------------

# 声卡还原串行化：后台还原线程与下一步 _run_audio('apply') 互斥，避免同时改默认音频设备抢设备。
_restore_lock = threading.Lock()
# 历史写串行化：_persist_verify（UIA 校验）与 _restore_async（声卡还原）都是后台线程，
# 都做「读-改-写」历史文件，必须互斥，否则一个的写会被另一个覆盖。
_history_lock = threading.Lock()


def _run_audio(action: str) -> dict:
    if not AUDIO_PS1.exists():
        raise RuntimeError(f"缺 audio_config.ps1: {AUDIO_PS1}")
    # apply 与后台还原互斥：下一步切卡若撞上上一条还在后台还原，会同时改默认音频设备 →
    # 抢设备导致状态不确定。用 _restore_lock 挡住，apply 等还原收尾再切。
    acquired = False
    try:
        if action == "apply":
            _restore_lock.acquire()
            acquired = True
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", str(AUDIO_PS1), "-action", action],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace",
            # 关键：不弹控制台窗口。实测（2026-09-09）弹出的 PowerShell 蓝窗会
            # 短暂遮挡微信右下角，导致 _find_green_send 截图截到控制台 →
            # 浮层检测连续误判 → 自动发送整体降级为手动。
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"audio_config {action} 执行超时(120s)")
    finally:
        if acquired:
            _restore_lock.release()
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


class _PendingApply:
    """「切默认麦 → CABLE」后台预热任务（2026-09-10 端到端延迟优化）。

    实测 audio_config.ps1 apply 要 ~3s，串行排在 TTS(≈6s)/RVC 之后纯属白等。
    send_text 拿到发送锁后立刻起这个后台线程，让切卡与 TTS 合成并行；
    _do_send 真正要用麦克风前只 .result() 等一个尾差（apply < TTS 时实测 ≈0s），
    端到端省 ~3s。
    """

    def __init__(self) -> None:
        self._done = threading.Event()
        self._result: dict | None = None
        self._error: BaseException | None = None
        self._consumed = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="wechat-audio-apply")
        self._thread.start()

    def _run(self) -> None:
        try:
            self._result = _run_audio("apply")
        except BaseException as e:   # 原样转交 result()，交给 _do_send 的异常路径处理
            self._error = e
        finally:
            self._done.set()

    def result(self) -> dict:
        """阻塞到 apply 结束；成功返回结果 dict，失败原样抛出（_do_send 内调用一次）。"""
        self._consumed = True
        self._done.wait(timeout=130)          # _run_audio 自身 timeout=120
        if self._error is not None:
            raise self._error
        if self._result is None:
            raise RuntimeError("audio_config apply 未返回结果（后台任务超时？）")
        return self._result

    def abandon(self) -> None:
        """消费前的任务作废（TTS 失败 / 早退路径）：等线程收尾并还原声卡，
        绝不把系统默认麦留在 CABLE 上。已消费（_do_send 接手）时是安全的空操作。"""
        if self._consumed:
            return        # _do_send 已消费：还原由其异常/早退路径负责，别重复 restore
        self._consumed = True
        self._done.wait(timeout=130)
        try:
            _safe_restore()
        except Exception:
            pass


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
# 关键：开播瞬间打一行 PLAYING 并 flush。
# 子进程 import numpy/sounddevice/soundfile 要 2~3s，若主进程在点击录音后才同步等播放，
# 这段启动开销会被微信完整录进语音（实测 2.7s 音频 → 7" 消息）。主进程改为读到这行
# 再点录音，让静音头去覆盖微信录音的启动延迟，而不是被录成空白。
print("PLAYING", flush=True)
sd.play(np.concatenate([lead, data, tail]), sr, device=idx)
sd.wait()
print("DONE", flush=True)
'''


def _start_play_oneshot(wav: Path) -> subprocess.Popen:
    """一次性播放子进程（回退路径）：冷导入 numpy/sounddevice/soundfile 后播 wav。"""
    if not RVC_VENV_PY.exists():
        raise RuntimeError(f"找不到 RVC venv 解释器: {RVC_VENV_PY}（sounddevice 在该环境）")
    return subprocess.Popen(
        [str(RVC_VENV_PY), "-c", _PLAY_SCRIPT, str(wav), OUTPUT_DEVICE_KEYWORD,
         str(PLAY_LEAD_S), str(TAIL_S)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        creationflags=_NO_WINDOW,   # 播放期间不得弹出控制台遮挡微信
    )


def _start_play(wav: Path):
    """启动播放：优先复用常驻 worker（省 ~2.5-3s 冷导入），失败退回一次性子进程。

    返回对象同时满足 _wait_play_start（读 stdout 的 playing 行）与 _wait_play_done
    （wait 到 done）的契约——worker 走 _PlayWorkerHandle，一次性走真实 Popen。
    """
    if PLAY_WORKER_ENABLED:
        proc = _get_play_worker()
        if proc is not None:
            cmd = json.dumps({"wav": str(wav), "lead": PLAY_LEAD_S, "tail": TAIL_S})
            try:
                proc.stdin.write(cmd + "\n")
                proc.stdin.flush()
                return _PlayWorkerHandle(proc)
            except Exception as e:
                logger.warning("[wechat] 播放命令发送失败，回退一次性播放: %s", e)
                _stop_play_worker()
    return _start_play_oneshot(wav)


def _wait_play_start(proc, timeout: float = 40.0) -> bool:
    """阻塞等到子进程/worker 真正开始播放（stdout 出 playing 信号）。超时/崩溃返回 False。

    一次性子进程打 `PLAYING`；常驻 worker 打 `{"type":"playing"}`——都含 "play" 子串。
    子进程起不来时 stdout 会关闭，readline() 立即返回空串，不会卡死。
    """
    try:
        line = proc.stdout.readline() if proc.stdout else ""
        return bool(line) and "play" in (line or "").lower()
    except Exception:
        return False


def _wait_play_done(proc: subprocess.Popen, duration_s: float) -> None:
    """等播放子进程自然结束；异常兜底杀进程。"""
    try:
        proc.wait(timeout=duration_s + 30)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError("播放到 CABLE Input 超时")


def _play_to_cable(wav: Path, duration_s: float) -> None:
    if not RVC_VENV_PY.exists():
        raise RuntimeError(f"找不到 RVC venv 解释器: {RVC_VENV_PY}（sounddevice 在该环境）")
    try:
        proc = subprocess.run(
            [str(RVC_VENV_PY), "-c", _PLAY_SCRIPT, str(wav), OUTPUT_DEVICE_KEYWORD,
             str(LEAD_S), str(TAIL_S)],
            capture_output=True, text=True, timeout=duration_s + 30,
            creationflags=_NO_WINDOW,   # 同上：播放期间不得弹出控制台遮挡微信
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


# PostMessage 状态：按住期间的 (目标窗口, lparam)，松开时用同一窗口/坐标
_postmsg_ctx: tuple[int, int] | None = None
_rect_ctx: tuple[int, int, int, int] | None = None   # 录音中的窗口 rect（finish 找绿钮用）
_record_via: str | None = None    # 本次录音启动方式：postmsg / realclick / None
_exstyle_restore: tuple[int, int] | None = None  # (渲染子窗口 hwnd, 原扩展样式) 实时点击路径用
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x200, 0x201, 0x202
MK_LBUTTON = 0x0001


# ---------------- 渲染子窗口与 WS_EX_TRANSPARENT（借鉴 wechatauto-replica guia.py） ----------------

def _find_render_hwnd(main_hwnd: int) -> int:
    """枚举微信主窗口子窗口，找 Qt 自绘渲染层（类名前缀 MMUIRenderSubWindow*，取最大面积）。

    微信 4.x 聊天区是独立渲染子窗口（MMUIRenderSubWindow / MMUIRenderSubWindowHW 等
    变体），鼠标事件要落到它上面才算数。找不到时返回主窗口句柄（回退）。
    """
    import ctypes
    from ctypes import wintypes

    u = _user32()
    hits: list[tuple[int, int]] = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(h, _lp):
        buf = ctypes.create_unicode_buffer(256)
        u.GetClassNameW(h, buf, 256)
        if buf.value.startswith("MMUIRenderSubWindow"):
            r = wintypes.RECT()
            if u.GetWindowRect(h, ctypes.byref(r)):
                area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                if area > 0:
                    hits.append((area, h))
        return True

    u.EnumChildWindows(main_hwnd, WNDENUMPROC(cb), 0)
    if not hits:
        return main_hwnd
    hits.sort(key=lambda t2: t2[0], reverse=True)
    return hits[0][1]


def _exstyle_clear_transparent(hwnd: int) -> int | None:
    """临时去掉窗口的 WS_EX_TRANSPARENT 样式位，返回原扩展样式（本就没有则返回 None）。

    根因（wechatauto-replica 同款发现）：微信渲染子窗口设置
    WS_EX_LAYERED | WS_EX_TRANSPARENT，真实/SendInput 注入的鼠标点击会
    **穿透**到主窗口而到不了渲染层——这就是"模拟点击话筒没反应"的机理。
    摘掉该位后点击能被渲染窗口接收，用完必须恢复（保证画面正常合成）。
    """
    u = _user32()
    GWL_EXSTYLE, WS_EX_TRANSPARENT = -20, 0x20
    old = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if old & WS_EX_TRANSPARENT:
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, old & ~WS_EX_TRANSPARENT)
        time.sleep(0.05)
        return old
    return None


def _exstyle_restore_if_needed() -> None:
    """恢复渲染子窗口的扩展样式（realclick 路径结束时调用）。"""
    global _exstyle_restore
    if _exstyle_restore:
        hwnd, old = _exstyle_restore
        try:
            _user32().SetWindowLongW(hwnd, -20, old)
        except Exception:
            pass
        _exstyle_restore = None
        time.sleep(0.05)


def _postmsg_mouse(screen_point: tuple[int, int] | None, down: bool = False,
                   up: bool = False, target: int | None = None,
                   lparam: int | None = None) -> tuple[int, int] | None:
    """把鼠标按下/抬起直投微信窗口过程（绕过被过滤的注入输入队列）。

    down=True：定位光标下实际子窗口 → 客户区坐标 → MOUSEMOVE+LBUTTONDOWN，
    返回 (target, lparam) 供配对 UP 使用；up=True：向指定 target 发 LBUTTONUP。
    失败返回 None（调用方回退 SendInput）。
    """
    import ctypes
    from ctypes import wintypes
    u = _user32()
    try:
        if down and screen_point:
            pt = wintypes.POINT(*screen_point)
            win = u.WindowFromPoint(pt)
            if not win:
                return None
            cp = wintypes.POINT(*screen_point)
            u.ScreenToClient(win, ctypes.byref(cp))
            lp = (cp.y & 0xFFFF) << 16 | (cp.x & 0xFFFF)
            u.PostMessageW(win, WM_MOUSEMOVE, 0, lp)
            time.sleep(0.04)
            u.PostMessageW(win, WM_LBUTTONDOWN, MK_LBUTTON, lp)
            return win, lp
        if up and target is not None and lparam is not None:
            u.PostMessageW(target, WM_LBUTTONUP, 0, lparam)
            return target, lparam
    except Exception:
        return None
    return None


def _mic_point(rect: tuple[int, int, int, int],
               offset_x: int = MIC_OFFSET_X, offset_y: int = MIC_OFFSET_Y) -> tuple[int, int]:
    """由窗口 rect 计算话筒图标坐标（输入框右下角、发送按钮左侧，可配置偏移）。"""
    left, top, right, bottom = rect
    return (right + offset_x, bottom + offset_y)


def _ncc_match(img: np.ndarray, tpl: np.ndarray) -> tuple[tuple[int, int] | None, float]:
    """灰度归一化互相关模板匹配（numpy 纯实现，零 cv2 依赖）。

    实测坑：下采样会毁掉细线条图标（话筒线条仅 1-2px，隔行抽点匹配分数从 1.0 掉到 0.62），
    必须 ds=1 全精度。搜索区控制在右下 340x150 内，全精度匹配 <1s。
    返回 (窗口内模板左上角坐标, 分数)。
    """
    ih, iw = img.shape
    th, tw = tpl.shape
    if ih < th or iw < tw:
        return None, -1.0
    t = tpl.astype(np.float64)
    t0 = t - t.mean()
    t_norm = np.sqrt((t0 ** 2).sum())
    if t_norm == 0:
        return None, -1.0
    best_score, best_pos = -2.0, None
    for y in range(ih - th + 1):
        for x in range(iw - tw + 1):
            win = img[y:y + th, x:x + tw].astype(np.float64)
            w0 = win - win.mean()
            denom = np.sqrt((w0 ** 2).sum()) * t_norm
            if denom < 1e-6:
                continue
            score = float((w0 * t0).sum() / denom)
            if score > best_score:
                best_score, best_pos = score, (x, y)
    return best_pos, best_score


# 话筒图标模板（从真实微信窗口截图裁剪；缺失/不匹配时回退固定偏移）
MIC_TEMPLATE = cfg.ROOT / "m2_server" / "assets" / "wechat_mic_template.png"
TEMPLATE_DIR = cfg.ROOT / "m2_server" / "assets"


def _mic_templates() -> list[Path]:
    """所有话筒模板（assets/wechat_mic_template*.png）。

    实测（2026-09-09）：微信输入框右下角的图标组会随界面状态左右漂移约 20px，
    且图标渲染细节随之变化——单一模板换一种界面状态就掉到 0.33 分。
    因此支持多模板，取最高分；命中率不足时由 _mic_point 固定偏移兜底。
    """
    files = sorted(TEMPLATE_DIR.glob("wechat_mic_template*.png"))
    return files or [MIC_TEMPLATE]


def _find_mic_icon(rect: tuple[int, int, int, int], thresh: float = 0.75) -> tuple[int, int] | None:
    """截图微信窗口右下角，模板匹配定位话筒图标。返回屏幕坐标；失败返回 None。"""
    try:
        from PIL import Image as PILImage
        from PIL import ImageGrab
        l, t, r, b = rect
        box = (max(0, r - 340), max(0, b - 150), r, b)   # 搜索区：右下 340x150
        shot = np.asarray(ImageGrab.grab(bbox=box).convert("L"))
        best: tuple[float, tuple[int, int], tuple[int, int]] | None = None
        for tpl_path in _mic_templates():
            try:
                tpl_img = PILImage.open(tpl_path).convert("L")
            except Exception:
                continue
            pos, score = _ncc_match(shot, np.asarray(tpl_img))
            if pos is None:
                continue
            if best is None or score > best[0]:
                best = (score, pos, tpl_img.size)
        if best is None or best[0] < thresh:
            return None
        score, pos, (tw, thh) = best
        cx = box[0] + pos[0] + tw // 2    # 匹配位置 + 模板中心
        cy = box[1] + pos[1] + thh // 2
        return cx, cy
    except Exception:
        return None


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    import ctypes
    from ctypes import wintypes
    rect = wintypes.RECT()
    if not _user32().GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect 失败")
    return rect.left, rect.top, rect.right, rect.bottom


def _ensure_onscreen(hwnd: int) -> None:
    """把微信窗口挪回所在显示器的工作区（防止底边超出屏幕/被任务栏遮挡话筒）。

    实测坑：窗口底边超出屏幕时，自动隐藏的任务栏弹出会正好盖住输入框右下角，
    鼠标点击会点到任务栏上。挪窗用 SetWindowPos，保持窗口尺寸不变。
    """
    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    u = _user32()
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    hmon = u.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
    if not hmon or not u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return
    l, t, r, b = _window_rect(hwnd)
    wa = mi.rcWork
    dx = dy = 0
    # 优先级：底边 > 右边 > 顶边/左边。
    # 话筒在窗口右下角，底边被（自动隐藏的）任务栏压住会直接导致点击失效；
    # 而最大化窗口的 top 常为负值（Windows 把边框裁到屏外），此时若为了
    # 「顶边不溢出」把窗口往下推，反而会把底边推出屏幕 —— 实测踩过（2026-09-09）。
    if b > wa.bottom:
        dy = wa.bottom - b
    elif t < wa.top and b <= wa.top:      # 仅当窗口整体在上边界之外才拉回
        dy = wa.top - t
    if r > wa.right:
        dx = wa.right - r
    elif l < wa.left and r <= wa.left:    # 仅当窗口整体在左边界之外才拉回
        dx = wa.left - l
    if dx or dy:
        # SWP_NOSIZE(0x1) | SWP_NOZORDER(0x4) | SWP_NOACTIVATE(0x10)
        u.SetWindowPos(hwnd, 0, l + dx, t + dy, 0, 0, 0x1 | 0x4 | 0x10)
        time.sleep(0.15)


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


# 录音浮层判定：灰度差分阈值。
# 实测标定（2026-09-09）：静态界面相邻快照差分恒为 0.000；浮层弹出后稳定在
# 2.329 —— 信噪比极佳，取 1.0 留 5 倍余量。
# 注意：不能用「绿色发送钮是否存在」判据——微信输入框的绿色「发送(S)」按钮是
# 常驻的，非录音态也能匹配到，会导致假阳性（以为在录音，实际录到静音）。
OVERLAY_DIFF = float(os.environ.get("VM_WECHAT_OVERLAY_DIFF", "1.0"))
_overlay_baseline: np.ndarray | None = None


def _grab_bottom_gray(rect: tuple[int, int, int, int]) -> np.ndarray:
    """截取窗口右下角（浮层出现的位置）灰度图，用于差分比较。

    高度 140 与 _find_green_send 同一区域：浮层只在底部 140 高内变化（× 取消 +
    录音波纹 + ↑绿钮），上方是聊天内容保持不动——若 box 过高（260）混合聊天
    像素会把信号稀释到 0.1~0.2 量级，触发不了阈值（2026-09-09 实测）。
    """
    from PIL import ImageGrab
    l, t, r, b = rect
    u = _user32()
    sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    box = (max(0, min(r, sw) - 560), max(0, min(b, sh) - 140), min(r, sw), min(b, sh))
    return np.asarray(ImageGrab.grab(bbox=box).convert("L")).astype(np.int16)


def _snapshot_overlay_baseline(rect: tuple[int, int, int, int]) -> None:
    """按下话筒前存一张基线，供 _wait_record_overlay 做差分。"""
    global _overlay_baseline
    try:
        _overlay_baseline = _grab_bottom_gray(rect)
    except Exception as e:      # 截图失败不该阻断发送
        logger.warning("[wechat] 基线快照失败，浮层检测将退化为绿色按钮判据: %s", e)
        _overlay_baseline = None


def _wait_record_overlay(rect: tuple[int, int, int, int], timeout: float = 6.0) -> bool:
    """轮询等待录音浮层出现。

    判据优先级（2026-09-10 方案 A）：
      1. **UIA**：`mmui::ChatVoiceRecordView` 是否存在——结构化、不受渲染/配色影响
      2. 像素兜底：绿钮 HSV（微信绿 (18,199,125)，>50 像素）——UIA 不可用时用
    两条判据并行轮询，谁先命中算谁，任一可用即返回，互不阻塞。
    """
    deadline = time.time() + timeout
    use_uia = _uia_ready()
    while time.time() < deadline:
        if use_uia:
            try:
                if _uia.overlay_exists():
                    return True
            except Exception as e:
                logger.debug("[wechat] UIA 浮层检测失败，本轮到像素: %s", e)
                use_uia = False
        if _find_green_send(rect, retries=1):
            return True
        time.sleep(0.25)
    return False


def _trigger_record(ui: dict | None = None) -> None:
    """开始录制语音消息。

    mic 路径（默认）：摘 WS_EX_TRANSPARENT + SendInput **单击**语音按钮，微信
    进入持续录音态并弹出浮层（× / 波形 / ↑绿钮）。结束由 _finish_record 点
    绿钮 ↑ 发送（录制时长 = 实际经过时间，不会 60s 截断）。
    2026-09-10 真机实测：这个圆形语音按钮是"单击开始持续录音"，保持按住不松
    反而起不了浮层——旧实现（按住等松手）已废弃。

    alt 路径：SendInput 按下 Alt 键（_finish_record 再抬起）。

    ui：可选，_do_send 并行准备阶段算好的 {hwnd, rect, mic}，命中则跳过
        前台化/找话筒（与播放子进程冷导入并行，省 ~1s）。
    """
    global _postmsg_ctx, _rect_ctx, _record_via, _exstyle_restore
    _postmsg_ctx = None
    _rect_ctx = None
    _record_via = None
    if RECORD_METHOD == "mic":
        if ui and ui.get("hwnd"):
            hwnd = ui["hwnd"]
            rect = ui["rect"]
            cached_mic = ui.get("mic")
        else:
            hwnd = _foreground_wechat()
            _ensure_onscreen(hwnd)                 # 防止窗口底边超屏被任务栏遮挡
            rect = _window_rect(hwnd)
            cached_mic = None
        _rect_ctx = rect
        # 首选 UIA 点击（2026-09-10 方案 A）：UIA 走 COM 调用，不经系统输入队列，
        # 天然不受 WS_EX_TRANSPARENT 点击穿透影响，也不用摘样式位、不依赖坐标。
        if _uia_ready():
            try:
                if _uia.click_voice_button() and _wait_record_overlay(rect, timeout=5.0):
                    _record_via = "uia"
                    return
            except Exception as e:
                logger.debug("[wechat] UIA 点击语音按钮失败，回退像素链路: %s", e)
        # 降级：摘 WS_EX_TRANSPARENT + SendInput 单击语音按钮
        point = cached_mic or (_find_mic_icon(rect) or _mic_point(rect))
        # 摘样式（必做）+ SendInput 单击语音按钮
        render = _find_render_hwnd(hwnd)
        old_ex = _exstyle_clear_transparent(render)
        if old_ex is not None:
            _exstyle_restore = (render, old_ex)
        _mouse_move_abs(*point)
        time.sleep(0.12)
        # 单击（DOWN+UP 短按）：2026-09-10 真机实测——这个圆形语音按钮是
        # "单击开始持续录音、点 ↑绿钮结束发送"，保持按住不松反而起不了浮层。
        _mouse_left(True)
        time.sleep(0.06)
        _mouse_left(False)
        if _wait_record_overlay(rect, timeout=8.0):
            _record_via = "realclick"
            return
        # 浮层未出现：best-effort 还原样式 + 抛错走降级
        try:
            _mouse_left(False)
        except Exception:
            pass
        _exstyle_restore_if_needed()
        raise RuntimeError("微信录音未能启动（浮层未出现），请稍后重试")
    _foreground_wechat()
    vk, scan = _record_key_code()
    _send_input_kb(vk, scan, False)


def _find_green_send(rect: tuple[int, int, int, int], retries: int = 4) -> tuple[int, int] | None:
    """录音浮层上的绿色 ↑ 发送按钮：按微信绿 (18,199,125) 色域找质心。

    浮层渲染有延迟，重试几次；找不到返回 None（调用方点 × 取消兜底）。
    """
    from PIL import ImageGrab
    l, t, r, b = rect
    # 取域必须 clamp 到屏幕内：bbox 超出屏幕时 PIL 会把屏外部分填黑，
    # 若绿钮正好落在填黑区就永远检测不到（最大化窗口底边常溢出几像素）。
    u = _user32()
    sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    box = (max(0, min(r, sw) - 560), max(0, min(b, sh) - 140), min(r, sw), min(b, sh))
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    for _ in range(retries):
        img = np.asarray(ImageGrab.grab(bbox=box).convert("RGB")).astype(int)
        green = (img[:, :, 1] > 150) & (img[:, :, 0] < 120) & \
                (img[:, :, 2] > 80) & (img[:, :, 2] < 180)
        ys, xs = np.nonzero(green)
        if len(xs) > 50:
            return int(xs.mean()) + box[0], int(ys.mean()) + box[1]
        time.sleep(0.4)
    return None


def _finish_record() -> bool:
    """结束录音并让微信发送/丢弃。

    mic 路径：trigger 单击语音按钮进入持续录音态，这里点浮层绿钮 ↑ 发送
    （2026-09-10 真机实测：单击起浮层并持续录音 → 点绿钮发送，录制时长 =
    实际经过时间，不会再 60s 截断）。找不到绿钮则点 × 取消，避免挂起录音。

    alt 路径：SendInput 抬起 Alt 键。
    返回 True=已发送，False=已取消。
    """
    global _postmsg_ctx, _rect_ctx, _record_via, _exstyle_restore
    try:
        if RECORD_METHOD == "mic":
            rect = _rect_ctx or _window_rect(_find_wechat_hwnd())
            time.sleep(TAIL_S)   # 尾音缓冲（等 wav 尾音真正录进去）
            # 发送钮定位：UIA 结构化矩形优先，绿钮 HSV 兜底（2026-09-10 方案 A）。
            send_pt = None
            if _uia_ready():
                try:
                    send_pt = _uia_center(_uia.send_button_rect())
                except Exception as e:
                    logger.debug("[wechat] UIA 发送钮定位失败，回退绿钮: %s", e)
            send_pt = send_pt or _find_green_send(rect)
            if send_pt:
                _mouse_move_abs(*send_pt)
                time.sleep(0.12)
                _mouse_left(True)
                time.sleep(0.06)
                _mouse_left(False)   # 点 ↑ 绿钮 = 发送
                return True
            # 没找到绿钮（浮层异常）：点 × 取消，避免挂起录音
            cancel_pt = _cancel_point(rect)
            if cancel_pt:
                _mouse_move_abs(*cancel_pt)
                time.sleep(0.12)
                _mouse_left(True)
                time.sleep(0.06)
                _mouse_left(False)
            return False
        # alt 路径：SendInput Alt 抬起
        vk, scan = _record_key_code()
        _send_input_kb(vk, scan, True)
        return True
    finally:
        _postmsg_ctx = None
        _rect_ctx = None
        _record_via = None
        _exstyle_restore_if_needed()


def _cancel_point(rect: tuple[int, int, int, int]) -> tuple[int, int] | None:
    """录音浮层 × 取消按钮位置。

    优先 UIA（`mmui::XButton '取消'`，实测 (891,1519,933,1561)）；拿不到再退回
    老的硬编码偏移（话筒原位置左侧 218px，2026-09-09 曾算偏过 40px）。
    """
    if _uia_ready():
        try:
            pt = _uia_center(_uia.cancel_button_rect())
            if pt:
                return pt
        except Exception as e:
            logger.debug("[wechat] UIA 取消钮定位失败，回退偏移: %s", e)
    mx, my = _mic_point(rect)
    return (mx - 218, my)


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


class SendTextReq(BaseModel):
    text: str
    voice_id: str = ""        # TTS 参考音色（决定语气/韵律，"怎么说"）
    rvc_voice: str = ""       # RVC 音色（决定"谁在说"）；留空=不换声，音色会明显不像
    pitch: int = 0
    index_rate: float = 0.5


@router.post("/send_text")
def send_text(req: SendTextReq):
    """一键：文字 → TTS → RVC 换声 → 全自动发成微信语音消息。

    桌宠「合成并发送」走的就是这个接口。链路里 **RVC 是音色的唯一来源**——
    TTS 零样本克隆复现不了袋鼠音色（2026-08-31 用户 A/B 亲耳判定），
    所以 rvc_voice 留空会得到一条"普通播音腔"，不是 bug。

    延迟优化（2026-09-10）：切默认麦→CABLE（~3s）已提前到与 TTS 合成（~6s）
    并行（_PendingApply），_do_send 用麦克风前只等尾差，端到端省 ~3s。
    """
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    if not _send_lock.acquire(blocking=False):
        raise HTTPException(409, "已有一次微信语音发送在进行中，请等它结束")
    apply_task: _PendingApply | None = None
    try:
        _t0 = time.time()
        # 切卡 ~3s 别串行等在 TTS 后面：立刻起后台预热与合成并行（见 _PendingApply）。
        # 合成/组装任何一步失败，下面的 except 会 abandon() 兜底还原声卡。
        apply_task = _PendingApply()
        from tts_api import synth_wav
        wav, duration_s, _vid = synth_wav(req.text, req.voice_id)
        steps = [f"合成: {wav.name}（{duration_s:.1f}s，voice={_vid or '默认'}，用时 {time.time()-_t0:.1f}s）"]
        # 没显式给 rvc_voice 时，按 voicebank id → RVC 实验名的约定推一个
        # （kangaroo → kangaroo_v2）。推不到就照发，并在 steps 里说清楚音色会不像。
        rvc_voice = req.rvc_voice
        if not rvc_voice:
            from rvc_convert import resolve_rvc_voice
            rvc_voice = resolve_rvc_voice(_vid or req.voice_id) or ""
        if rvc_voice:
            from rvc_convert import rvc_convert as _rvc
            _t1 = time.time()
            wav = _rvc(wav, rvc_voice, req.pitch, req.index_rate)
            steps.append(f"RVC 换声 → {rvc_voice}（用时 {time.time()-_t1:.1f}s）")
        else:
            steps.append("⚠ 没找到对应 RVC 音色，未换声（会是普通播音腔）")
        _t2 = time.time()
        res = _do_send(SendVoiceReq(wav=wav.name), pre_apply=apply_task)
        steps.append(f"微信录制发送（用时 {time.time()-_t2:.1f}s）")
        # _do_send 成功时返回 dict，失败可能返回 JSONResponse
        if isinstance(res, dict):
            res["steps"] = steps + list(res.get("steps", []))
            res["wav"] = wav.name
        else:
            # 早退路径（404 等）没走到 pre_apply 消费点，必须收尾还原声卡
            if apply_task is not None:
                apply_task.abandon()
        return res
    except HTTPException:
        if apply_task is not None:
            apply_task.abandon()
        raise
    except Exception as e:
        if apply_task is not None:
            apply_task.abandon()
        raise HTTPException(status_code=500, detail=f"一键发送失败: {e}")
    finally:
        _send_lock.release()


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


def _uia_verify_sent(before_msg: str | None, expect_s: float,
                     before_count: int = -1) -> list[str]:
    """发送后读 UIA 里最新语音消息，校验真的发出去了 + 时长正常。

    这是"ok≠真发出"那道坑的自动化防线（2026-09-10）：以前只能靠截图肉眼看气泡，
    现在直接读 `mmui::ChatVoiceItemView` 的名称（形如 `语音15"秒`）。

    判据用**消息条数**而不是文本（2026-09-10 修）：微信只暴露形如 `语音15"秒` 的名称，
    连续两条时长相同的语音文本完全一样，纯字符串对比会把"发送成功"误报成"没发出去"。
    """
    msg = None
    count = -1
    for _ in range(6):
        try:
            msg = _uia.latest_voice_message()
            count = len(_uia.voice_messages() or [])
        except Exception:
            msg, count = None, -1
        # 条数变多 = 确定新增了一条；文本不同 = 也确定（兜底）
        if msg and ((before_count >= 0 and count > before_count) or msg != before_msg):
            break
        time.sleep(0.5)
    if not msg:
        return ["UIA 校验：读不到语音消息（跳过）"]

    added = before_count >= 0 and count > before_count
    if not added and msg == before_msg:
        return [f"⚠ UIA 校验：最新语音仍是 {msg}（条数 {before_count}→{count}，本次可能没发出去）"]

    secs = _uia.duration_from_message(msg)
    # 注意：UIA 只暴露聊天可视区里的消息（虚拟列表），条数经常恒定不变，
    # 所以"条数没涨"不等于没发出去，别把它当失败判据。
    note = (f"条数 {before_count}→{count}，确认新增" if added
            else f"条数 {before_count}→{count}，UIA 仅暴露可视区故不增属正常")
    out = [f"UIA 校验：最新语音 {msg}（{note}）"]
    if secs is not None and expect_s >= 3 and secs > max(expect_s * 3, expect_s + 20):
        out.append(f"⚠ 时长异常（{secs:.0f}s 远大于预期 {expect_s:.1f}s），疑似 60s 截断复发")
    elif secs is not None and expect_s >= 3 and secs > expect_s + 4:
        # 静音头尾吞掉太多：提示而不是判失败
        out.append(f"提示：录得比音频长 {secs - expect_s:.1f}s（首尾静音），可下调 LEAD_S/TAIL_S")
    return out


def _do_send(req: SendVoiceReq, pre_apply: _PendingApply | None = None):
    """执行一次微信语音自动发送。pre_apply：send_text 预热的切卡任务（与 TTS 并行）。

    传了 pre_apply 就只 .result() 等它收尾（不再同步跑第二次 apply）；
    不传（直连 /send_voice、tools/*）保持原地同步切卡，行为不变。
    """
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

    # UIA 预热（方案 A）：激活失败不影响主流程，后面各环节自动回退像素链路
    uia_active = _uia_ready()
    steps.append("UIA 结构化访问就绪" if uia_active else "UIA 不可用（走像素链路）")
    before_msg = None
    before_count = -1
    if uia_active:
        try:
            before_msg = _uia.latest_voice_message()
            before_count = len(_uia.voice_messages() or [])
        except Exception:
            before_msg, before_count = None, -1

    restored = False
    proc = None          # 后台播放进程（异常路径要能 kill）
    _tw = time.time()
    try:
        # 2) 默认麦克风 → CABLE Output（微信从这里录；apply 自动备份原设备）。
        #    send_text 已把切卡提前到与 TTS 并行（pre_apply），这里只等尾差（实测 ≈0s）；
        #    没有预热任务时保持原地同步切卡。
        _ta = time.time()
        if pre_apply is not None:
            pre_apply.result()
            steps.append(f"[{time.time()-_tw:.1f}s] 麦克风已切到 CABLE Output"
                         f"（切卡已与 TTS 并行，此处仅等 {time.time()-_ta:.1f}s）")
        else:
            _run_audio("apply")
            steps.append(f"[{time.time()-_tw:.1f}s] 麦克风已切到 CABLE Output")

        # 3) 起播放（常驻 worker 复用 / 一次性子进程）。冷导入若发生，与下面的 UI 准备并行。
        _t_play = time.time()
        proc = _start_play(wav)

        # --- 并行：播放子进程冷导入（worker 已预热则≈0）期间，把微信前台化 + 找话筒算好 ---
        # 这段 UI 准备 ~0.5-1s，原本串行排在切卡/导入之后纯等；现在与播放导入重叠，省 ~1s。
        _ui: dict = {}

        def _prep_ui() -> None:
            try:
                h = _foreground_wechat()
                _ensure_onscreen(h)                      # 防止窗口底边超屏被任务栏遮挡
                r = _window_rect(h)
                _ui["hwnd"] = h
                _ui["rect"] = r
                _ui["mic"] = _find_mic_icon(r) or _mic_point(r)
            except Exception as e:
                _ui["err"] = e

        _prep_t = threading.Thread(target=_prep_ui, daemon=True)
        _prep_t.start()
        play_ready = _wait_play_start(proc)             # 主线程等导入/ready（与 _prep_ui 并行）
        _prep_t.join()
        if play_ready:
            steps.append(f"[{time.time()-_tw:.1f}s] 播放就绪（冷导入 {time.time()-_t_play:.1f}s，"
                         f"已与 UI 准备并行）")
        else:
            steps.append("⚠ 未等到播放开始信号，仍按原计划录音（开头可能被削）")

        # 4) 点语音按钮开始录制（UI 已并行准备好，直接复用；准备失败则退回原路径重算）
        _trigger_record(ui=_ui if not _ui.get("err") else None)
        if RECORD_METHOD == "mic":
            steps.append(f"[{time.time()-_tw:.1f}s] " + ("UIA 已点击语音按钮，开始录音" if _record_via == "uia"
                         else "已点击话筒图标，开始录音"))
        else:
            steps.append(f"[{time.time()-_tw:.1f}s] 已按住 {RECORD_KEY.upper()} 开始录音")

        # 5) 等 wav 真正播完（播完 = 子进程退出 / worker 回 done）
        _wait_play_done(proc, duration)
        steps.append(f"[{time.time()-_tw:.1f}s] 已播放 {duration:.1f}s 到微信录音")

        # 6) 结束录音并发送
        #    播放脚本尾部已自带 TAIL_S 静音，这里只留极小缓冲给声卡驱动（再多就是白录空白）
        time.sleep(0.15)
        via = _record_via
        sent = _finish_record()
        _hist_appended = False
        if sent:
            steps.append(f"[{time.time()-_tw:.1f}s] " + {
                "uia": "UIA 点击语音按钮录音 → 已点发送钮，语音已发送",
                "realclick": "已点击语音按钮 → 已点发送钮，语音已发送",
                "postmsg": "已点浮层发送按钮，语音已发送",
            }.get(via, "语音已发送"))
            # 发送已成功：先落历史（后台写回校验/还原结果都依赖它）
            _append_history(wav, duration, "ok")
            _hist_appended = True
            # UIA 校验只是安全网，放后台线程不阻塞返回（省 ~0.5-3s）
            if uia_active:
                threading.Thread(
                    target=lambda: _persist_verify(_uia_verify_sent(before_msg, duration, before_count)),
                    daemon=True,
                ).start()
                steps.append("UIA 发送后校验：后台线程进行中（结果写入发送历史）")
            # 声卡还原 ~3s，放后台线程：下一步切卡由 _restore_lock 等它收尾，不抢设备，
            # 也不阻塞本次返回（省 ~3s 同步等待）。
            threading.Thread(target=_restore_async, daemon=True).start()
            steps.append(f"[{time.time()-_tw:.1f}s] 声卡还原已交后台线程（不阻塞返回，约 3s）")
        else:
            steps.append("未找到发送按钮，已取消录音（本次未发送）")
            restored, restore_err = _safe_restore()
            return {"ok": True, "outcome": "cancelled", "method": RECORD_METHOD,
                    "wav": wav.name, "duration_s": round(duration, 1),
                    "steps": steps, "restored": restored, "restore_error": restore_err,
                    "_history": _append_history(wav, duration, "cancelled")}
    except Exception as exc:
        # 失败也要：⓪掐掉后台播放（否则会一直往 CABLE 灌声音）
        #            ①松开录音键/鼠标（防止按住不放卡死）②还原声卡（reset 兜底）
        try:
            if proc is not None and proc.poll() is None:
                proc.kill()
        except Exception:
            pass
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

    # 6) 声卡还原已在上一步交后台线程（_restore_async），此处不阻塞直接返回。
    #    立即返回的 restored 记为 None（pending），最终结果由后台线程写回发送历史。
    return {"ok": True, "outcome": "ok", "method": RECORD_METHOD, "wav": wav.name, "duration_s": round(duration, 1),
            "steps": steps, "restored": None,
            "_history": (True if _hist_appended else _append_history(wav, duration, "ok"))}


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
