"""按住说话的麦克风采集（`mic_capture.py`）—— 全部用假 PyAudio，不碰真声卡。

为什么这组值得写：`mic_capture` 里那个「排除虚拟声卡」的判断，错了**不会有任何报错**，
症状是"变声后听着有点怪但说不清哪怪"——因为录到的是 CABLE 上已经变好的声音，
再换一次声就是双重变声。也就是说：这一条判据失效时，用户拿到的是一个**安静的错误结果**
（能播、能发、只是音色不对），比抛异常难查得多。

同样钉住「按住不放不会留下一个永远在录的线程」：到上限由采集线程自己收尾。
这条只在"用户手指滑出按钮 / up 事件丢失"时才起作用，人工测几乎测不到。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import mic_capture  # noqa: E402

_CHUNK = mic_capture._CHUNK


def _dev(index: int, name: str, rate: int = 48000, ch: int = 1) -> dict:
    return {
        "index": index,
        "name": name,
        "hostApi": 0,
        "maxInputChannels": ch,
        "defaultSampleRate": rate,
        "isLoopbackDevice": False,
    }


_REAL_MIC = _dev(0, "麦克风 (Realtek(R) Audio)")
_CABLE_IN = _dev(1, "CABLE Output (VB-Audio Virtual Cable)")
_LOOPBACK = {**_dev(2, "扬声器 (Realtek(R) Audio)"), "isLoopbackDevice": True}


class _FakeStream:
    """假输入流。

    `sleep_s` 不是装饰：真麦克风每次 read 都要等一个块（~10ms），而假流瞬间就能
    读完 —— 不睡一下，一个"上限 30 秒"的录制会在微秒内跑完，于是
    “还在录中/手动停止”这些用例全变成跟线程抢时序的脆测试。
    """

    def __init__(self, fill: bytes = b"\x01\x02", sleep_s: float = 0.002) -> None:
        self.reads = 0
        self._fill = fill
        self._sleep_s = sleep_s

    def read(self, n: int, exception_on_overflow: bool = False) -> bytes:
        self.reads += 1
        if self._sleep_s:
            time.sleep(self._sleep_s)
        return self._fill * n

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakePa:
    """只实现 mic_capture 用到的那几个方法。"""

    def __init__(self, devices: list[dict], default_in: int | None = 0, sleep_s: float = 0.002) -> None:
        self._devices = devices
        self._default_in = default_in
        self.stream = _FakeStream(sleep_s=sleep_s)
        self.terminated = False

    def get_host_api_info_by_type(self, _t):
        if not self._devices:
            raise OSError("WASAPI 不在")
        return {"index": 0, "defaultInputDevice": self._default_in}

    def get_device_info_generator(self):
        return iter(list(self._devices))

    def open(self, **_kw):
        return self.stream

    def terminate(self):
        self.terminated = True


@pytest.fixture()
def fake_pa(monkeypatch):
    """把 mic_capture 里的 pyaudio.PyAudio 换成可控的假实现。"""

    def _install(devices, default_in=0, sleep_s=0.002):
        pa = _FakePa(devices, default_in, sleep_s)
        monkeypatch.setattr(mic_capture.pyaudio, "PyAudio", lambda *a, **k: pa)
        return pa

    return _install


# ------------------------------------------------------------ 虚拟声卡的识别

@pytest.mark.parametrize(
    "name,expect",
    [
        ("CABLE Output (VB-Audio Virtual Cable)", True),
        ("CABLE Input (VB-Audio Virtual C", True),
        ("VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)", True),
        ("立体声混音 (Realtek(R) Audio)", True),
        ("Stereo Mix (Realtek)", True),
        ("麦克风 (Realtek(R) Audio)", False),
        ("Headset Microphone (USB Audio Device)", False),
        ("", False),
    ],
)
def test_is_virtual_device(name, expect):
    assert mic_capture.is_virtual_device(name) is expect


# ------------------------------------------------------------ 设备挑选

def test_picks_real_mic_even_when_cable_is_default():
    """★ 核心用例：实时变声把默认录音设备切成 CABLE 之后，仍要挑到真麦克风。

    这正是"双重变声"那个坑的来源 —— 若这里跟着默认走，录到的是**已经变好的声音**。
    """
    p = _FakePa([_REAL_MIC, _CABLE_IN], default_in=1)
    assert mic_capture.pick_mic_device(p)["name"] == _REAL_MIC["name"]


def test_prefers_default_when_default_is_real():
    p = _FakePa([_CABLE_IN, _REAL_MIC], default_in=0)
    assert mic_capture.pick_mic_device(p)["name"] == _REAL_MIC["name"]


def test_ignores_loopback_pseudo_devices():
    """loopback 伪设备（扬声器的内录端点）根本没有真实输入能力，不能当麦克风。"""
    assert mic_capture.input_devices(_FakePa([_LOOPBACK, _REAL_MIC])) == [_REAL_MIC]


def test_only_virtual_devices_raises_instead_of_settling():
    """只有虚拟设备时宁可报错：将就录 CABLE 会让用户拿到一个音色不对的结果。"""
    with pytest.raises(mic_capture.MicCaptureError) as e:
        mic_capture.pick_mic_device(_FakePa([_CABLE_IN], default_in=0))
    assert "虚拟声卡" in str(e.value)


def test_no_input_devices_raises():
    with pytest.raises(mic_capture.MicCaptureError):
        mic_capture.pick_mic_device(_FakePa([], default_in=None))


# ------------------------------------------------------------ 落盘

def test_finalize_writes_valid_wav(tmp_path):
    path = mic_capture._finalize([b"\x00\x01" * _CHUNK], 48000, 1, tmp_path / "a.wav")
    assert path.exists()
    assert mic_capture._wav_duration(path) == pytest.approx(_CHUNK / 48000, rel=1e-3)


def test_finalize_with_no_frames_is_a_zero_length_file(tmp_path):
    """一帧都没有也要落盘（0 秒），由端点判"太短"并给出可读的提示。"""
    path = mic_capture._finalize([], 48000, 1, tmp_path / "b.wav")
    assert path.exists()
    assert mic_capture._wav_duration(path) == 0.0


def test_wav_duration_of_garbage_is_zero(tmp_path):
    bad = tmp_path / "not_a_wav.wav"
    bad.write_bytes(b"nope")
    assert mic_capture._wav_duration(bad) == 0.0


# ------------------------------------------------------------ 录制会话

def test_recorder_auto_stops_at_limit(fake_pa, tmp_path):
    """★ 按住不放 / up 事件丢失：采集线程必须自己收尾，不能留一个永远在录的线程。"""
    fake_pa([_REAL_MIC], default_in=0)
    rec = mic_capture.MicRecorder()
    out = tmp_path / "hold.wav"
    info = rec.start(out, max_seconds=0.05)
    assert info["device"] == _REAL_MIC["name"]
    rec._thread.join(timeout=5)
    assert not rec.active, "跑满上限后采集线程应已收尾"
    assert rec.auto_stopped is True
    assert rec.error == ""
    assert mic_capture._wav_duration(out) == pytest.approx(0.05, abs=0.02)


def test_recorder_stop_returns_same_product_after_auto_stop(fake_pa, tmp_path):
    """已自动收尾时 stop() 不能报错 —— 用户松手晚于上限是常态。"""
    fake_pa([_REAL_MIC], default_in=0)
    rec = mic_capture.MicRecorder()
    out = tmp_path / "hold2.wav"
    rec.start(out, max_seconds=0.05)
    rec._thread.join(timeout=5)
    path, dur = rec.stop(timeout=2)
    assert path == out and dur > 0


def test_recorder_stop_manual_keeps_early_frames(fake_pa, tmp_path):
    """手动 stop：应在**没跑满上限**时就收尾，且已采到的帧不丢。"""
    pa = fake_pa([_REAL_MIC], default_in=0)
    rec = mic_capture.MicRecorder()
    out = tmp_path / "manual.wav"
    rec.start(out, max_seconds=30.0)
    # 等它真的采到几块再收（上限是 30s，假流那块不会自己跑完）
    for _ in range(200):
        if pa.stream.reads >= 5:
            break
        time.sleep(0.005)
    assert pa.stream.reads >= 5, "假流没采到帧，手动停止的用例失去意义"
    reads_at_stop = pa.stream.reads
    path, dur = rec.stop(timeout=5)
    assert path == out
    assert rec.auto_stopped is False
    assert 0 < dur < 5.0
    assert pa.stream.reads <= reads_at_stop + 2, "stop 后不该再大量采集"


def test_stop_without_start_raises():
    rec = mic_capture.MicRecorder()
    with pytest.raises(mic_capture.MicCaptureError):
        rec.stop()


def test_second_start_while_active_raises(fake_pa, tmp_path):
    fake_pa([_REAL_MIC], default_in=0)
    rec = mic_capture.MicRecorder()
    rec.start(tmp_path / "one.wav", max_seconds=30.0)
    try:
        with pytest.raises(mic_capture.MicCaptureError):
            rec.start(tmp_path / "two.wav", max_seconds=30.0)
    finally:
        rec.stop(timeout=5)


def test_module_singleton_lifecycle(fake_pa, tmp_path):
    """模块级 start/stop：一次只允许一个"按住"，结束后单例必须清空。"""
    fake_pa([_REAL_MIC], default_in=0)
    mic_capture.start(tmp_path / "s.wav", max_seconds=30.0)
    try:
        assert mic_capture.current() is not None
        with pytest.raises(mic_capture.MicCaptureError):
            mic_capture.start(tmp_path / "s2.wav", max_seconds=30.0)
    finally:
        path, dur, auto = mic_capture.stop(timeout=5)
    assert path.name == "s.wav" and dur > 0
    assert mic_capture.current() is None


def test_module_stop_without_recording_raises():
    with pytest.raises(mic_capture.MicCaptureError):
        mic_capture.stop()


def test_max_seconds_is_clamped_to_hard_ceiling(fake_pa, tmp_path):
    """外部传 600 秒也只能录到 MAX_HOLD_SECONDS —— 微信单条语音就 60 秒。"""
    fake_pa([_REAL_MIC], default_in=0)
    rec = mic_capture.MicRecorder()
    try:
        info = rec.start(tmp_path / "cap.wav", max_seconds=600)
        assert info["max_seconds"] == mic_capture.MAX_HOLD_SECONDS
    finally:
        rec.stop(timeout=5)
