"""微信语音消息发送（桌宠右键入口）。

思路（全程不 Hook、不注入微信，只模拟人手操作）：
    0. **重启微信**（必要时）——理由见下方「为什么必须重启微信」
    1. audio_config.ps1 apply  —— 系统默认麦克风切到 CABLE Output（自动备份原设备）
    2. 前台化微信聊天窗口，触发微信 4.1.9+ 的官方「发送语音消息」开始录音：
       - RECORD_METHOD=mic（默认，2026-09-10 真机改版）：**单击**输入框右下角
         话筒图标即进入持续录音态并弹出浮层（旧文档写"长按说话、松开自动发送"，
         那个交互在这个版本上不成立——保持按住反而不出浮层）；
         鼠标注入接受度高于键盘
       - RECORD_METHOD=alt：按住 Alt 键（SendInput 带扫描码，比旧 keybd_event 更真实；
         但实测部分微信版本会忽略软件注入的键盘输入）
       微信从系统默认麦克风采集，此刻即 CABLE Output
    3. 用 RVC venv 的 sounddevice 把 TTS 产物 wav 直接播进 CABLE Input
       （微信同步从 CABLE Output 录到的就是这段声音），首尾各垫静音防掐头去尾
    4. 结束录音并发送：点浮层绿色 ↑ 钮（mic 路径，录制时长 = 实际经过时间，
       不会 60s 截断）；alt 路径则是抬起 Alt 键。找不到 ↑ 钮一律点 × 取消，
       绝不留下挂起的录音（单条最长 60s）
    5. audio_config.ps1 restore —— 还原原声卡

为什么必须重启微信（2026-09-11 实测，证据链见 `docs/犯错档案-微信.md` §2.15）：
    微信在**进程启动时**就绑定好采集设备，之后改 Windows 默认麦克风对它不热生效。
    症状极具误导性：往 CABLE Input 灌满幅信号（peak 0.763），微信却录到安静房间声，
    发出去的语音听着是静音，而 CABLE 驱动本身完全无辜（三种 API × 六种采样率全通）。
    所以「切卡」与「录音」之间必须夹一次微信重启，顺序是硬约束：
        **杀微信 → 切卡 → 拉起微信 → 录音**
    是否重启由 VM_WECHAT_RESTART 决定（**2026-09-17 起默认 0 = 绝不重启微信**，
    用户拍板：强杀会退回登录界面；auto 需显式设置才启用，见 _need_wechat_restart）；
    重启要 10~30s，故与 TTS 合成并行（_PendingRecordingEnv）。

依赖：主环境零新增（ctypes + subprocess，psutil 可选）；播放走 D:/RVC/.venv 的
sounddevice，与 cascade_stream/offline_vc 同一约定。进程/窗口操作用 wechat_proc。

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

import contextlib

import config as cfg
import numpy as np
import session_out
import soundfile as sf
import wechat_proc as wproc  # 进程/窗口底层操作（重启微信链路）
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
# 输出目标（cascade_stream 同款关键词）：wav 播进 CABLE 的「渲染端」，微信从 CABLE 的
# 「采集端」录到这段声音。注意：VB-Audio Virtual Cable 的渲染端在不同系统叫法不同——
# 英文 Windows 是 "CABLE Input (VB-Audio Virtual Cable)"，中文 Windows 被本地化成
# "扬声器 (VB-Audio Virtual Cable)"。两者都含 "VB-Audio Virtual Cable" 这个常量串，
# 且都带输出通道（采集端 "CABLE Output (...)" 输出通道为 0 会被 max_output_channels>0 过滤掉），
# 故用这个常量串做关键字可中英文通吃，避免写死 "CABLE Input" 在本机匹配不到设备。
# `|` 后面的端点词是**兜底候选**：英文系统下 MME 会把名字截断到 31 字符
# （"CABLE Input (VB-Audio Virtual C"），完整驱动名反而匹配不上（2026-09-19 级联事故）。
# 消费方 play_worker._resolve_device 按序尝试两个候选；_device_keyword() 只取第一个。
OUTPUT_DEVICE_KEYWORD = os.environ.get(
    "VM_LIVE_OUTPUT_DEVICE", "VB-Audio Virtual Cable|CABLE Input"
)
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
LEAD_S = float(os.environ.get("VM_WECHAT_LEAD_S", "0.35"))  # 按住后等待录音开始
TAIL_S = float(os.environ.get("VM_WECHAT_TAIL_S", "0.3"))  # 播完后的尾巴静音（松开前）
# 自动发送专用静音头：开播后先垫这段再出人声，用来盖住「点按钮→微信真正开始录」的延迟。
# 太小会削掉开头第一个字，太大会在语音前留下空白。0.8s 是实测折中。
PLAY_LEAD_S = float(os.environ.get("VM_WECHAT_PLAY_LEAD_S", "0.8"))
# 自动按键流程失败时是否自动降级为「引导式手动发送」（播放到 CABLE + 用户自己按住说话）
AUTO_FALLBACK = os.environ.get("VM_WECHAT_AUTO_FALLBACK", "1") == "1"

# ---- 长文分段发送的预算（2026-09-23，见 docs/微信语音-长文分段发送方案.md §2）----
# 微信语音单条硬上限 60s，到点自动结束并发送 —— **平台限制，无解**。
# 所以问题不是"能不能超过"，而是"怎么保证整段话都发出去"：把「一次发一条 wav」
# 改成「一次发一批 ≤MAX_CHUNK_S 的 wav」，切分点落在句子/静音处。
#
# 现状最要命的不是截断本身，而是**没有人在发之前知道它有多长**：_record_and_send 第 5 步
# 是 _wait_play_done(proc, duration) —— 音频多长微信就录多长，到 60s 被切掉，剩下的丢了。
# _uia_verify_sent 里那句"疑似 60s 截断复发"是**事后报警**；本组常量把它前移成**事前预算**。
MAX_MSG_S = 60.0  # 微信硬上限，不可调
# 单条可承载的音频预算 = 60 − PLAY_LEAD_S(0.8) − TAIL_S(0.3) − FINISH_LAG(0.3) − SAFETY(4.6)。
# SAFETY_S 的四项构成（别省，这段余量买的是"绝不静默截断"）：UIA 秒数整数向上取整 ±1.0；
# TTS 语速随句子/音色波动 1.5；声卡驱动缓冲与收尾抖动 1.1；微信侧自身计数余量 1.0。
FINISH_LAG_S = 0.3  # time.sleep(0.15) + 点发送钮的延迟
SAFETY_S = float(os.environ.get("VM_WECHAT_SAFETY_S", "4.6"))
MAX_CHUNK_S = float(
    os.environ.get(
        "VM_WECHAT_MAX_CHUNK_S", f"{MAX_MSG_S - 0.8 - 0.3 - FINISH_LAG_S - SAFETY_S:.1f}"
    )
)  # 默认 54.0
# 下限：微信语音最短 1s（说话时间太短会被拒）。短于此的尾段并入上一段，
# 能并则并、不能并就补静音 —— 而不是发一条 1.2 秒的。
MIN_CHUNK_S = float(os.environ.get("VM_WECHAT_MIN_CHUNK_S", "2.5"))
# 段间停顿：等录音浮层消失之后再等这一小段，避免衔接太生硬。
BATCH_GAP_S = float(os.environ.get("VM_WECHAT_BATCH_GAP_S", "0.8"))
# 段内句子之间拼接的静音（pack_chunks 的 gap_s 默认值从这里来，单位秒）
SENT_GAP_S = float(os.environ.get("VM_WECHAT_SENT_GAP_S", "0.3"))
# 人声与音效之间垫的静音（`[爆炸]` 插进来时）。0.15s 是"能听出是两件事、又不觉得断"：
# 不垫的话音效会**咬住**最后一个字的尾音（TTS 尾巴常带一点共鸣），听着像混在一起；
# 垫太长（>0.4s）在微信里会显得中间空了一拍。它也算进预算（见 _split_for_budget）。
SFX_GAP_S = float(os.environ.get("VM_WECHAT_SFX_GAP_S", "0.15"))

# ---- 录音前的微信重启（2026-09-11 引入，2026-09-17 起默认关闭）----
# 微信绑定采集设备是在**进程启动时**，改默认麦克风对它不热生效 → 必须重启它才会
# 重新枚举到 CABLE Output。但强杀微信（taskkill /F）副作用是退回登录界面要求重新登录，
# 用户 2026-09-17 拍板：**绝不杀微信**，故默认值改为 0。取值（**调用时**读，便于测试 monkeypatch）：
#   0(默认) = 从不重启（旧行为；微信已绑在 CABLE 上时录音正常，否则可能录到物理麦）
#   auto    = 按微信自己的遥测证据判断，只在确实需要时才重启（省 10~30s）
#   1       = 每次发送都重启（最稳，最慢，会强杀微信）
RESTART_MODE_ENV = "VM_WECHAT_RESTART"
# 等微信主窗口就绪的上限（含用户手动扫码登录的时间）
RESTART_WAIT_S = float(os.environ.get("VM_WECHAT_RESTART_WAIT_S", "90"))
# WM_CLOSE 给微信体面退出的宽限（微信默认"关闭=收进托盘"，别指望它，给短点）
RESTART_KILL_GRACE_S = float(os.environ.get("VM_WECHAT_RESTART_KILL_GRACE_S", "2.5"))

_send_lock = threading.Lock()
_play_proc = None  # 正在向 CABLE 播放的子进程，供 /stop_play 中止
PLAY_WORKER_ENABLED = os.environ.get("VM_WECHAT_PLAY_WORKER", "1") == "1"
PLAY_WORKER_SCRIPT = cfg.ROOT / "m2_server" / "play_worker.py"
_play_worker_proc = None  # 常驻播放 worker（play_worker.py），复用同进程省冷导入


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
                with contextlib.suppress(Exception):
                    msg = json.loads(line).get("msg", "")
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
            stdout=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
            creationflags=_NO_WINDOW,
        )
        line = proc.stdout.readline() if proc.stdout else ""
        if not line or '"ready"' not in line:
            with contextlib.suppress(Exception):
                proc.kill()
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
        with contextlib.suppress(Exception):
            proc.stdin.close()
        proc.kill()
    except Exception:
        pass


def _persist_verify(verify_steps: list[str], history_file: Path | None = None) -> None:
    """把后台 UIA 校验结果写回发送历史最后一条（异步线程调用，不阻塞返回）。

    发送已序列化，最后一条必是本次刚落的历史；发完才起本线程，故无竞态。

    ``history_file``：与 ``_restore_async`` 同理，**由调用方在起线程时传入**。
    这里比 _restore_async 更隐蔽：调用方是
    ``target=lambda: _persist_verify(_uia_verify_sent(...))``，lambda 体要等
    ``_uia_verify_sent``（UIA 扫微信窗口，可能几百 ms~3s）跑完才真正执行本函数 ——
    等它落到 ``HISTORY_FILE`` 这行时，调用方的作用域（以及测试的 monkeypatch）
    早已退出。所以哪怕只晚几百毫秒，也足以把测试数据写进用户真实历史。
    """
    path = history_file or HISTORY_FILE
    try:
        if not path.exists():
            return
        hist = json.loads(path.read_text("utf-8"))
        if hist:
            hist[-1].setdefault("verify", []).extend(verify_steps)
            path.write_text(json.dumps(hist, ensure_ascii=False), "utf-8")
    except Exception as e:
        logger.debug("[wechat] 写回校验结果失败: %s", e)


def _restore_async(history_file: Path | None = None) -> None:
    """后台线程还原声卡并写回发送历史最后一条，不阻塞 _do_send 返回（省 ~3s 同步等待）。

    与下一步 _run_audio('apply') 通过 _restore_lock 互斥，避免同时改默认音频设备抢设备。
    发送已落库（_append_history 在起本线程前完成），最后一条必是本次，无竞态。

    ``history_file``：本次要写回的历史文件，**由调用方在起线程时传入**。
    为什么不能在线程里直接读模块全局 ``HISTORY_FILE``（2026-09-18 实测踩坑）：
    线程是异步的，它的生命周期会跨越调用方的作用域。单测里 ``HISTORY_FILE`` 被
    monkeypatch 到 tmp_path，线程**读**的时候 patch 还在（读到 tmp_path 的内容），
    **写**的时候测试已结束、patch 已撤销 —— 于是把测试数据原样搬进了用户真实的
    ``outputs/wechat_send_history.json``（实测：跑一遍单测就多出一条
    `tts_x.wav / 1.0s`，用户在桌宠「最近发送」里看到它，以为是自己发的）。
    把路径在起线程时定下来，读写必然指向同一个文件。
    """
    path = history_file or HISTORY_FILE
    try:
        with _restore_lock:
            restored, restore_err = _safe_restore()
        with _history_lock:
            try:
                if path.exists():
                    hist = json.loads(path.read_text("utf-8"))
                    if hist:
                        hist[-1]["restored"] = restored
                        hist[-1]["restore_error"] = restore_err
                        path.write_text(json.dumps(hist, ensure_ascii=False), "utf-8")
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
except Exception as _e:  # uiautomation 未安装 / 非 Windows → 纯像素链路
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


def _await_uia_active(retries: int = 3, delay_s: float = 0.5) -> bool:
    """等微信 UIA 控件树物化后再判可用性（**必须在微信已起来之后调用**）。

    2026-09-19 实测事故：判定原先排在 `pre_apply.result()`（会拉起微信）**之前**，
    而 `ensure_active()` 写完激活字节只 `sleep(0.5)` 就查控件树 —— 微信刚重启时
    树还没物化 → 判 False，且这个**否定结果会缓存 3 秒**；发送链路只调一次，
    于是**凡涉及重启微信的发送必然退化到像素链路**（实测 5 条里 4 条走像素）。
    像素链路是按坐标盲点，点歪过一次直接弹出了浏览器的网页版文件传输助手。

    重试前先 `reset_state()` 清掉那个否定缓存，所以这里的 delay 不需要等满 3 秒，
    只要给 Qt 建树留一点时间即可（这也正是 `reset_state()` docstring 所说
    「微信重启后调用」的落点，此前生产代码里零调用）。
    """
    if _uia is None:  # 模块都没导进来，重试也没意义
        return False
    for i in range(max(1, retries)):
        if _uia_ready():
            return True
        if i < retries - 1:
            try:
                _uia.reset_state()
            except Exception:
                pass
            time.sleep(delay_s)
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
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(AUDIO_PS1),
                "-action",
                action,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
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
        raise RuntimeError(
            f"audio_config {action} 失败: {json.dumps(data, ensure_ascii=False)[:500]}"
        )
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
        self._thread = threading.Thread(target=self._run, daemon=True, name="wechat-audio-apply")
        self._thread.start()

    def _run(self) -> None:
        try:
            self._result = _run_audio("apply")
        except BaseException as e:  # 原样转交 result()，交给 _do_send 的异常路径处理
            self._error = e
        finally:
            self._done.set()

    _TIMEOUT = 130.0  # 后台任务等待上限（_run_audio 自身 timeout=120）

    def result(self) -> dict:
        """阻塞到后台准备结束；成功返回结果 dict，失败原样抛出（_do_send 内调用一次）。"""
        self._consumed = True
        self._done.wait(timeout=self._TIMEOUT)
        if self._error is not None:
            raise self._error
        if self._result is None:
            raise RuntimeError("录音环境准备未返回结果（后台任务超时？）")
        return self._result

    def abandon(self) -> None:
        """消费前的任务作废（TTS 失败 / 早退路径）：等线程收尾并还原声卡，
        绝不把系统默认麦留在 CABLE 上。已消费（_do_send 接手）时是安全的空操作。"""
        if self._consumed:
            return  # _do_send 已消费：还原由其异常/早退路径负责，别重复 restore
        self._consumed = True
        self._done.wait(timeout=self._TIMEOUT)
        with contextlib.suppress(Exception):
            self._after_run()
        with contextlib.suppress(Exception):
            _safe_restore()

    def _after_run(self) -> None:
        """子类钩子：任务作废时的额外收尾（基类无事可做）。"""


# ---------------- 录音环境准备：必要时重启微信 + 切卡 ----------------
# 详见模块 docstring：微信在进程启动时绑定采集设备，改默认麦克风对它不热生效。


def _restart_mode() -> str:
    """当前的重启策略（**每次调用时**读环境变量，便于测试与运行时切换）。

    2026-09-17 起默认 "0"（用户拍板：绝不杀微信）——此前默认 auto 会按遥测保守
    重启微信（杀进程 → 切卡 → 拉起），强杀副作用是微信退回登录界面要求重新登录，
    用户强烈不满。环境变量 VM_WECHAT_RESTART 仍可覆盖（"1"/"auto" 可恢复旧行为）。
    """
    return os.environ.get(RESTART_MODE_ENV, "0").strip().lower()


def _device_keyword() -> str:
    """目标设备关键词（用来判断微信上次录音是不是录的 CABLE）。

    播放端叫法与采集端叫法只有**中段**相同，所以不能拿整串去比端点：
        "CABLE Input (…)" / "扬声器 (…)"（播放端，中文系统被本地化）
        "CABLE Output (VB-Audio Virtual Cable)"（采集端，微信遥测里读到的）
    但**也不能只取首词**（2026-09-18 修正）：`OUTPUT_DEVICE_KEYWORD` 现在默认是
    常量串 "VB-Audio Virtual Cable"，首词是 "VB-Audio"，而本机设备表里还有
    Voicemeeter 的 `CABLE Output (VB-Audio Point)` / `Input (VB-Audio Point)`——
    只匹配 "VB-Audio" 会把它们当成 CABLE，于是 auto 模式误判"无需重启"，
    录到静音再发出去（正是 §2.15 花大力气修掉的症状）。
    故：剥掉端点词（CABLE Input / 扬声器…）后取剩余部分做包含判断。
    """
    kw = OUTPUT_DEVICE_KEYWORD.strip().lower()
    # 多候选（`|` 分隔）时只取**第一个**（驱动名）：本函数产出的是"剥掉端点词的中段串"，
    # 用来判断微信遥测里的设备名是不是我们的虚拟声卡 —— 拿端点词候选去比会误判
    # （端点词在中文系统里压根不出现，在英文系统里又是被截断的那半截）。
    kw = kw.split("|")[0].strip()
    if not kw:
        return ""
    for tok in ("cable input", "cable output", "speaker", "speakers", "麦克风", "扬声器"):
        if kw.startswith(tok):
            kw = kw[len(tok) :]
            break
    return kw.strip(" ()[]-—")


def _need_wechat_restart() -> tuple[bool, str]:
    """判断这次发送前要不要重启微信。返回 (是否重启, 人话原因)。

    auto 模式的判据是**微信自己的遥测**（它最近一次录音实际用了哪个输入设备），
    而不是"猜"。读不到证据时保守选择重启：漏重启的代价是"静音语音发出去"，
    误重启的代价只是多等十几秒。
    """
    mode = _restart_mode()
    if mode == "0":
        return False, f"{RESTART_MODE_ENV}=0 已关闭重启"
    if mode == "1":
        return True, f"{RESTART_MODE_ENV}=1 强制每次重启"
    if not wproc.list_wechat_processes():
        return True, "微信当前没在运行（需要拉起来）"
    dev = wproc.input_device_probe().get("device")
    kw = _device_keyword()
    if not dev:
        return True, "读不到微信上次录音用的输入设备，保守起见重启一次"
    if kw and kw in dev.lower():
        return False, f"微信上次录音已用「{dev}」，无需重启"
    return True, f"微信上次录音用的是「{dev}」（不是 {OUTPUT_DEVICE_KEYWORD}），需重启让它重新枚举"


def _binding_warning() -> str:
    """RESTART=0 时的**只读**提醒：这次录音很可能录到错误设备（→ 静音语音）。

    不重启是用户拍板的选择（强杀会把微信退回登录页），但"不重启"不等于
    "不该告诉用户"：`_need_wechat_restart()` 在 mode=0 时会在读遥测之前就短路，
    于是"微信绑的不是 CABLE"这个已知的静音成因再也没人过问，用户只会收到一条
    听起来正常的静音语音（2026-09-18 复核时发现的盲区）。判据与 auto 模式同源，
    全部是读操作，绝不修改任何状态。返回空串 = 没有值得提醒的问题。
    """
    if not wproc.list_wechat_processes():
        return f"微信当前没在运行，且 {RESTART_MODE_ENV}=0 不会自动拉起" "（本次会降级为手动发送）"
    dev = wproc.input_device_probe().get("device")
    if not dev:
        return (
            "读不到微信上次录音用的输入设备，无法确认它会不会录到 CABLE；"
            "若发出去是静音，需设 VM_WECHAT_RESTART=auto"
        )
    kw = _device_keyword()
    if kw and kw in dev.lower():
        return ""
    return (
        f"微信上次录音用的是「{dev}」而不是 {OUTPUT_DEVICE_KEYWORD}，"
        f"而 {RESTART_MODE_ENV}=0 不会重启它 → 这条语音可能录成静音"
    )


def _relaunch_quietly(exe) -> None:
    """兜底拉起微信（失败只记日志，不往上冒——调用方往往正在处理别的异常）。"""
    try:
        wproc.start_wechat(exe)
    except Exception as e:
        logger.warning("[wechat] 兜底拉起微信失败: %s", e)


def _prepare_recording_env() -> dict:
    """录音前的环境准备：需要时重启微信，再把默认麦克风切到 CABLE Output。

    顺序是硬约束：**杀微信 → 切卡 → 拉起微信**（反了就是白切）。
    返回 dict（``kind="recording_env"``）供 _do_send 拼 steps；失败抛 RuntimeError，
    且绝不把用户的微信留在死状态。
    """
    need, why = _need_wechat_restart()
    info = {
        "kind": "recording_env",
        "restart": False,
        "reason": why,
        "exe": "",
        "killed": [],
        "new_pid": None,
        "hwnd": None,
        "waited_s": 0.0,
    }
    if not need:
        _run_audio("apply")
        # 不重启时补一次只读的绑定检查（见 _binding_warning）：把"可能录成静音"
        # 变成用户看得见的警告，而不是等他听完才发现。
        warn = _binding_warning() if _restart_mode() == "0" else ""
        info["summary"] = f"麦克风已切到 CABLE Output（未重启微信：{why}）"
        if warn:
            info["warning"] = warn
            info["summary"] += f"；⚠ {warn}"
        return info

    exe = wproc.resolve_wechat_exe()
    if exe is None:
        raise RuntimeError(
            "需要重启微信才能让它重新枚举录音设备，但找不到微信主程序；"
            "请设环境变量 VM_WECHAT_EXE 指向 Weixin.exe"
        )
    info["exe"] = str(exe)
    info["killed"] = wproc.kill_wechat(grace_s=RESTART_KILL_GRACE_S).get("pids", [])
    try:
        # 微信不在场时切卡，它启动时才会绑到 CABLE Output
        _run_audio("apply")
        info["new_pid"] = wproc.start_wechat(exe)
        ready = wproc.wait_wechat_ready(timeout_s=RESTART_WAIT_S)
    except BaseException:
        if not wproc.list_wechat_processes():  # 别把用户的微信丢在死状态
            _relaunch_quietly(exe)
        raise
    info.update(restart=True, hwnd=ready["hwnd"], waited_s=ready["waited_s"])
    kill_note = f"杀掉旧进程 {info['killed']}" if info["killed"] else "微信原本未运行"
    info["summary"] = (
        f"已重启微信（{kill_note} → 新 PID {info['new_pid']}，"
        f"{ready['waited_s']:.1f}s 窗口就绪）并切麦克风到 CABLE Output"
        f"；原因：{why}"
    )
    return info


def _send_preflight() -> str:
    """发送前**只读**预检：微信没开=这次发送注定失败，别等用户白等合成。

    只在 VM_WECHAT_RESTART=0（默认，绝不碰微信）下拦截：这种模式没人会替
    用户开微信，微信没开/收托盘/停登录页全是必败。auto/1 会自己拉起或重启
    微信，没有可预判的硬失败（登录页由 wait_wechat_ready 的报错兜着）。
    报错文案直接借用 wechat_proc.find_wechat_hwnd 的分因版本。
    """
    if _restart_mode() != "0":
        return ""
    try:
        wproc.find_wechat_hwnd()
    except RuntimeError as e:
        return f"{e}（当前 VM_WECHAT_RESTART=0，不会自动帮你打开微信）"
    return ""


class _PendingRecordingEnv(_PendingApply):
    """把「重启微信 + 切卡」提前到与 TTS 合成并行（2026-09-11）。

    重启是这条链路最大的延迟来源（杀 ~2.5s + 切卡 ~3s + 拉起并等窗口 10~30s），
    串行排在 TTS(≈6s)/RVC 之后纯属白等；send_text 拿到发送锁就起本任务，
    _do_send 真正要用麦克风前才 .result() 等尾差。
    """

    _TIMEOUT = 240.0  # 覆盖 杀 + 切卡 + 拉起 + 等窗口(90s) 的最坏情况

    def _run(self) -> None:
        try:
            self._result = _prepare_recording_env()
        except BaseException as e:  # 原样转交 result()，交给 _do_send 的异常路径处理
            self._error = e
        finally:
            self._done.set()

    def _after_run(self) -> None:
        """作废兜底：确保微信还活着（绝不因一次失败发送把用户的微信用没了）。

        注意 `VM_WECHAT_RESTART=0` 时必须完全不动作——那种模式下微信是死是活
        本来就不归这条链路管，别去替用户开微信。
        """
        if _restart_mode() == "0":
            return
        if wproc.list_wechat_processes():
            return
        exe = wproc.resolve_wechat_exe()
        if exe:
            _relaunch_quietly(exe)


# ---------------- 播放 wav → CABLE Input（RVC venv 子进程，唯一有 sounddevice） ----------------

_PLAY_SCRIPT = r"""
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
"""


def _start_play_oneshot(wav: Path) -> subprocess.Popen:
    """一次性播放子进程（回退路径）：冷导入 numpy/sounddevice/soundfile 后播 wav。"""
    if not RVC_VENV_PY.exists():
        raise RuntimeError(f"找不到 RVC venv 解释器: {RVC_VENV_PY}（sounddevice 在该环境）")
    return subprocess.Popen(
        [
            str(RVC_VENV_PY),
            "-c",
            _PLAY_SCRIPT,
            str(wav),
            OUTPUT_DEVICE_KEYWORD,
            str(PLAY_LEAD_S),
            str(TAIL_S),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        creationflags=_NO_WINDOW,  # 播放期间不得弹出控制台遮挡微信
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
        with contextlib.suppress(Exception):
            proc.kill()
        raise RuntimeError("播放到 CABLE Input 超时")


def _play_to_cable(wav: Path, duration_s: float) -> None:
    if not RVC_VENV_PY.exists():
        raise RuntimeError(f"找不到 RVC venv 解释器: {RVC_VENV_PY}（sounddevice 在该环境）")
    try:
        proc = subprocess.run(
            [
                str(RVC_VENV_PY),
                "-c",
                _PLAY_SCRIPT,
                str(wav),
                OUTPUT_DEVICE_KEYWORD,
                str(LEAD_S),
                str(TAIL_S),
            ],
            capture_output=True,
            text=True,
            timeout=duration_s + 30,
            creationflags=_NO_WINDOW,  # 同上：播放期间不得弹出控制台遮挡微信
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
    "alt": (0xA4, 0x38),  # VK_LMENU
    "menu": (0xA4, 0x38),
    "ctrl": (0xA2, 0x1D),  # VK_LCONTROL
    "control": (0xA2, 0x1D),
    "shift": (0xA0, 0x2A),  # VK_LSHIFT
    "win": (0x5B, 0x5B),  # VK_LWIN
    "lwin": (0x5B, 0x5B),
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
    _fields_ = [
        ("wVk", _ctypes.c_ushort),
        ("wScan", _ctypes.c_ushort),
        ("dwFlags", _ctypes.c_ulong),
        ("time", _ctypes.c_ulong),
        ("dwExtraInfo", _ctypes.c_void_p),
    ]


class _MOUSEINPUT(_ctypes.Structure):
    _fields_ = [
        ("dx", _ctypes.c_long),
        ("dy", _ctypes.c_long),
        ("mouseData", _ctypes.c_ulong),
        ("dwFlags", _ctypes.c_ulong),
        ("time", _ctypes.c_ulong),
        ("dwExtraInfo", _ctypes.c_void_p),
    ]


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
with contextlib.suppress(Exception):
    _user32().SetProcessDPIAware()

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
_rect_ctx: tuple[int, int, int, int] | None = None  # 录音中的窗口 rect（finish 找绿钮用）
_record_via: str | None = None  # 本次录音启动方式：postmsg / realclick / None
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
        with contextlib.suppress(Exception):
            _user32().SetWindowLongW(hwnd, -20, old)
        _exstyle_restore = None
        time.sleep(0.05)


def _postmsg_mouse(
    screen_point: tuple[int, int] | None,
    down: bool = False,
    up: bool = False,
    target: int | None = None,
    lparam: int | None = None,
) -> tuple[int, int] | None:
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


def _mic_point(
    rect: tuple[int, int, int, int], offset_x: int = MIC_OFFSET_X, offset_y: int = MIC_OFFSET_Y
) -> tuple[int, int]:
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
    t_norm = np.sqrt((t0**2).sum())
    if t_norm == 0:
        return None, -1.0
    best_score, best_pos = -2.0, None
    for y in range(ih - th + 1):
        for x in range(iw - tw + 1):
            win = img[y : y + th, x : x + tw].astype(np.float64)
            w0 = win - win.mean()
            denom = np.sqrt((w0**2).sum()) * t_norm
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

        left, top, right, bottom = rect
        box = (max(0, right - 340), max(0, bottom - 150), right, bottom)  # 搜索区：右下 340x150
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
        cx = box[0] + pos[0] + tw // 2  # 匹配位置 + 模板中心
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
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    u = _user32()
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    hmon = u.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    if not hmon or not u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
        return
    left, top, right, bottom = _window_rect(hwnd)
    wa = mi.rcWork
    dx = dy = 0
    # 优先级：底边 > 右边 > 顶边/左边。
    # 话筒在窗口右下角，底边被（自动隐藏的）任务栏压住会直接导致点击失效；
    # 而最大化窗口的 top 常为负值（Windows 把边框裁到屏外），此时若为了
    # 「顶边不溢出」把窗口往下推，反而会把底边推出屏幕 —— 实测踩过（2026-09-09）。
    if bottom > wa.bottom:
        dy = wa.bottom - bottom
    elif top < wa.top and bottom <= wa.top:  # 仅当窗口整体在上边界之外才拉回
        dy = wa.top - top
    if right > wa.right:
        dx = wa.right - right
    elif left < wa.left and right <= wa.left:  # 仅当窗口整体在左边界之外才拉回
        dx = wa.left - left
    if dx or dy:
        # SWP_NOSIZE(0x1) | SWP_NOZORDER(0x4) | SWP_NOACTIVATE(0x10)
        u.SetWindowPos(hwnd, 0, left + dx, top + dy, 0, 0, 0x1 | 0x4 | 0x10)
        time.sleep(0.15)


def _find_wechat_hwnd() -> int:
    """找微信主窗口句柄（面积最大的那个）。

    实现在 `wechat_proc.find_wechat_hwnd`——重启链路要按窗口面积判断"是否还在
    登录页"，那套枚举逻辑只能有一份，故这里只做转发。
    """
    return wproc.find_wechat_hwnd()


def _foreground_wechat() -> int:
    """把微信拉到前台，返回其窗口句柄。SetForegroundWindow 有系统限制，用 ALT 抖动绕过。"""
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = _find_wechat_hwnd()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        time.sleep(0.3)
    vk, scan = _KEYMAP["alt"]
    _send_input_kb(vk, scan, False)  # ALT down/up 解锁前台切换限制
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

    left, top, right, bottom = rect
    u = _user32()
    sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    box = (
        max(0, min(right, sw) - 560),
        max(0, min(bottom, sh) - 140),
        min(right, sw),
        min(bottom, sh),
    )
    return np.asarray(ImageGrab.grab(bbox=box).convert("L")).astype(np.int16)


def _snapshot_overlay_baseline(rect: tuple[int, int, int, int]) -> None:
    """按下话筒前存一张基线，供 _wait_record_overlay 做差分。"""
    global _overlay_baseline
    try:
        _overlay_baseline = _grab_bottom_gray(rect)
    except Exception as e:  # 截图失败不该阻断发送
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
            _ensure_onscreen(hwnd)  # 防止窗口底边超屏被任务栏遮挡
            rect = _window_rect(hwnd)
            cached_mic = None
        _rect_ctx = rect
        # 首选 UIA 点击（2026-09-10 方案 A）：UIA 走 COM 调用，不经系统输入队列，
        # 天然不受 WS_EX_TRANSPARENT 点击穿透影响，也不用摘样式位、不依赖坐标。
        if _uia_ready():
            clicked = False
            try:
                clicked = _uia.click_voice_button()
            except Exception as e:
                logger.debug("[wechat] UIA 点击语音按钮失败，回退像素链路: %s", e)
            if clicked:
                # 点击已经真的发出去了 —— 此后**绝不能再点第二次**：这个话筒是
                # "单击开始持续录音"，重复点击会把进行中的录音停掉/送出去
                # （2026-09-18 复核发现的隐患，旧代码把"点击失败"和"没看见浮层"
                # 当成同一件事，检测超时就又点一次）。
                if _wait_record_overlay(rect, timeout=5.0):
                    _record_via = "uia"
                    return
                # 检测失败≠没在录音（浮层渲染慢 / 判据受限），再看一眼就够了
                time.sleep(0.5)
                if _wait_record_overlay(rect, timeout=2.0):
                    _record_via = "uia"
                    return
                raise RuntimeError(
                    "UIA 已点击语音按钮但录音浮层未出现；为避免把录音误发出去，"
                    "本次不重复点击（请稍后重试）"
                )
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
        with contextlib.suppress(Exception):
            _mouse_left(False)
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

    left, top, right, bottom = rect
    # 取域必须 clamp 到屏幕内：bbox 超出屏幕时 PIL 会把屏外部分填黑，
    # 若绿钮正好落在填黑区就永远检测不到（最大化窗口底边常溢出几像素）。
    u = _user32()
    sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    box = (
        max(0, min(right, sw) - 560),
        max(0, min(bottom, sh) - 140),
        min(right, sw),
        min(bottom, sh),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    for _ in range(retries):
        img = np.asarray(ImageGrab.grab(bbox=box).convert("RGB")).astype(int)
        green = (
            (img[:, :, 1] > 150) & (img[:, :, 0] < 120) & (img[:, :, 2] > 80) & (img[:, :, 2] < 180)
        )
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
            time.sleep(TAIL_S)  # 尾音缓冲（等 wav 尾音真正录进去）
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
                _mouse_left(False)  # 点 ↑ 绿钮 = 发送
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


def _trim_edges(
    path: Path, keep_head_s: float = 0.12, keep_tail_s: float = 0.12, thresh: float = 0.015
) -> Path:
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
        win = max(int(sr * 0.02), 1)  # 20ms 滑动窗
        n = len(d) // win
        rms = np.sqrt((d[: n * win] ** 2).reshape(n, win).mean(axis=1))
        idx = np.nonzero(rms > thresh)[0]
        if len(idx) == 0:  # 整段都静音，不裁（可能本就是纯静音测试）
            return path
        s = max(int((idx[0] - keep_head_s * 50) * win), 0)
        e = min(int((idx[-1] + 1 + keep_tail_s * 50) * win), len(d))
        if e - s < int(sr * 0.3):  # 裁后太短（<0.3s），保持原样
            return path
        tmp = path.with_name(f"{path.stem}_trim{path.suffix}")
        sf.write(tmp, d[s:e], sr, format="WAV")
        return tmp
    except Exception:
        return path


# ---------------- 分包算法（纯函数，可单测） ----------------


def pack_chunks(
    durs: list[float],
    max_s: float = MAX_CHUNK_S,
    min_s: float = MIN_CHUNK_S,
    gap_s: float = SENT_GAP_S,
) -> list[tuple[int, int]]:
    """按**实测时长**把句子贪心装箱成 ≤max_s 的若干段，返回 [(起, 止)] 左闭右开。

    这是整个分段方案的**唯一硬判据**所在：返回的每一段，加上 PLAY_LEAD_S/TAIL_S/
    FINISH_LAG_S 都必须 ≤60。所以它是纯函数、必须被单测 + 变异测试覆盖。

    为什么用实测时长而不是"字数 ÷ 语速"估算：字数→秒数只是代理量，中英混排、
    数字（"2026 年 9 月 22 日"读出来比看起来长）、标点停顿都会让它偏；**偏小的方向
    就是静默截断**，代价最大。而为了分包本来就要逐句合成，`sf.read` 一下就有真实秒数。

    单句自己就超预算的情形**不在这里处理** —— 那需要"再切一次并重新合成"，
    不是纯函数能决定的，由调用方做三级降级（见 _split_for_budget）。

    ⚠️ 返回的每段**已守预算**（含 gap），但**单句超预算时例外**：那种段照样原样返回，
    调用方必须先降级再发。用 `_chunk_plan_ok()` 自检，别假设输出天然合法。
    """
    if max_s <= 0:
        raise ValueError("max_s 必须为正")
    out: list[tuple[int, int]] = []
    i, n = 0, len(durs)
    while i < n:
        j, acc = i, 0.0
        while j < n:
            # 段内句子之间要留 gap；本段第一句前面没有 gap
            add = durs[j] + (gap_s if j > i else 0.0)
            if acc + add > max_s and j > i:
                break
            acc += add
            j += 1
        out.append((i, j))
        i = j
    # 尾段太短 → 并入上一段（能并则并，避免发一条 1.2 秒的）。
    # ⚠️ 只在**并入后仍守预算**时才并：否则最后两条气泡的总时长会破 60s
    #    （pack_chunks([54.0, 1.0]) 就是这样 —— 并进去是 55.3s 音频，实发 56.7s，
    #     虽然侥幸没到 60，但已经吃穿了 SAFETY 余量，属于"赌一把"而不是"保证"）。
    #    宁可多发一条短气泡，也不赌 —— 多一条气泡的成本远低于漏一句话。
    if len(out) >= 2 and sum(durs[out[-1][0] : out[-1][1]]) < min_s:
        prev, last = out[-2], out[-1]
        merged_span = sum(durs[prev[0] : last[1]]) + gap_s * max(last[1] - prev[0] - 1, 0)
        if merged_span <= max_s:
            out.pop()
            out[-1] = (prev[0], last[1])
    return out


def _chunk_plan_ok(
    durs: list[float], plan: list[tuple[int, int]], gap_s: float = SENT_GAP_S
) -> bool:
    """校验一份分包方案确实守预算。返回 False 就是有段会吃穿余量或破 60s。

    判据与 `_check_budget` **必须是同一条**（否则"自检通过"和"护栏放行"会打架）：
    每段既要 `≤ MAX_CHUNK_S`（含 SAFETY 余量），也要实发 `≤ MAX_MSG_S`（物理上限）。
    """
    eps = 1e-6
    for a, b in plan:
        span = sum(durs[a:b]) + gap_s * max(b - a - 1, 0)
        if span > MAX_CHUNK_S + eps:
            return False
        if PLAY_LEAD_S + span + TAIL_S + FINISH_LAG_S > MAX_MSG_S + eps:
            return False
    return True


# ---------------- 主流程 ----------------


class SendVoiceReq(BaseModel):
    wav: str | None = None  # outputs/ 下的文件名；缺省=最近一次 TTS 合成产物


class SendTextReq(BaseModel):
    text: str
    voice_id: str = ""  # TTS 参考音色（决定语气/韵律，"怎么说"）
    rvc_voice: str = ""  # RVC 音色（决定"谁在说"）；留空=不换声，音色会明显不像
    no_rvc: bool = False  # 显式跳过 RVC：纯 TTS 零样本克隆（与"推不出模型"的静默降级区分开）
    pitch: int = 0
    index_rate: float = 0.5


def _voice_has_reference(voice_id: str) -> bool:
    """该音色是否有 reference.wav（TTS 合成的前提，与 common.voice_ref 同一判据）。"""
    from common import is_valid_voice_id
    from runtime import VOICEBANK

    if not voice_id or not is_valid_voice_id(voice_id):
        return False
    return (VOICEBANK / voice_id / "reference.wav").exists()


def _tts_ref_for(want: str, rvc_voice: str) -> tuple[str, str]:
    """给一个「只有 RVC 权重、没有参考音」的音色借一段 TTS 参考音。

    返回 `(拿去合成的 voice_id, 借自谁的音色名或空串)`。

    **只给 send_text / preview_text 这两条「末尾必过 RVC」的链路用，
    别挪去做 /api/tts 的通用回退。** 两条链路里 RVC 的地位相反：
      · send_text：末尾必过 RVC，音色由 RVC 决定 → 语气借谁的都不影响"像不像"；
      · /api/tts：没有 RVC，参考音**就是**音色本身 → 借一段别的声音来合成，
        结果是"看起来能用、其实完全不是那个音色"，比直接报错更糟。

    为什么只在 `rvc_voice` 非空时才借：没有 RVC 覆盖时，借来的参考音会直接决定
    输出音色 —— 那是在拿别人的声音冒充用户选的音色，必须让它照原样报错。

    借的对象按"用户意图"排序：主界面当前选中的音色 → 音色库里第一个有参考音的。
    都借不到就原样返回 want，让 synth_wav 抛它本来就该抛的那个 400/404。
    """
    if not want or not rvc_voice or _voice_has_reference(want):
        return want, ""
    from common import selected_voice
    from runtime import VOICEBANK

    cur = selected_voice()
    if cur and cur != want and _voice_has_reference(cur):
        return cur, cur
    # glob 而不是 iterdir：音色库目录不存在时它返回空集而不是抛 FileNotFoundError
    for ref in sorted(VOICEBANK.glob("*/reference.wav")):
        if ref.parent.name != want:
            return ref.parent.name, ref.parent.name
    return want, ""


class PreviewTextReq(BaseModel):
    """试听请求：与 `SendTextReq` 同形，但**不发送任何东西、不碰声卡**。"""

    text: str
    voice_id: str = ""
    rvc_voice: str = ""
    no_rvc: bool = False  # 与 SendTextReq.no_rvc 同义：试听也要能听"千问直出"的样子
    pitch: int = 0
    index_rate: float = 0.5  # 与 send_text 同值：试听要预测"发出去是什么样"，不是修辞过的版本


def _preview_quality_ok(path: Path) -> tuple[bool, str]:
    """试听输出质量关：复用 market_preview 的判据（静音/破音/削顶/NaN/截断）。

    为什么必须过这一关：本机 TTS 对卡通 / 市场音色的零样本克隆**会吐 1s 纯静音**
    （`market_preview` 的 docstring 记了这条 2026-09-06 的实测），而"试听听到一段空白"
    比"试听不可用"更糟 —— 用户会以为这个音色坏了，而其实只是 TTS 没合好。
    质检模块本身不可用时按合格处理：试听不该因为质检不可用而整体失败。
    """
    try:
        from market_preview import _quality_ok

        return _quality_ok(path)
    except Exception:  # noqa: BLE001
        return True, ""


def _preview_sample(voice_id: str, reason: str, steps: list[str]):
    """退回「固定样板句」试听：market_preview（中性真人源句 → RVC，voice-to-voice）。

    这是仓库里最稳的一条试听路 —— 2026-09-07 特意把它从"截取袋鼠参考音当源句"
    换成中性源句，就是为了压掉"每个音色试听都带袋鼠腔"。所以这里的兜底不是
    "降级成不可用"，而是"降级成听固定句"：至少能确认音色本身是对的。

    注意 `generate()` 是异步的（后台线程），首次会返回 generating —— 如实把它
    报给前端并让用户再点一次，不在这里自旋等待（试听是交互动作，不该卡住请求）。
    """
    steps.append(f"⚠ 改用样板句试听：{reason}")
    try:
        import market_preview

        st = market_preview.generate(voice_id)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(
            status_code=500,
            content={"ok": False, "source": "sample", "error": f"样板句试听也不可用：{e}", "steps": steps},
        )
    if st.get("status") == "ready":
        return {
            "ok": True,
            "source": "sample",
            "url": st["url"],
            "text": market_preview.PREVIEW_TEXT,
            "steps": steps,
            "note": "这条是固定样板句，不是你输入的文字（你的文字没合出可用的声音）",
        }
    if st.get("status") == "generating":
        return {
            "ok": False,
            "source": "sample",
            "status": "generating",
            "error": "样板句试听正在生成，等几秒再点一次「试听」",
            "steps": steps,
        }
    return {
        "ok": False,
        "source": "sample",
        "status": st.get("status") or "failed",
        "error": st.get("error") or f"样板句试听不可用（{st.get('status')}）",
        "steps": steps,
    }


@router.post("/preview_text")
def preview_text(req: PreviewTextReq):
    """试听：文字 → TTS（借参考音做语气）→ RVC 换成目标音色 → 返回可播放 wav。

    **不碰微信、不碰声卡、不占发送锁** —— 这是它跟 send_text 的唯一区别，也正因如此
    它才能给"市场装的音色"提供试听：这类音色只有 RVC 权重、没有参考音，`/api/tts`
    会直接 404，面板以前就是因此把「试听」禁用掉的。

    为何不直接给 `/api/tts` 加个参数：那条链里**没有 RVC**，参考音**就是**音色本身 ——
    让它去借一段别的声音会合出"看着能用、其实不是这个音色"的结果（见 `_tts_ref_for`）。
    借用参考音与换声必须绑在一起做，所以单独一条端点。

    四段降级，顺序都是「先给真东西，再退回能用的东西」：
      1. 正常：TTS(借参考音) → RVC(目标音色) —— 听到的就是"发出去会是什么样"；
      2. 推不出目标模型 / 音色自带参考音而 RVC 失败 → 保留 TTS 结果
         （＝今天的 `/api/tts` 行为：参考音本身就是这个音色，所以它仍是对的）；
      3. **借了参考音又没换成声** → 退回样板句。此时 TTS 结果是"借来那个人的嗓音"，
         保留它等于拿别人的声音冒充用户选的音色 —— 这正是本端点要解决的问题，不能自己犯；
      4. 输出不过质量关（典型：TTS 吐静音）→ 退回样板句。
    """
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    from common import selected_voice
    from rvc_convert import resolve_rvc_voice
    from tts_api import synth_wav

    steps: list[str] = []
    want = req.voice_id or selected_voice()
    # 与 send_text 同一开关：no_rvc=True 时跳过 RVC，试听 = 纯 TTS 克隆（要求音色自带参考音）
    rvc_voice = "" if req.no_rvc else (req.rvc_voice or resolve_rvc_voice(want) or "")
    # 与 send_text 用**同一个**借参考音规则 → 试听听到的语气就是发出去的语气
    tts_voice, borrowed = _tts_ref_for(want, rvc_voice)
    try:
        wav, duration_s, vid = synth_wav(req.text, tts_voice)
    except HTTPException as e:
        return _preview_sample(want, f"TTS 失败：{e.detail}", steps)
    except Exception as e:  # noqa: BLE001
        return _preview_sample(want, f"TTS 失败：{e}", steps)
    if borrowed:
        steps.append(f"「{want}」没有参考音，语气借自 {borrowed}")
    # 2) RVC 换声；推不出目标模型就保持 TTS 结果（与今天的 /api/tts 一致）
    if rvc_voice:
        try:
            from rvc_convert import rvc_convert

            wav = rvc_convert(wav, rvc_voice, req.pitch, req.index_rate)
            duration_s = round(_wav_duration(wav), 1)
            steps.append(f"RVC 换声 → {rvc_voice}")
        except Exception as e:  # noqa: BLE001
            # ★ 借了参考音又没换成声 = **你现在听到的是借来那个人的嗓音**。
            # 这是本条链路上唯一会"拿别人的声音冒充你选的音色"的口子，而且很隐蔽：
            # 音频能播、时长正常、步骤里也只是个 ⚠ —— 但音色是错的。
            # 用户对这个端点的要求原话是"能听到它真正的声音，而不是…听到借来的嗓音"，
            # 所以这里不能沿用在 /api/tts 里成立的"保留 TTS 结果"：
            # 那条链里参考音**就是**音色本身，保留它是对的结果；这里不是。
            # 退回样板句（那是这个音色**自己**的 RVC 渲染）比给一段错音色的更诚实。
            if borrowed:
                return _preview_sample(
                    want,
                    f"RVC 换声失败（{e}）—— 借来的参考音会留在音频里，这段不是「{want}」的声音",
                    steps,
                )
            steps.append(f"⚠ RVC 换声失败，本次试听是 TTS 原声（{e}）")
    # 3) 质量关：静音/破音一律退回样板句，绝不把一段空白当成功播给用户
    ok, why = _preview_quality_ok(wav)
    if not ok:
        return _preview_sample(want, f"合成结果不合格（{why}）", steps)
    # **刻意不登记进作品历史**（`history.register`）：
    #   ① 语义上试听是中间产物，不是"作品" —— 每点一次试听就往作品库塞一行是噪声；
    #   ② 安全上 `history.HISTORY_FILE` 是**导入时早绑定**的（`cfg.OUTPUTS_DIR / ...`，
    #      同 `docs/犯错档案-工程.md` §8.36 的 MARKET_DIR 那一类）—— 单测只能 patch `cfg`，
    #      早绑定的常量照旧指向真实 outputs/，于是"跑一遍单测往用户作品库里塞记录"
    #      （同样的事这个仓在 §2.24 已经踩过一次）。不写就彻底没这个面。
    return {
        "ok": True,
        "source": "text",
        "voice_id": want,
        "rvc_voice": rvc_voice,
        "tts_voice": vid,
        "borrowed": borrowed,
        "wav": wav.name,
        # `.session/` 前缀必须带上：这条试听的产物现在是会话产物（见 session_out）
        "url": f"/api/media/outputs/{session_out.rel_url(wav.name)}",
        "duration_s": duration_s,
        "steps": steps,
    }


# ---------------- 文字入口的分包（逐句合成 → 实测时长 → 装箱） ----------------


def _synth_sentence(sentence: str, tts_voice: str) -> bytes:
    """单句合成 → wav 字节。分段链路里 TTS 的唯一出口，单独成函数是为了可注入。

    为什么不复用 `tts_api.synth_wav`（那才是 TTS 的"正式"入口）：
      · `synth_wav` 会传 `ref_text`，即走 **ICL 语气克隆**；分段链路要的是
        **x-vector 纯声纹**（`ref_text=""`，见 `_split_for_budget` 的 D3 理由②）；
      · `synth_wav` 每句都会落一个 `tts_*.wav`（长文就是几十个文件），
        而这里本来就落 `wxseg_*.wav`，再叠一层是双份垃圾。
    两者是**刻意的分叉**，不是懒得复用 —— 同款 x-vector 路线在 `audiobook.py` 已实测。
    """
    from qwen3_tts import tts as qwen_tts

    return qwen_tts(
        sentence,
        ref_audio=str(_voice_ref_for(tts_voice)[0]),
        ref_text="",
        language="Chinese" if _looks_zh(sentence) else "English",
        voice_id=tts_voice,
    )


def _sfx_clip(sid: str) -> tuple[Path, float]:
    """音效 id → (wav 路径, 秒数)。读不到就抛可读的错误（不让它变成"第 N 句合成失败"）。"""
    import sfx_lib

    try:
        p, _builtin = sfx_lib.resolve_path(sid)
    except sfx_lib.SfxError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return p, sfx_lib.duration_s(p)


def _split_for_budget(
    text: str,
    tts_voice: str,
    want: str,
    rvc_voice: str,
    pitch: int,
    index_rate: float,
    steps: list[str],
    synth=None,
) -> tuple[list[Path], list[str]]:
    """长文 → 逐句合成 → 按实测时长装箱 → 逐段拼接/换声 → 若干 ≤54s 的 wav。

    为什么**逐句合成**而不是整段合成后再切（D3，三条都不是偏好问题）：
      1. 切分点必须落在句子边界 —— 整段在 54 秒处硬切会把词切成两半；
      2. 稳定性：audiobook.py 实测记录，逐句走 x-vector 声纹克隆（ref_text 置空）
         批任务更稳，且 8GB 显存下 ICL 长参考会跌进 WDDM 共享内存慢路径，
         **整段长文正是最长的那个参考**；
      3. 失败面小：单句失败只毁一句，可重试可跳过；整段失败就全废。

    为什么用**实测时长**而不是字数估算（D2）：字数→秒数只是代理量，中英混排、
    数字（"2026 年 9 月 22 日"读出来比看起来长）、标点停顿都会让它偏；
    **偏小的方向就是静默截断**，代价最大。而逐句合成后 `sf.read` 一下就有真实秒数。

    返回 (wav 列表, 说明 steps)。只有一段时仍返回长度 1 的列表 ——
    调用方据此判断"要不要分包"，不另开分支。

    `synth` 是单句合成器（`(句子, tts_voice) -> wav 字节`），默认 `_synth_sentence`。
    它是个**显式接缝**：单测里没有 TTS 模型，必须能替换掉；而在生产路径上
    用默认值，调用方不必知道。

    ---- 音效标记（2026-09-25）----
    文字里的 `[爆炸]` 会在**这一句之后**插一声（见 `sfx_mark.py`）。两个要点：
      · 标记在合成前就被剔掉，**不会被念出来**；
      · 音效时长**并进 `durs[i]`**，所以装箱时它就被算进预算 —— 否则加了音效的
        那条语音会超出 54s 预算，而超预算的后果是**静默截断**（这套预算存在的
        全部意义就是消灭它）。
    """
    from audiobook import split_sentences
    from pydub import AudioSegment

    import sfx_mark

    synth = synth or _synth_sentence
    sents = split_sentences(text)
    if not sents:
        raise HTTPException(status_code=400, detail="text 里没有可合成的句子")
    steps.append(f"断句：{len(sents)} 句（复用 audiobook.split_sentences）")

    # 先把每句里的音效标记解析掉：得到"要念的文字" + "这句之后接哪个音效"。
    # 必须在合成**之前**做 —— 合成器只认纯文本，标记混在里面会被逐字念出来。
    speak_texts: list[str] = []
    sfx_after: list[str | None] = []
    unknown: list[str] = []
    for s in sents:
        if not sfx_mark.has_mark(s):
            speak_texts.append(s)
            sfx_after.append(None)
            continue
        segs, unk, _prob = sfx_mark.parse(s)
        # 一段句子里可能有多个标记，但一句话只能接一个音效（插两下的语义没定义）——
        # 取**最后一个**（"说到这句末尾"的直觉），其余在 steps 里说明。
        sid = next((x[1] for x in reversed(segs) if x[1]), None)
        unknown.extend(unk)
        # 标记已从文本里剔除；末尾补一个空格，避免"那个 那个地方"这种粘连
        spoken = "".join(t for t, _ in segs).strip()
        speak_texts.append(spoken)
        sfx_after.append(sid)

    if unknown:
        # 未知名字必须报错，不能静默丢弃：用户写了 [爆炸声] 却没插进去、
        # 还照样发送成功，是最坏的结果（他以为插上了）。
        avail = "、".join(sfx_mark.available()[:12])
        raise HTTPException(
            status_code=400,
            detail=f"没有这个音效：{'、'.join(unknown)}。可用：{avail}",
        )

    # 逐句合成 + 记实测时长。参考音取 voice_ref(tts_voice) —— 与 audiobook 同款 x-vector 路线。
    # 先在这里探一次参考音：探不到就直接 400（"这个音色没参考音"是用户可自己修的错误），
    # 不必等第一句合成失败才发现，也避免把"音色不存在"报成"第 1 句合成失败"。
    _voice_ref_for(tts_voice)
    stamp = int(time.time() * 1000)
    seg_paths: list[Path] = []
    durs: list[float] = []
    n_sfx = 0
    for i, s in enumerate(speak_texts):
        if not s.strip():
            # 整句都被标记吃掉了（`[掌声]` 单独成句）：没有要念的文字。
            # 不能丢 —— 它就是"这里插一声"，所以给一段静音占位，音效照插。
            p = session_out.new_path(f"wxseg_{stamp}_{i + 1:04d}")
            AudioSegment.silent(duration=10, frame_rate=24000).export(str(p), format="wav")
        else:
            try:
                wav_bytes = synth(s, tts_voice)
            except HTTPException:
                # 音色/参考音这类"用户能自己修"的错误原样上抛（400/404），别包成 500
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"第 {i+1} 句合成失败: {e}") from e
            p = session_out.new_path(f"wxseg_{stamp}_{i + 1:04d}")
            p.write_bytes(wav_bytes)
            # 裁首尾静音再量时长：TTS 产物常带 0.5~1s 静音，不裁会让预算算虚（偏大），
            # 也把静音录进微信里（用户听到的是"开头一段空白"）。
            p = _trim_edges(p)
        seg_paths.append(p)
        d = _wav_duration(p)
        if sfx_after[i]:
            # ★ 音效时长并进这一句 —— 装箱时它就被算进预算。不并进来，
            #   带音效的那条会超 54s 而被**静默截断**（用户丢了后半句还不知道）。
            _sp, sd = _sfx_clip(sfx_after[i])
            d += sd + SFX_GAP_S
            n_sfx += 1
        durs.append(d)

    if n_sfx:
        steps.append(f"插入音效：{n_sfx} 处（已计入分段预算）")

    plan = pack_chunks(durs)
    if not _chunk_plan_ok(durs, plan):
        # 不该发生（pack_chunks 自己守预算）；真发生说明判据与现实脱节，
        # 宁可拒发也不要发一条会被截断的。
        bad = [f"{i}({d:.1f}s)" for i, d in enumerate(durs) if d > MAX_CHUNK_S]
        raise HTTPException(
            status_code=400,
            detail=(
                f"分包后仍有段超预算（{', '.join(bad) or '未知'}）——"
                f"单句过长时需先降级切分（见方案 D5）"
            ),
        )

    outs: list[Path] = []
    for k, (a, b) in enumerate(plan):
        seg = AudioSegment.empty()
        for j in range(a, b):
            if j > a:
                seg += AudioSegment.silent(duration=int(SENT_GAP_S * 1000), frame_rate=24000)
            seg += AudioSegment.from_wav(str(seg_paths[j]))
            if sfx_after[j]:
                # 音效接在这一句之后（同一段内）。点它在拼接阶段发生，
                # 而不是"播放时插"—— 播放是整段一次性灌进 CABLE 的，
                # 没有可以插入的时间窗口（见 sfx_mark.py 模块注释）。
                sp, _sd = _sfx_clip(sfx_after[j])
                seg += AudioSegment.silent(
                    duration=int(SFX_GAP_S * 1000), frame_rate=24000
                )
                seg += AudioSegment.from_wav(str(sp))
        raw = session_out.new_path(f"wxchunk_{stamp}_{k + 1:02d}")
        seg.export(str(raw), format="wav")
        if rvc_voice:
            from rvc_convert import rvc_convert as _rvc

            raw = _rvc(raw, rvc_voice, pitch, index_rate)
        outs.append(raw)
        steps.append(
            f"第 {k + 1}/{len(plan)} 条：{b - a} 句 / "
            f"{sum(durs[a:b]) + SENT_GAP_S * max(b - a - 1, 0):.1f}s（预算 {MAX_CHUNK_S:.0f}s）"
        )
    return outs, steps


def _looks_zh(s: str) -> bool:
    """句子是否以中文为主（决定 TTS 的 language 提示）。不引入新依赖，按字符占比判。"""
    zh = sum(1 for c in s if "\u4e00" <= c <= "\u9fff")
    return zh * 2 >= max(len(s.strip()), 1)


def _voice_ref_for(voice_id: str):
    """取参考音；失败时给出可执行的提示（不泄漏内部路径细节到用户可见文案）。"""
    from common import voice_ref

    try:
        return voice_ref(voice_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"音色 {voice_id} 没有可用参考音: {e}") from e


@router.post("/send_text")
def send_text(req: SendTextReq):
    """一键：文字 → TTS → RVC 换声 → 全自动发成微信语音消息。

    桌宠「合成并发送」走的就是这个接口。链路里 **RVC 是音色的唯一来源**——
    TTS 零样本克隆复现不了袋鼠音色（2026-08-31 用户 A/B 亲耳判定），
    所以 rvc_voice 留空会得到一条"普通播音腔"，不是 bug。

    延迟优化（2026-09-10）：切默认麦→CABLE（~3s）已提前到与 TTS 合成（~6s）
    并行（_PendingApply），_do_send 用麦克风前只等尾差，端到端省 ~3s。
    2026-09-11：若判断需要重启微信（见 _need_wechat_restart），重启 + 切卡整体
    也在这段并行里做（_PendingRecordingEnv），不额外拖慢端到端。
    2026-09-18：合成前先做一次只读预检（_send_preflight）——TTS+RVC 要 1~2 分钟，
    微信没开这种事没理由让用户等完整条合成链才被告知。
    """
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    if not _send_lock.acquire(blocking=False):
        raise HTTPException(409, "已有一次微信语音发送在进行中，请等它结束")
    apply_task: _PendingRecordingEnv | None = None
    try:
        # 只读预检：拦「注定要失败」的发送，别让用户白等合成。
        # 这里不真启动任何东西；mode=auto 时微信没开会由 _PendingRecordingEnv 拉起，照旧放行。
        pre_err = _send_preflight()
        if pre_err:
            raise HTTPException(status_code=409, detail=pre_err)
        _t0 = time.time()
        # 切卡（必要时还含重启微信）最长几十秒，别串行等在 TTS 后面：
        # 立刻起后台任务与合成并行（见 _PendingRecordingEnv）。
        # 合成/组装任何一步失败，下面的 except 会 abandon() 兜底还原声卡。
        apply_task = _PendingRecordingEnv()
        # 「谁在说」先定，再看「怎么说」—— 顺序不能反：
        #   · 显式给了 rvc_voice 就用它；否则按 voicebank id → RVC 实验名的约定推
        #     （kangaroo → kangaroo_v2），推不到就照发并在 steps 里说清楚音色会不像。
        #   · want 为空 = 桌宠面板选了"主界面选中"，交给 common.selected_voice() 定，
        #     推 RVC 时必须用同一个 want，否则又退回"不换声"（旧行为）。
        from common import selected_voice
        from rvc_convert import resolve_rvc_voice

        want = req.voice_id or selected_voice()
        # no_rvc=True = 用户在面板显式选了「千问直出」：跳过 RVC，纯 TTS 零样本克隆。
        # 这与"推不出模型"的静默降级是两回事 —— steps 里的文案必须分开，别让用户
        # 主动的选择被当成"音色配置缺失"的告警（2026-09-26 A/B 实测两条听感接近后加的）。
        rvc_voice = "" if req.no_rvc else (req.rvc_voice or resolve_rvc_voice(want) or "")
        # 市场装的音色只有 logs/<id>/<id>.pth、没有 reference.wav，直接拿它当 TTS 参考音
        # 会被 voice_ref() 拒掉整条请求（以前就是这么 400 的，桌宠下拉因此只能把这类
        # 音色整个滤掉 —— 用户在市场装的音色在面板里根本选不到）。
        tts_voice, borrowed = _tts_ref_for(want, rvc_voice)
        steps: list[str] = []
        if borrowed:
            steps.append(f"「{want}」没有参考音，语气借自 {borrowed}（音色由 RVC 决定，不影响像不像）")
        if req.no_rvc:
            steps.append("按选择跳过 RVC：纯 TTS 零样本克隆音色")
        elif not rvc_voice:
            steps.append("⚠ 没找到对应 RVC 音色，未换声（会是普通播音腔）")

        # 2026-09-23 分段发送：长文**逐句合成 → 实测时长装箱 → 一次发一批 ≤54s 的 wav**。
        # 背景：微信单条语音 60s 硬上限，到点自动结束并发送，后半段静默丢失。
        # 老实现是整段一次性 synth_wav → 一个 wav → 超 60s 必被截断（用户实测报的问题）。
        # 短文本（一句话装得下）走同一段代码，只是 plan 只有一段 —— 不另开分支，
        # 否则"分包修好了、单条还漏着"。
        _t1 = time.time()
        wavs, steps = _split_for_budget(
            req.text,
            tts_voice,
            want,
            rvc_voice,
            req.pitch,
            req.index_rate,
            steps,
        )
        steps.insert(
            0,
            f"合成分包：{len(wavs)} 条，用时 {time.time()-_t1:.1f}s"
            f"（逐句合成，实测时长装箱到 ≤{MAX_CHUNK_S:.0f}s）",
        )
        if rvc_voice:
            steps.append(f"RVC 换声 → {rvc_voice}")
        if len(wavs) == 1:
            # 单条：走 _do_send（它内部就是"只有一条的批次"），保持既有返回形状不变
            _t2 = time.time()
            res = _do_send(SendVoiceReq(wav=wavs[0].name), pre_apply=apply_task)
            steps.append(f"微信录制发送（用时 {time.time()-_t2:.1f}s）")
            if isinstance(res, dict):
                res["steps"] = steps + list(res.get("steps", []))
                res["wav"] = wavs[0].name
                res["total_chunks"] = 1
                res["sent_chunks"] = 1 if res.get("outcome") == "ok" else 0
                res["wavs"] = [wavs[0].name]
            elif apply_task is not None:
                # 早退路径（404 等）没走到 pre_apply 消费点，必须收尾还原声卡
                apply_task.abandon()
            return res

        # 多条：走批次内核 —— 环境准备与声卡还原**整批只做一次**（D7）。
        # 每条都切一次卡会白等 N×6s，而且反复切卡本身就是故障源。
        _t2 = time.time()
        res = _send_batch(wavs, pre_apply=apply_task)
        steps.append(f"微信录制发送 {len(wavs)} 条（用时 {time.time()-_t2:.1f}s）")
        res["steps"] = steps + list(res.get("steps", []))
        res["wav"] = wavs[0].name
        res["voice_id"] = want
        res["rvc_voice"] = rvc_voice
        res["tts_voice"] = tts_voice
        res["borrowed"] = borrowed
        # 失败时回传"剩下的原文"（D12），界面可据此提供「继续发剩下的」
        if res.get("failed_index") is not None:
            res["remaining_wavs"] = [w.name for w in wavs[res["failed_index"] :]]
        # 批次结果可能不是 ok（partial / failed / manual_fallback），仍返回 200：
        # 前端按 outcome 展示，避免"发了 2 条成功 1 条失败"被当成整条请求失败。
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


@router.get("/precheck")
def precheck():
    """发送前自检（**只读**，不动声卡、不碰微信）：这次发送会不会重启微信、为什么。

    排查「发出去的语音是静音」先打这个，比真发一条快得多，也不会打断对方。
    """
    need, why = _need_wechat_restart()
    probe = wproc.input_device_probe()
    procs = wproc.list_wechat_processes()
    exe = wproc.resolve_wechat_exe()
    wins = wproc.enum_wechat_windows()
    return {
        "ok": True,
        "restart_mode": _restart_mode(),
        "restart_needed": need,
        "reason": why,
        "wechat_running": bool(procs),
        "wechat_pids": [p["pid"] for p in procs],
        "wechat_exe": str(exe) if exe else None,
        "main_window_area": (wins[0]["area"] if wins else 0),
        "min_chat_area": wproc.MIN_CHAT_AREA,
        "last_input_device": probe.get("device"),
        "device_source": probe.get("file"),
        "target_keyword": _device_keyword(),
        "hint": (
            "微信会先被重启，再切麦克风到 CABLE Output"
            if need
            else "不会重启微信，直接切麦克风到 CABLE Output"
        ),
    }


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
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"加载实时变声模块失败: {exc}", "steps": steps},
        )
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
                return JSONResponse(
                    status_code=500,
                    content={"ok": False, "error": f"启动实时变声失败: {detail}", "steps": steps},
                )
            steps.append("已启动实时变声（模型加载中，稍等片刻）")
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": f"启动实时变声失败: {exc}", "steps": steps},
        )
    return {
        "ok": True,
        "steps": steps,
        "hint": "微信录音已切到 CABLE Output，变声运行中",
        "hint2": "去微信按住 Alt 说话，说完松开即发送",
        "warn": "若录到原声/电音，请把系统录音设备改成 CABLE Output",
    }


class PlayToCableReq(BaseModel):
    wav: str | None = None  # outputs/ 下的文件名；缺省=最近 TTS
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
        wav = Path(req.wav) if Path(req.wav).is_absolute() else _find_wav(req.wav)
        if wav is None or not wav.exists():
            return JSONResponse(
                status_code=404, content={"ok": False, "error": f"找不到音频 {req.wav}"}
            )
    else:
        wav = session_out.newest_tts()
        if wav is None:
            return JSONResponse(
                status_code=404,
                content={"ok": False, "error": "还没有 TTS 产物，先在网页上合成一条语音"},
            )
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
            [
                str(RVC_VENV_PY),
                "-c",
                _PLAY_SCRIPT,
                str(wav),
                OUTPUT_DEVICE_KEYWORD,
                str(lead),
                str(TAIL_S),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
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
        with contextlib.suppress(Exception):
            _run_audio("restore")
        return JSONResponse(
            status_code=500, content={"ok": False, "error": str(exc), "steps": steps}
        )
    return {
        "ok": True,
        "wav": wav.name,
        "duration_s": round(duration, 1),
        "lead_s": lead,
        "steps": steps,
        "hint": (
            f"音频已播放到 CABLE（{duration:.1f}s，含 {lead}s 静音头）。"
            f"请在这 2 秒静音头内到微信按住 Alt 开始说话，"
            f"录到这段音频后松开 Alt 发送。"
        ),
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
        return JSONResponse(status_code=500, content={"ok": False, "error": f"停止播放失败: {exc}"})
    return {"ok": True, "stopped": True, "detail": "已停止向 CABLE 播放，声卡将自动还原"}


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


def _uia_verify_sent(before_msg: str | None, expect_s: float, before_count: int = -1) -> list[str]:
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
    note = (
        f"条数 {before_count}→{count}，确认新增"
        if added
        else f"条数 {before_count}→{count}，UIA 仅暴露可视区故不增属正常"
    )
    out = [f"UIA 校验：最新语音 {msg}（{note}）"]
    if secs is not None and expect_s >= 3 and secs > max(expect_s * 3, expect_s + 20):
        out.append(f"⚠ 时长异常（{secs:.0f}s 远大于预期 {expect_s:.1f}s），疑似 60s 截断复发")
    elif secs is not None and expect_s >= 3 and secs > expect_s + 4:
        # 静音头尾吞掉太多：提示而不是判失败
        out.append(f"提示：录得比音频长 {secs - expect_s:.1f}s（首尾静音），可下调 LEAD_S/TAIL_S")
    return out


def _prepare_env(pre_apply: _PendingApply | None = None, steps: list[str] | None = None) -> dict:
    """录音环境准备：必要时重启微信，再把默认麦克风切到 CABLE Output。**批次内只做一次。**

    pre_apply：send_text 预热的切卡任务（与 TTS 并行）。传了就只 .result() 等它收尾
    （不再同步跑第二次 apply）；不传（直连 /send_voice、tools/*）保持原地同步切卡，行为不变。

    ⚠️ 调用方必须在 `finally` 里配对 `_safe_restore()`（或用 _send_batch，它已经管了）——
    否则用户的默认麦会留在 CABLE 上。

    从 _do_send 第 2 步抽出，行为完全相同（2026-09-23 分段重构）。
    """
    steps = steps if steps is not None else []
    _tw = time.time()
    _ta = time.time()
    if pre_apply is not None:
        env = pre_apply.result()
        if isinstance(env, dict) and env.get("kind") == "recording_env":
            steps.append(
                f"[{time.time()-_tw:.1f}s] {env['summary']}"
                f"（准备已与 TTS 并行，此处仅等 {time.time()-_ta:.1f}s）"
            )
        else:  # 兼容旧契约（测试里的假任务 / 老调用方）
            steps.append(
                f"[{time.time()-_tw:.1f}s] 麦克风已切到 CABLE Output"
                f"（切卡已与 TTS 并行，此处仅等 {time.time()-_ta:.1f}s）"
            )
    else:
        env = _prepare_recording_env()
        steps.append(f"[{time.time()-_tw:.1f}s] {env.get('summary', '麦克风已切到 CABLE Output')}")
    return env


def _find_wav(name: str) -> Path | None:
    """裸名 → wav 路径：**先会话目录、后 outputs 根**（`session_out.find` 的薄封装）。

    为什么不能直接 `cfg.OUTPUTS_DIR / name`：合成产物现在默认落在会话目录
    （`outputs/.session/`），只有用户点过「保存」的才在 outputs 根。只认根目录会
    让"刚合成完就发送"报「找不到音频」—— 而那正是最常用的那条路。

    返回 None 表示两处都没有；调用方按自己的语境报 404。
    """
    return session_out.find(name)


def _resolve_wav(req: SendVoiceReq):
    """定位要发的 wav：指定名 → 会话目录/roots 精确匹配；否则最近的 tts_*.wav。

    返回 (Path, None) 或 (None, JSONResponse 错误)。从 _do_send 第 1 步抽出。
    """
    if req.wav:
        wav = Path(req.wav) if Path(req.wav).is_absolute() else _find_wav(req.wav)
        if wav is None or not wav.exists():
            return None, JSONResponse(
                status_code=404,
                content={"ok": False, "outcome": "failed", "error": f"找不到音频 {req.wav}"},
            )
        return wav, None
    wav = session_out.newest_tts()
    if wav is None:
        return None, JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "outcome": "failed",
                "error": "还没有 TTS 产物，先在网页上合成一条语音",
            },
        )
    return wav, None


def _record_and_send(
    wav: Path,
    duration: float,
    uia_active: bool | None = None,
    before_msg: str | None = None,
    before_count: int = -1,
    steps: list[str] | None = None,
    env: dict | None = None,
    index: int | None = None,
    total: int | None = None,
    gap_s: float = 0.0,
    reuse_restore: bool = False,
) -> dict:
    """录制并发送**一条** wav（= 原 _do_send 的第 3~6 步 + 校验 + 落历史）。

    这是批次与单条共用的内核：`_do_send` 就是"只有一条的批次"，
    两条路径走同一段代码 —— 避免"批次修好了、单条还漏着"。

    ⚠️ 调用方负责**环境准备**（_prepare_env）与**声卡还原**：本函数只做一条的
    录制/发送/落历史。单条路径（_do_send）自己管还原；批次路径（_send_batch）
    在 finally 里统一还原一次（reuse_restore=False 时必须还原）。

    ``uia_active``/``before_msg``/``before_count``：由调用方在**本段开始前**取好的
    UIA 基线。**必须每段重取** —— 沿用第一条的 before_count 会让第二条起永远判成
    "已新增"（基线没动），见 D10。

    ``gap_s``：本段开始前等录音浮层消失 + 这一小段停顿（段间衔接，见 D9）。

    返回与 _do_send 同构的 dict。**不抛异常**：异常内部处理并返回
    outcome=failed / manual_fallback（与 _do_send 的历史契约一致）。
    """
    steps = steps if steps is not None else []
    env = env if isinstance(env, dict) else {}
    bind_warning = env.get("warning", "")
    _tw = time.time()
    proc = None
    prefix = f"[第 {index+1}/{total} 条] " if (index is not None and total is not None) else ""

    # 段间：先等上一次的录音浮层消失（它是 _trigger_record 的前置条件，残留会让后续全失败），
    # 再留 gap_s 停顿。**不要**用固定 sleep 当同步手段（速查表第 55 条）。
    if index is not None and index > 0:
        _await_overlay_gone(gap_s)
        steps.append(f"{prefix}上一段录音浮层已消失，停顿 {gap_s:.1f}s 后继续")

    if not steps or not any("音频:" in s for s in steps):
        steps.append(f"{prefix}音频: {wav.name}（{duration:.1f}s）")

    try:
        # 3) 起播放（常驻 worker 复用 / 一次性子进程）。冷导入若发生，与下面的 UI 准备并行。
        _t_play = time.time()
        proc = _start_play(wav)

        # --- 并行：播放子进程冷导入（worker 已预热则≈0）期间，把微信前台化 + 找话筒算好 ---
        # 这段 UI 准备 ~0.5-1s，原本串行排在切卡/导入之后纯等；现在与播放导入重叠，省 ~1s。
        _ui: dict = {}

        def _prep_ui() -> None:
            try:
                h = _foreground_wechat()
                _ensure_onscreen(h)  # 防止窗口底边超屏被任务栏遮挡
                r = _window_rect(h)
                _ui["hwnd"] = h
                _ui["rect"] = r
                _ui["mic"] = _find_mic_icon(r) or _mic_point(r)
            except Exception as e:
                _ui["err"] = e

        _prep_t = threading.Thread(target=_prep_ui, daemon=True)
        _prep_t.start()
        play_ready = _wait_play_start(proc)  # 主线程等导入/ready（与 _prep_ui 并行）
        _prep_t.join()
        if play_ready:
            steps.append(
                f"{prefix}[{time.time()-_tw:.1f}s] 播放就绪（冷导入 {time.time()-_t_play:.1f}s，"
                f"已与 UI 准备并行）"
            )
        else:
            steps.append(f"{prefix}⚠ 未等到播放开始信号，仍按原计划录音（开头可能被削）")

        # 4) 点语音按钮开始录制（UI 已并行准备好，直接复用；准备失败则退回原路径重算）
        _trigger_record(ui=_ui if not _ui.get("err") else None)
        if RECORD_METHOD == "mic":
            steps.append(
                f"{prefix}[{time.time()-_tw:.1f}s] "
                + (
                    "UIA 已点击语音按钮，开始录音"
                    if _record_via == "uia"
                    else "已点击话筒图标，开始录音"
                )
            )
        else:
            steps.append(f"{prefix}[{time.time()-_tw:.1f}s] 已按住 {RECORD_KEY.upper()} 开始录音")

        # 5) 等 wav 真正播完（播完 = 子进程退出 / worker 回 done）
        _wait_play_done(proc, duration)
        steps.append(f"{prefix}[{time.time()-_tw:.1f}s] 已播放 {duration:.1f}s 到微信录音")

        # 6) 结束录音并发送
        #    播放脚本尾部已自带 TAIL_S 静音，这里只留极小缓冲给声卡驱动（再多就是白录空白）
        time.sleep(0.15)
        via = _record_via
        sent = _finish_record()
        if sent:
            steps.append(
                f"{prefix}[{time.time()-_tw:.1f}s] "
                + {
                    "uia": "UIA 点击语音按钮录音 → 已点发送钮，语音已发送",
                    "realclick": "已点击语音按钮 → 已点发送钮，语音已发送",
                    "postmsg": "已点浮层发送按钮，语音已发送",
                }.get(via, "语音已发送")
            )
            # 发送已成功：先落历史（后台写回校验/还原结果都依赖它）
            _append_history(wav, duration, "ok", warning=bind_warning, total_chunks=total or 1)
            hist_appended = True
            # UIA 校验只是安全网，放后台线程不阻塞返回（省 ~0.5-3s）
            if uia_active:
                # 路径在起线程时**就地取一次**存进局部变量，由 lambda 闭包带走。
                # 不能让线程体自己去读全局 HISTORY_FILE：lambda 要等 _uia_verify_sent
                # （UIA 扫窗口）跑完才执行 _persist_verify，那时调用方作用域已退出，
                # 测试的 monkeypatch 也已撤销 → 读到真实 outputs/ 路径（见其 docstring）。
                hist_path = HISTORY_FILE
                threading.Thread(
                    target=lambda: _persist_verify(
                        _uia_verify_sent(before_msg, duration, before_count), hist_path
                    ),
                    daemon=True,
                ).start()
                steps.append(f"{prefix}UIA 发送后校验：后台线程进行中（结果写入发送历史）")
            if reuse_restore:
                # 批次路径：还原由 _send_batch 的 finally 统一做一次，这里不动声卡
                steps.append(f"{prefix}声卡还原交由批次统一处理（批次结束一次性还原）")
            else:
                # 声卡还原 ~3s，放后台线程：下一步切卡由 _restore_lock 等它收尾，不抢设备，
                # 也不阻塞本次返回（省 ~3s 同步等待）。
                # 把本次的历史文件路径**传进去**（见 _restore_async docstring：线程异步，
                # 让它自己去读全局 HISTORY_FILE 会跨越作用域、读写到不同文件）。
                threading.Thread(target=_restore_async, args=(HISTORY_FILE,), daemon=True).start()
                steps.append(f"{prefix}[{time.time()-_tw:.1f}s] 声卡还原已交后台线程（不阻塞返回，约 3s）")
            return {
                "ok": True,
                "outcome": "ok",
                "method": RECORD_METHOD,
                "wav": wav.name,
                "duration_s": round(duration, 1),
                "steps": steps,
                "restored": None,
                "warning": bind_warning,
                "_history": (
                    True
                    if hist_appended
                    else _append_history(
                        wav, duration, "ok", warning=bind_warning, total_chunks=total or 1
                    )
                ),
            }

        steps.append(f"{prefix}未找到发送按钮，已取消录音（本次未发送）")
        restored, restore_err = (False, "") if reuse_restore else _safe_restore()
        return {
            "ok": True,
            "outcome": "cancelled",
            "method": RECORD_METHOD,
            "wav": wav.name,
            "duration_s": round(duration, 1),
            "steps": steps,
            "restored": restored,
            "restore_error": restore_err,
            "_history": _append_history(
                wav, duration, "cancelled", warning=bind_warning, total_chunks=total or 1
            ),
        }
    except Exception as exc:
        # 失败也要：⓪掐掉后台播放（否则会一直往 CABLE 灌声音）
        #            ①松开录音键/鼠标（防止按住不放卡死）②还原声卡（reset 兜底）
        try:
            if proc is not None and proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        with contextlib.suppress(Exception):
            _finish_record()
        restored, restore_err = (False, "") if reuse_restore else _safe_restore()
        # 自动降级：模拟按键/播放失败 → 引导式手动发送（播放到 CABLE，用户自己按 Alt）
        if AUTO_FALLBACK:
            fb = _guided_fallback(wav, duration, steps)
            return {
                "ok": True,
                "outcome": "manual_fallback",
                "wav": wav.name,
                "duration_s": round(duration, 1),
                "steps": steps + fb["steps"],
                "restored": restored,
                "restore_error": restore_err,
                "auto_error": str(exc),
                "fallback": fb,
                "_history": _append_history(wav, duration, "manual_fallback", warning=bind_warning),
            }
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "outcome": "failed",
                "error": str(exc),
                "steps": steps,
                "restored": restored,
                "restore_error": restore_err,
            },
        )


def _overlay_visible() -> bool:
    """录音浮层当前是否还在（UIA 优先，像素兜底）。任何异常都当"看不见"。"""
    if _uia_ready():
        try:
            if _uia.overlay_exists():
                return True
        except Exception as e:
            logger.debug("[wechat] UIA 浮层检测失败，本轮到像素: %s", e)
    h = _foreground_wechat()
    return bool(h and _find_green_send(_window_rect(h), retries=1))


def _await_overlay_gone(gap_s: float = BATCH_GAP_S, timeout: float | None = None) -> bool:
    """等微信的录音浮层消失，再留 gap_s 停顿。返回是否等到。

    为什么必须等：`_trigger_record()` 的前置条件是**上一次的录音浮层已经消失**
    —— 浮层残留会让后续每一次点击都落在浮层上（速查表第 11 条：「前一次失败后，
    后续全部失败」）。批次内连续发送正是最容易被这条打中的场景。

    **不用固定 sleep 当同步手段**（速查表第 55 条：固定 sleep 换轮询）。
    """
    if timeout is None:
        timeout = gap_s + 2.0
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if not _overlay_visible():
                break
        except Exception:
            break
        time.sleep(0.2)
    if gap_s > 0:
        time.sleep(gap_s)
    return True


def _check_budget(duration: float) -> str:
    """单条预算护栏：返回空串 = 可以发；否则返回拒绝原因。

    ⚠️ **这是这条 bug 唯一的防复发机制。** 任何绕过分包的调用方（直连 /send_voice、
    tools/*、将来的新入口）都在这里被拦住，而不是发一条注定被 60s 截断的语音。

    判据就是 §2 那条：`PLAY_LEAD_S + audio + TAIL_S + FINISH_LAG_S ≤ 60`，
    **并且** `audio ≤ MAX_CHUNK_S`（后者已经把 SAFETY_S 余量扣掉了）。
    为什么两条都要：第一条是物理上限（防止真的到点被切），第二条是**余量纪律** ——
    只查第一条的话，55.4s 会"恰好卡在 59.9s"通过，而 SAFETY_S 里那 4.6s 就是为了
    应付"UIA 秒数取整 / TTS 语速波动 / 驱动抖动 / 微信自身计数"的。
    赌这 4.6s 换来的只是"少一条气泡"，代价却是"静默截断一整句"。

    ⚠️ 用 `eps` 比较：`MAX_CHUNK_S` 是从算式推出来的浮点数，`54.0 <= 54.00000000000001`
    成立但反过来不成立；不设 eps 会让"恰好等于预算"这个合法边界被误拒。

    变异测试要求：把本函数的判据删掉，断言"超长单条被拒发"的用例**必须变红**。
    """
    eps = 1e-6
    if duration <= MAX_CHUNK_S + eps and PLAY_LEAD_S + duration + TAIL_S + FINISH_LAG_S <= MAX_MSG_S + eps:
        return ""
    need_chunks = _min_chunks_for(duration)
    return (
        f"这段音频 {duration:.1f}s 超过单条上限（预算 {MAX_CHUNK_S:.0f}s，"
        f"微信硬上限 {MAX_MSG_S:.0f}s），直接发会被平台静默截断、后半段丢失。"
        f"请改用分段发送（这段会切成约 {need_chunks} 条语音）。"
    )


def _min_chunks_for(duration: float, max_s: float | None = None) -> int:
    """至少要切成几条才装得下（护栏提示语用；批量场景的准确条数以 pack_chunks 为准）。"""
    max_s = max_s if max_s is not None else MAX_CHUNK_S
    if max_s <= 0:
        return 1
    return max(1, int(duration / max_s) + (1 if duration % max_s else 0))


def _do_send(req: SendVoiceReq, pre_apply: _PendingApply | None = None):
    """执行一次微信语音自动发送。pre_apply：send_text 预热的切卡任务（与 TTS 并行）。

    传了 pre_apply 就只 .result() 等它收尾（不再同步跑第二次 apply）；
    不传（直连 /send_voice、tools/*）保持原地同步切卡，行为不变。

    2026-09-23 起内部改为 `_prepare_env` → `_record_and_send`（**签名与返回结构一个字没改**），
    于是"单条"就是"只有一条的批次" —— 批次修好了单条不会还漏着。被 3 个真机回归脚本
    （tools/tts_and_send.py、wechat_e2e_check.py、wechat_regression.py）与 4 个测试文件
    共 20+ 条用例依赖，改动前先跑它们。
    """
    steps: list[str] = []

    # 1) 定位 wav
    wav, err = _resolve_wav(req)
    if err is not None:
        return err
    duration = _wav_duration(wav)
    steps.append(f"音频: {wav.name}（{duration:.1f}s）")

    # 1.5) ★ 预算护栏：超预算就拒发，绝不发一条注定被截断的。
    #      这是"任何绕过分包的调用方也不会静默出错"的兜底。
    if (reject := _check_budget(duration)):
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "outcome": "too_long",
                "error": reject,
                "wav": wav.name,
                "duration_s": round(duration, 1),
                "max_chunk_s": MAX_CHUNK_S,
                "steps": steps,
            },
        )

    # 录音环境告警由 _record_and_send 从 env 里取（它负责拼 steps 与落历史），这里不重复取。
    env = _prepare_env(pre_apply, steps)

    # 2.5) UIA 就绪判定 —— **必须在录音环境准备之后**（微信此时才真的起来了）。
    #      见 _await_uia_active 的 docstring：判早了会让所有"含重启微信"的发送
    #      一律退化到按坐标盲点的像素链路（test_do_send_judges_uia_after_recording_env 守这条）。
    uia_active = _await_uia_active()
    steps.append("UIA 结构化访问就绪" if uia_active else "UIA 不可用（走像素链路）")
    before_msg, before_count = None, -1
    if uia_active:
        try:
            before_msg = _uia.latest_voice_message()
            before_count = len(_uia.voice_messages() or [])
        except Exception:
            before_msg, before_count = None, -1

    # 3~6) 录制并发送（含失败降级）
    return _record_and_send(
        wav,
        duration,
        uia_active=uia_active,
        before_msg=before_msg,
        before_count=before_count,
        steps=steps,
        env=env,
        reuse_restore=False,  # 单条路径自己还原（保持原行为：后台线程还原）
    )


def _send_batch(
    wavs: list[Path],
    pre_apply: _PendingApply | None = None,
    on_progress=None,
) -> dict:
    """**有序 wav 列表** → 一次录音环境准备 + 循环发送 + 统一还原。

    这是 D7/D13 的落点：入参是"有序 wav 列表"，**分包只是造出这个列表的策略**。
    文字入口造出逐句装箱的结果，音频入口造出静音切分的结果，两者都走这里；
    需要"两个来源一起发"时由前端拼成一个列表交过来 —— **绝不在这里串联两次批次**
    （否则切卡两次、可能重启微信两次，还会被 _send_lock 的 409 挡下第二次）。

    失败策略（D12）：**失败即停**，并回传 remaining_text 供"继续发剩下的"。
    继续发看起来"多送几条"，但失败通常意味着环境已经坏了（声卡/浮层/微信被抢焦点），
    后面几条只会变成静音或噪声音频 —— 发出去一条错的不如不发。
    """
    steps: list[str] = []
    total = len(wavs)
    if total == 0:
        return {
            "ok": True,
            "outcome": "ok",
            "total_chunks": 0,
            "sent_chunks": 0,
            "failed_index": None,
            "steps": ["没有需要发送的音频"],
        }

    results: list[dict] = []
    env = _prepare_env(pre_apply, steps)
    try:
        for k, wav in enumerate(wavs):
            duration = _wav_duration(wav)
            # ★ 每段重取 UIA 基线（D10）：沿用第一条会让第 2 条起永远判成"已新增"
            uia_active = _await_uia_active()
            before_msg, before_count = None, -1
            if uia_active:
                try:
                    before_msg = _uia.latest_voice_message()
                    before_count = len(_uia.voice_messages() or [])
                except Exception:
                    before_msg, before_count = None, -1

            seg_steps: list[str] = []
            res = _record_and_send(
                wav,
                duration,
                uia_active=uia_active,
                before_msg=before_msg,
                before_count=before_count,
                steps=seg_steps,
                env=env,
                index=k,
                total=total,
                gap_s=BATCH_GAP_S,
                reuse_restore=True,  # 还原统一由本函数的 finally 做一次
            )
            results.append(res)
            steps += seg_steps
            if on_progress:
                with contextlib.suppress(Exception):
                    on_progress(k + 1, total, duration)
            if not isinstance(res, dict) or res.get("outcome") != "ok":
                return _batch_result(results, wavs, steps, failed_index=k)
        return _batch_result(results, wavs, steps)
    finally:
        # ★ 一次，且失败也要还原（D7）。reuse_restore=True 时各段不再各自还原。
        _safe_restore()


def _batch_result(
    results: list[dict], wavs: list[Path], steps: list[str], failed_index: int | None = None
) -> dict:
    """把逐段结果折成批次响应（§7 的出口形状）。"""
    sent = sum(1 for r in results if isinstance(r, dict) and r.get("outcome") == "ok")
    outcomes = [r.get("outcome") for r in results if isinstance(r, dict)]
    # 整批只要有一条是 manual_fallback，就要让用户知道（它不是失败，但要人按 Alt）
    if failed_index is None:
        outcome = "manual_fallback" if "manual_fallback" in outcomes else "ok"
    else:
        outcome = "partial" if sent else (outcomes[-1] if outcomes else "failed")
    total = len(wavs)
    return {
        "ok": outcome != "failed",
        "outcome": outcome,
        "total_chunks": total,
        "sent_chunks": sent,
        "failed_index": failed_index,
        "wavs": [w.name for w in wavs],
        "duration_s": round(sum(r.get("duration_s") or 0 for r in results if isinstance(r, dict)), 1),
        "warning": next(
            (r.get("warning") for r in results if isinstance(r, dict) and r.get("warning")), ""
        ),
        "steps": steps,
    }


def _guided_fallback(wav: Path, duration: float, steps: list[str]) -> dict:
    """降级引导：把 wav 播到 CABLE（带 2s 静音头），提示用户手动按 Alt。返回引导结果。"""
    try:
        _run_audio("apply")
        _play_to_cable(wav, duration)
        _safe_restore()
        return {
            "steps": ["已降级：音频已播到 CABLE，请到微信手动录完松开发送"],
            "hint": "到微信按住 Alt（或长按输入框右下角话筒图标）说话，录到这段音频后松开发送",
            "lead_s": 2.0,
        }
    except Exception as e:
        return {
            "steps": ["降级播放失败"],
            "hint": f"自动降级也失败（{e}），请改用「手动发送」",
            "lead_s": 2.0,
        }


# ---------------- 发送历史（桌宠「最近发送」用） ----------------

HISTORY_FILE = cfg.OUTPUTS_DIR / "wechat_send_history.json"
HISTORY_MAX = 20


def _append_history(
    wav: Path,
    duration_s: float,
    outcome: str = "ok",
    warning: str = "",
    total_chunks: int = 1,
) -> bool:
    """把一次发送记进历史（最多 HISTORY_MAX 条，覆盖写）。outcome: ok/manual_fallback/failed。

    warning：录音环境告警（典型是 RESTART=0 下微信绑的不是 CABLE → 可能录成静音）。
    2026-09-18 加入：此前该告警只出现在 API 响应的 steps/summary 里，不落库，于是
    17:35 那条静音语音事后在发送历史里查不到任何线索 —— 前端把它当成了"残留旧文案"。
    现在它随记录一起持久化，`GET /history` 与桌宠「最近发送」都能看到。

    total_chunks（2026-09-23 分段发送）：这一批总共几条、本条是第几条。
    **必须落库**，否则桌宠的兜底看门狗只知道"又出现了一行历史"，
    分不清"整批发完了"和"才发到第 1 条" —— 会在第 1 条落地时就打绿勾收尾，
    而此时后面的还在录。落库后看门狗能直接数够 `total_chunks` 条就收尾。
    """
    try:
        hist = []
        if HISTORY_FILE.exists():
            hist = json.loads(HISTORY_FILE.read_text("utf-8"))
        rec = {
            "wav": wav.name,
            "duration_s": round(duration_s, 1),
            "ts": int(time.time()),
            "outcome": outcome,
        }
        if warning:
            rec["warning"] = warning
        if total_chunks > 1:
            # 单条不进库这个字段：省得每行都多一个恒为 1 的噪声字段，
            # 也让"长文分段"在历史里一眼可辨（前端据此显示"3 条"）
            rec["total_chunks"] = total_chunks
        hist.append(rec)
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
    wav = session_out.newest_tts()
    if wav is None:
        return {"ok": False, "error": "还没有 TTS 产物"}
    return {
        "ok": True,
        "wav": wav.name,
        "duration_s": round(_wav_duration(wav), 1),
        # 会话产物在 `.session/` 下 —— 自己拼 `outputs/{name}` 会让预览 404
        "url": f"/api/media/outputs/{session_out.rel_url(wav.name)}",
    }
