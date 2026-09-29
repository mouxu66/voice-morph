"""按住说话 · 抓**真实麦克风**（WASAPI input，支持 start/stop）。

用途：桌宠「按住说话」——按住时录你本机麦克风，松开后交给 `rvc_convert` 换声，
再走既有微信发送链路。这样**不需要开实时变声、也不碰虚拟声卡切换**
（切声卡的物理代价是 3~6 秒，按住几秒说话根本来不及）。

★ 为什么必须显式挑"非虚拟"的输入设备，而不能用系统默认录音设备：

    实时变声开着时，**系统默认录音设备已经被切到 CABLE Output** ——
    微信正是靠这个才录到变声后的声音（见 `wechat_voice.py` 的切卡逻辑）。
    如果这里按"默认"去录，录到的就是**已经变好的声音**，再换一次声 = 双重变声，
    音色会比预期更远、还多一层伪影，而且症状是"听着有点怪但说不清哪怪"。

    所以本模块与 `loopback_capture.py` 是**互为镜像**的两件事：
      · loopback_capture：抓默认**播放**设备 → 录系统正在播的声音（内录）；
      · mic_capture     ：抓默认**录音**设备，但排除虚拟声卡 → 录你说的声音。

    两者都不用 sounddevice —— pet.companion 的 extras.python 里声明的是
    **pyaudiowpatch**，用未声明的依赖正是本项目踩过的 CI 红坑
    （`docs/犯错档案-工程.md` 里 Pillow/comtypes 那次）。
"""

from __future__ import annotations

import threading
import wave
from pathlib import Path

import pyaudiowpatch as pyaudio


class MicCaptureError(Exception):
    """麦克风录制失败（无可用输入设备 / WASAPI 异常 / 状态不对）。"""


#: 名字里出现这些片段就当**虚拟声卡**，不作为"真实麦克风"候选。
#: 与播放端那个双候选关键词（`VB-Audio Virtual Cable|CABLE Input`）不是一回事 ——
#: 那边是"要命中"，这边是"要避开"，所以单独一份、不参与
#: `tests/test_cable_keyword_consistency.py` 的一致性口径。
VIRTUAL_MIC_HINTS = (
    "cable",
    "vb-audio",
    "voicemeeter",
    "virtual",
    "loopback",
    "stereo mix",
    "立体声混音",
    "line in",
    "what u hear",
)

#: 单次"按住"的硬上限（秒）。微信单条语音上限 60 秒；留 1 秒余量给收尾。
#: 到点由采集线程自己停并落盘 —— 不能让"松开"成为唯一出口：
#: 用户按住不放、或手指滑出按钮导致 up 事件丢失时，录音必须能自己收住。
MAX_HOLD_SECONDS = 59.0
#: 「太短」判定线（秒）：低于它视为误触/按空，端点据此给出可读提示。
#: ⚠️ 它**不是**上限的下限 —— 两者用途不同，别拿它去钳 max_seconds（会静默把
#: 一个合法的短录制拉长到 0.3s）。上限另用一个极小的地板防退化。
MIN_HOLD_SECONDS = 0.3
_LIMIT_FLOOR_SECONDS = 0.05

#: 每次读取的帧数：480 @48kHz ≈ 10ms，够小以保证 stop() 的响应，也够大以不空转。
_CHUNK = 480


def is_virtual_device(name: str) -> bool:
    """设备名是否像虚拟声卡（→ 不能当作麦克风用）。"""
    low = (name or "").strip().lower()
    return any(h in low for h in VIRTUAL_MIC_HINTS)


def input_devices(p: "pyaudio.PyAudio") -> list[dict]:
    """WASAPI 下所有**真实输入**设备（排除 loopback 伪设备），按枚举顺序。"""
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError as e:
        raise MicCaptureError(f"WASAPI 不可用: {e}") from e
    host = wasapi["index"]
    out: list[dict] = []
    for dev in p.get_device_info_generator():
        if dev.get("hostApi") != host:
            continue
        if dev.get("isLoopbackDevice"):
            continue
        if int(dev.get("maxInputChannels") or 0) <= 0:
            continue
        out.append(dev)
    return out


def pick_mic_device(p: "pyaudio.PyAudio") -> dict:
    """挑一个真实麦克风：**非虚拟优先**，且优先系统默认录音设备（如果它不是虚拟的）。

    只有虚拟设备可用时**报错而不是将就** —— 录到 CABLE 上去会让用户听到
    双重变声的怪声音，而他完全无从判断问题出在哪。宁可他看到一句能看懂的话。
    """
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    except OSError as e:
        raise MicCaptureError(f"WASAPI 不可用: {e}") from e
    devs = input_devices(p)
    if not devs:
        raise MicCaptureError("找不到任何 WASAPI 输入设备（麦克风没插好或被占用）")

    default_idx = wasapi.get("defaultInputDevice")

    def name_of(d: dict) -> str:
        return str(d.get("name") or "")

    real = [d for d in devs if not is_virtual_device(name_of(d))]
    if not real:
        raise MicCaptureError(
            "只找到虚拟声卡、没有真实麦克风 —— 按住说话需要录你的真实麦克风"
            "（实时变声会把系统默认录音设备切到 CABLE，所以这里不能将就）"
        )
    for d in real:
        if d.get("index") == default_idx:
            return d
    return real[0]


def _finalize(frames: list[bytes], rate: int, channels: int, out_path: Path) -> Path:
    """把已采集的帧写成 16bit wav。帧为空也写（得到一个 0 秒文件，由调用方判定）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"".join(frames))
    return out_path


class MicRecorder:
    """一次"按住"的录音会话。

    生命周期：`start()` → （用户在按住）→ `stop()`。
    到 `max_seconds` 上限时**采集线程自己收尾**，`stop()` 之后再来取同一条产物，
    所以「按住不放」和「up 事件丢失」都不会留下一个永远在录的线程。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self.path: Path | None = None
        self.device_name = ""
        self.rate = 0
        self.error = ""
        self.auto_stopped = False
        self._started_at = 0.0

    # ---- 状态 ----
    @property
    def active(self) -> bool:
        """还在录（线程未收尾）。"""
        return self._thread is not None and not self._done.is_set()

    def elapsed_s(self, now: float) -> float:
        return max(0.0, now - self._started_at) if self._started_at else 0.0

    # ---- 开始 ----
    def start(self, out_path: Path, max_seconds: float = MAX_HOLD_SECONDS) -> dict:
        with self._lock:
            if self.active:
                raise MicCaptureError("已在录音中")
            limit = min(max(float(max_seconds), _LIMIT_FLOOR_SECONDS), MAX_HOLD_SECONDS)
            p = pyaudio.PyAudio()
            try:
                dev = pick_mic_device(p)
                rate = int(dev["defaultSampleRate"])
                channels = max(1, min(2, int(dev["maxInputChannels"]) or 1))
            except MicCaptureError:
                p.terminate()
                raise
            except Exception as e:  # noqa: BLE001
                p.terminate()
                raise MicCaptureError(f"选择麦克风失败: {e}") from e

            self._stop.clear()
            self._done.clear()
            self.error = ""
            self.auto_stopped = False
            self.path = out_path
            self.device_name = str(dev.get("name") or "")
            self.rate = rate

            self._thread = threading.Thread(
                target=self._run,
                args=(p, dev["index"], rate, channels, out_path, limit),
                name="mic-capture",
                daemon=True,
            )
            import time as _t

            self._started_at = _t.time()
            self._thread.start()
            return {
                "device": self.device_name,
                "rate": rate,
                "channels": channels,
                "max_seconds": limit,
            }

    def _run(self, p, index: int, rate: int, channels: int, out_path: Path, limit: float) -> None:
        frames: list[bytes] = []
        chunk = _CHUNK
        try:
            with p.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                frames_per_buffer=chunk,
                input=True,
                input_device_index=index,
            ) as stream:
                total = max(1, int(rate / chunk * limit))
                for _ in range(total):
                    if self._stop.is_set():
                        break
                    frames.append(stream.read(chunk, exception_on_overflow=False))
                else:
                    # for-else：跑满上限都没等到 stop → 是"到点自己收尾"，不是用户松开
                    self.auto_stopped = True
            _finalize(frames, rate, channels, out_path)
        except Exception as e:  # noqa: BLE001 —— 采集线程里任何异常都不能静默丢掉
            self.error = f"{type(e).__name__}: {e}"
        finally:
            with self._lock:
                try:
                    p.terminate()
                except Exception:  # noqa: BLE001
                    pass
            self._done.set()

    # ---- 结束 ----
    def stop(self, timeout: float = 8.0) -> tuple[Path, float]:
        """停止并返回 (wav 路径, 时长秒)。已自动收尾时也返回同一条产物。"""
        thread = self._thread
        if thread is None:
            raise MicCaptureError("没有正在进行的录音")
        self._stop.set()
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise MicCaptureError("停止录音超时（采集线程没退出）")
        self._done.set()
        if self.error:
            raise MicCaptureError(f"录音失败: {self.error}")
        if self.path is None or not self.path.exists():
            raise MicCaptureError("录音未产出文件")
        return self.path, _wav_duration(self.path)


def wav_duration(path: Path) -> float:
    """读 wav 时长（秒）；读不出来返回 0（调用方据此判"太短"）。

    公开名字给的是 **capture_api 的"手机当麦克风"上传**：它拿到的是 ffmpeg 转出来的
    wav，也要用同一个"太短/太长"口径，不能两边各写一个读法。
    """
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getframerate() <= 0:
                return 0.0
            return wf.getnframes() / wf.getframerate()
    except Exception:  # noqa: BLE001
        return 0.0


#: 旧名（本模块内部与既有测试都叫它 `_wav_duration`）：同一条函数，不另存实现。
_wav_duration = wav_duration


# ---- 单例：一次只允许一个"按住" ----
_recorder: MicRecorder | None = None
_recorder_lock = threading.Lock()


def current() -> MicRecorder | None:
    with _recorder_lock:
        return _recorder


def start(out_path: Path, max_seconds: float = MAX_HOLD_SECONDS) -> dict:
    """开始录音（同时只允许一个）。返回设备信息，见 `MicRecorder.start`。"""
    global _recorder
    with _recorder_lock:
        if _recorder is not None and _recorder.active:
            raise MicCaptureError("已在录音中")
        rec = MicRecorder()
        info = rec.start(out_path, max_seconds)
        _recorder = rec
        return info


def stop(timeout: float = 8.0) -> tuple[Path, float, bool]:
    """停止录音，返回 (路径, 时长, 是否到上限自动收尾)。"""
    global _recorder
    with _recorder_lock:
        rec = _recorder
    if rec is None:
        raise MicCaptureError("没有正在进行的录音")
    path, dur = rec.stop(timeout=timeout)
    auto = rec.auto_stopped
    with _recorder_lock:
        _recorder = None
    return path, dur, auto
