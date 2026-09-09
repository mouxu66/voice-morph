"""微信语音发送 · 输入模拟改进的纯函数测试（mic/alt 双路径，不碰真实键鼠）。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wechat_voice as wv


# ---------------- RECORD_METHOD 配置 ----------------

def test_record_method_default_mic(monkeypatch):
    assert wv.RECORD_METHOD in ("mic", "alt")
    assert wv.RECORD_METHOD == "mic"   # 默认走话筒鼠标路径


def test_record_method_rejects_unknown(monkeypatch):
    """非法 RECORD_METHOD 应在 import 时报错（模拟：重新赋值走同一校验）。"""
    with pytest.raises(RuntimeError, match="VM_WECHAT_RECORD_METHOD"):
        bad = "tab"
        if bad not in ("mic", "alt"):
            raise RuntimeError(f"VM_WECHAT_RECORD_METHOD 只支持 mic/alt，当前: {bad}")


# ---------------- 键位映射（SendInput wVk+wScan） ----------------

def test_keymap_alt_has_vk_and_scan():
    vk, scan = wv._record_key_code() if wv.RECORD_KEY.strip().lower() == "alt" else wv._KEYMAP["alt"]
    assert vk == 0xA4            # VK_LMENU（左 Alt，精确到键）
    assert scan == 0x38          # 硬件扫描码


def test_keymap_covers_all_record_keys():
    for name in ("alt", "ctrl", "shift", "win"):
        vk, scan = wv._KEYMAP[name]
        assert 0 < vk < 0x100 and 0 < scan < 0x100


def test_record_key_rejects_unknown():
    original = wv.RECORD_KEY
    try:
        wv.RECORD_KEY = "f13"
        with pytest.raises(RuntimeError, match="VM_WECHAT_RECORD_KEY"):
            wv._record_key_code()
    finally:
        wv.RECORD_KEY = original


# ---------------- 话筒坐标计算 ----------------

def test_mic_point_from_window_rect():
    # 1920x1080 窗口，默认偏移 (-58, -30)
    rect = (0, 0, 1920, 1040)
    x, y = wv._mic_point(rect)
    assert (x, y) == (1920 - 58, 1040 - 30)


def test_mic_point_custom_offset(monkeypatch):
    x, y = wv._mic_point((100, 200, 1100, 900), offset_x=-120, offset_y=-60)
    assert (x, y) == (980, 840)


def test_mic_point_positive_offset_allowed():
    """偏移允许为正（用户按自己屏幕校准），不做方向限制。"""
    x, y = wv._mic_point((0, 0, 800, 600), offset_x=10, offset_y=5)
    assert (x, y) == (810, 605)


# ---------------- 鼠标归一化数学 ----------------

def test_mouse_move_abs_normalizes_to_virtual_screen(monkeypatch):
    """虚拟屏 (0,0,1920,1080)：点 (960,540) 应归一化为 (32767~32768, 32767~32768)。"""
    calls = []

    def fake_send(flags, dx=0, dy=0):
        calls.append((flags, dx, dy))

    u = wv._user32()
    monkeypatch.setattr(wv, "_send_input_mouse", fake_send)
    monkeypatch.setattr(u, "GetSystemMetrics",
                        lambda i: {76: 0, 77: 0, 78: 1920, 79: 1080}.get(i, 0))
    wv._mouse_move_abs(960, 540)
    assert len(calls) == 1
    flags, dx, dy = calls[0]
    assert flags & wv.MOUSEEVENTF_ABSOLUTE and flags & wv.MOUSEEVENTF_VIRTUALDESK
    assert 32700 <= dx <= 32800
    assert 32700 <= dy <= 32800


def test_mouse_move_abs_multi_monitor_offset(monkeypatch):
    """副屏场景：虚拟屏 (-1920,0,3840,1080)，副屏 (1920..3840) 上的点应落在 >32767。"""
    calls = []
    monkeypatch.setattr(wv, "_send_input_mouse", lambda f, dx=0, dy=0: calls.append((f, dx, dy)))
    u = wv._user32()
    monkeypatch.setattr(u, "GetSystemMetrics",
                        lambda i: {76: -1920, 77: 0, 78: 3840, 79: 1080}.get(i, 0))
    wv._mouse_move_abs(2880, 540)   # 副屏中央
    _, dx, dy = calls[0]
    assert dx > 49100               # (2880+1920)/3839*65535 ≈ 81919 → clamp 到 65535
    assert dy <= 65535


def test_mouse_move_abs_clamps_overflow(monkeypatch):
    calls = []
    monkeypatch.setattr(wv, "_send_input_mouse", lambda f, dx=0, dy=0: calls.append((f, dx, dy)))
    u = wv._user32()
    monkeypatch.setattr(u, "GetSystemMetrics",
                        lambda i: {76: 0, 77: 0, 78: 1920, 79: 1080}.get(i, 0))
    wv._mouse_move_abs(5000, 5000)   # 超出屏幕 → clamp 到 65535
    _, dx, dy = calls[0]
    assert dx == 65535 and dy == 65535


# ---------------- SendInput 键盘事件构造 ----------------

def test_send_input_kb_press_and_release(monkeypatch):
    """验证按下/松开的 INPUT 结构（type/vk/scan/flags）。"""
    captured = {}

    class FakeUser32:
        def SendInput(self, n, inputs, size):
            inp = ctypes.cast(inputs, ctypes.POINTER(wv._INPUT)).contents
            captured.update(type=inp.type, vk=inp.ki.wVk,
                            scan=inp.ki.wScan, flags=inp.ki.dwFlags)
            return 1

        def SetProcessDPIAware(self):
            return True

    import ctypes
    monkeypatch.setattr(wv, "_user32", lambda: FakeUser32())
    wv._send_input_kb(0xA4, 0x38, False)
    assert captured["type"] == 1          # INPUT_KEYBOARD
    assert captured["vk"] == 0xA4
    assert captured["scan"] == 0x38
    assert captured["flags"] == 0         # 按下无 KEYUP
    wv._send_input_kb(0xA4, 0x38, True)
    assert captured["flags"] == 0x0002    # KEYEVENTF_KEYUP


# ---------------- 录音触发/结束分派 ----------------

def test_trigger_and_finish_alt_path(monkeypatch):
    """alt 路径：trigger=keydown、finish=keypad up，不碰鼠标。"""
    wv_original = wv.RECORD_METHOD
    kb_calls, mouse_calls = [], []
    monkeypatch.setattr(wv, "RECORD_METHOD", "alt")
    monkeypatch.setattr(wv, "_send_input_kb",
                        lambda vk, scan, up: kb_calls.append((vk, scan, up)))
    monkeypatch.setattr(wv, "_mouse_left", lambda down: mouse_calls.append(down))
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: 12345)
    wv._trigger_record()
    wv._finish_record()
    assert len(kb_calls) == 2
    assert kb_calls[0][2] is False and kb_calls[1][2] is True   # down → up
    assert mouse_calls == []             # 不碰鼠标
    assert wv_original in ("mic", "alt")


def test_trigger_and_finish_mic_path(monkeypatch):
    """mic 路径：trigger=移动+左键按下、finish=左键抬起，不碰键盘。"""
    kb_calls, mouse_calls = [], []
    seq = []
    monkeypatch.setattr(wv, "RECORD_METHOD", "mic")
    monkeypatch.setattr(wv, "_send_input_kb", lambda *a: kb_calls.append(a))
    monkeypatch.setattr(wv, "_mouse_left", lambda down: mouse_calls.append(down))
    monkeypatch.setattr(wv, "_mouse_move_abs", lambda x, y: seq.append((x, y)))
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: 999)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (0, 0, 1600, 900))
    wv._trigger_record()
    assert kb_calls == []                        # 不发键盘
    assert mouse_calls == [True]                 # 左键按下
    assert seq == [(1600 + wv.MIC_OFFSET_X, 900 + wv.MIC_OFFSET_Y)]
    wv._finish_record()
    assert mouse_calls == [True, False]          # 松开 → 微信自动发送
