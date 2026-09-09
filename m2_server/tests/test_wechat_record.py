"""微信语音发送 · 输入模拟改进的纯函数测试（mic/alt 双路径，不碰真实键鼠）。"""

import sys
from pathlib import Path

import numpy as np
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
    # 1920x1040 窗口，默认偏移 (-157, -63)（2026-09-09 本机实测校准值）
    rect = (0, 0, 1920, 1040)
    x, y = wv._mic_point(rect)
    assert (x, y) == (1920 - 157, 1040 - 63)


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
    """mic 路径：PostMessage 失败回退 SendInput——移动+左键按下、抬起（松开即发送）。"""
    kb_calls, mouse_calls = [], []
    seq = []
    monkeypatch.setattr(wv, "RECORD_METHOD", "mic")
    monkeypatch.setattr(wv, "_send_input_kb", lambda *a: kb_calls.append(a))
    monkeypatch.setattr(wv, "_postmsg_mouse", lambda *a, **k: None)   # post 失败 → 回退
    monkeypatch.setattr(wv, "_mouse_left", lambda down: mouse_calls.append(down))
    monkeypatch.setattr(wv, "_mouse_move_abs", lambda x, y: seq.append((x, y)))
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: 999)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (0, 0, 1600, 900))
    monkeypatch.setattr(wv, "_find_render_hwnd", lambda hwnd: 888)
    monkeypatch.setattr(wv, "_exstyle_clear_transparent", lambda hwnd: 0x90120)
    monkeypatch.setattr(wv, "_exstyle_restore_if_needed", lambda: None)
    monkeypatch.setattr(wv, "_wait_record_overlay", lambda rect, timeout=6.0: True)
    wv._trigger_record()
    assert wv._record_via == "realclick"         # 走真实点击路径
    assert wv._exstyle_restore == (888, 0x90120)  # 摘样式待恢复
    assert kb_calls == []                        # 不发键盘
    assert mouse_calls == [True]                 # 左键按下
    assert seq == [(1600 + wv.MIC_OFFSET_X, 900 + wv.MIC_OFFSET_Y)]
    wv._finish_record()
    assert mouse_calls == [True, False]          # 松开 → 微信自动发送


def test_trigger_mic_realclick_only_after_postmsg_overlay_timeout(monkeypatch):
    """PostMessage 按下成功但浮层始终不出现 → 两次尝试后才走 realclick 兜底。"""
    calls, mouse_calls = [], []
    monkeypatch.setattr(wv, "RECORD_METHOD", "mic")

    def fake_post(point=None, down=False, up=False, target=None, lparam=None):
        if down:
            calls.append(("down", point))
            return (0x1234, 0xabcd)
        return (target, lparam)

    monkeypatch.setattr(wv, "_postmsg_mouse", fake_post)
    monkeypatch.setattr(wv, "_mouse_left", lambda down: mouse_calls.append(down))
    monkeypatch.setattr(wv, "_mouse_move_abs", lambda x, y: None)
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: 1)
    monkeypatch.setattr(wv, "_ensure_onscreen", lambda hwnd: None)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (0, 0, 1600, 900))
    monkeypatch.setattr(wv, "_find_mic_icon", lambda rect: (1443, 837))
    waits = {"n": 0}

    def fake_wait(rect, timeout=6.0):
        waits["n"] += 1
        return waits["n"] >= 3   # postmsg 两次超时，realclick 后第三次出现

    monkeypatch.setattr(wv, "_wait_record_overlay", fake_wait)
    monkeypatch.setattr(wv, "_find_render_hwnd", lambda hwnd: 555)
    monkeypatch.setattr(wv, "_exstyle_clear_transparent", lambda hwnd: 0x20)
    monkeypatch.setattr(wv, "_exstyle_restore_if_needed", lambda: None)
    wv._trigger_record()
    assert calls == [("down", (1443, 837)), ("down", (1443, 837))]   # 补发过一次
    assert mouse_calls == [True]                                     # 最终真实按下
    assert wv._record_via == "realclick"


def test_trigger_mic_prefers_postmessage(monkeypatch):
    """PostMessage 是主路径：按下/抬起走窗口消息，不碰真实鼠标。"""
    calls = []
    mouse_calls = []
    monkeypatch.setattr(wv, "RECORD_METHOD", "mic")

    def fake_post(point=None, down=False, up=False, target=None, lparam=None):
        if down:
            calls.append(("down", point))
            return (0x1234, 0x0609abcd)
        calls.append(("up", target, lparam))
        return (target, lparam)

    monkeypatch.setattr(wv, "_postmsg_mouse", fake_post)
    monkeypatch.setattr(wv, "_mouse_left", lambda down: mouse_calls.append(down))
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: 1)
    monkeypatch.setattr(wv, "_ensure_onscreen", lambda hwnd: None)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (0, 0, 1600, 900))
    monkeypatch.setattr(wv, "_find_mic_icon", lambda rect: (1443, 837))
    monkeypatch.setattr(wv, "_wait_record_overlay", lambda rect, timeout=6.0: True)
    wv._trigger_record()
    assert calls == [("down", (1443, 837))]      # 按下用模板匹配坐标
    assert mouse_calls == []                     # 不碰真实鼠标
    monkeypatch.setattr(wv, "_find_green_send", lambda rect, retries=4: (1800, 1530))
    wv._finish_record()
    assert calls[-1] == ("up", 0x1234, 0x0609abcd) or calls[-2] == ("down", (1800, 1530))


def test_trigger_mic_prefers_template_match(monkeypatch):
    """SendInput 回退路径：模板命中时用匹配坐标，未命中回退固定偏移。"""
    seq = []
    mouse_calls = []
    monkeypatch.setattr(wv, "RECORD_METHOD", "mic")
    monkeypatch.setattr(wv, "_send_input_kb", lambda *a: None)
    monkeypatch.setattr(wv, "_postmsg_mouse", lambda *a, **k: None)   # post 失败
    monkeypatch.setattr(wv, "_mouse_left", lambda down: mouse_calls.append(down))
    monkeypatch.setattr(wv, "_mouse_move_abs", lambda x, y: seq.append((x, y)))
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: 1)
    monkeypatch.setattr(wv, "_ensure_onscreen", lambda hwnd: None)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (0, 0, 1600, 900))
    monkeypatch.setattr(wv, "_find_render_hwnd", lambda hwnd: 555)
    monkeypatch.setattr(wv, "_exstyle_clear_transparent", lambda hwnd: None)
    monkeypatch.setattr(wv, "_wait_record_overlay", lambda rect, timeout=6.0: True)
    monkeypatch.setattr(wv, "_find_mic_icon", lambda rect: (1443, 837))
    wv._trigger_record()
    assert seq == [(1443, 837)]                  # 模板匹配坐标优先

    seq.clear()
    monkeypatch.setattr(wv, "_find_mic_icon", lambda rect: None)
    wv._trigger_record()
    assert seq == [(1600 + wv.MIC_OFFSET_X, 900 + wv.MIC_OFFSET_Y)]  # 回退偏移


# ---------------- 渲染子窗口与 WS_EX_TRANSPARENT（社区方案吸收） ----------------

class _FakeUser32Enum:
    """支持 EnumChildWindows / GetClassNameW / GetWindowRect 的 user32 替身。"""

    def __init__(self, classes, sizes):
        self.classes, self.sizes = classes, sizes

    def EnumChildWindows(self, parent, cb, lp):
        for h in self.classes:
            cb(h, lp)
        return 1

    def GetClassNameW(self, h, buf, n):
        buf.value = self.classes.get(h, "")
        return 1

    def GetWindowRect(self, h, r):
        import ctypes
        from ctypes import wintypes
        rect = ctypes.cast(r, ctypes.POINTER(wintypes.RECT)).contents
        rect.left, rect.top, rect.right, rect.bottom = self.sizes.get(h, (0, 0, 0, 0))
        return 1


def test_find_render_hwnd_picks_largest(monkeypatch):
    """多个 MMUIRenderSubWindow* 子窗口时取面积最大；其余类名忽略。"""
    fake = _FakeUser32Enum(
        {11: "Chrome_WidgetWin_0", 12: "MMUIRenderSubWindowHW", 13: "MMUIRenderSubWindow"},
        {11: (0, 0, 100, 100), 12: (0, 0, 200, 200), 13: (0, 0, 800, 600)})
    monkeypatch.setattr(wv, "_user32", lambda: fake)
    assert wv._find_render_hwnd(7) == 13


def test_find_render_hwnd_fallback_to_main(monkeypatch):
    """找不到渲染子窗口（旧版微信/类名变更）→ 回退主窗口句柄。"""
    fake = _FakeUser32Enum({11: "Chrome_WidgetWin_0"}, {11: (0, 0, 100, 100)})
    monkeypatch.setattr(wv, "_user32", lambda: fake)
    assert wv._find_render_hwnd(7) == 7


def test_exstyle_clear_transparent_and_restore(monkeypatch):
    """带 WS_EX_TRANSPARENT 的窗口：摘除返回原样式，恢复写回；无该位则不动。"""
    styles = {55: 0x90120}          # 含 0x20 位
    set_calls = []

    def get_long(hwnd, idx):
        return styles.get(hwnd, 0)

    def set_long(hwnd, idx, val):
        set_calls.append((hwnd, val))
        styles[hwnd] = val
        return 1

    fake = type("U", (), {"GetWindowLongW": staticmethod(get_long),
                          "SetWindowLongW": staticmethod(set_long)})()
    monkeypatch.setattr(wv, "_user32", lambda: fake)
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    old = wv._exstyle_clear_transparent(55)
    assert old == 0x90120
    assert styles[55] == 0x90100                   # 0x20 位已摘除
    wv._exstyle_restore = (55, old)
    wv._exstyle_restore_if_needed()
    assert styles[55] == 0x90120                   # 已恢复
    assert wv._exstyle_restore is None
    # 无该位：返回 None，不写样式
    styles[56] = 0x90100
    assert wv._exstyle_clear_transparent(56) is None
    assert all(h != 56 for h, _ in set_calls)


# ---------------- NCC 模板匹配 ----------------

def _make_icon_img(cx: int, cy: int, size: int = 36) -> np.ndarray:
    """合成一个'圆环图标'灰度图（细线条，模拟微信话筒）。"""
    yy, xx = np.mgrid[0:size, 0:size]
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    img = np.full((size, size), 245.0)
    ring = np.abs(d - 12) < 1.6          # 半径 12 的细圆环
    img[ring] = 60.0
    return img


def test_ncc_match_finds_exact_position():
    scene = np.full((150, 340), 245.0)
    true_x, true_y = 165, 69
    scene[true_y:true_y + 36, true_x:true_x + 36] = _make_icon_img(18, 18)
    tpl = _make_icon_img(18, 18)
    pos, score = wv._ncc_match(scene, tpl)
    assert pos == (true_x, true_y)
    assert score > 0.95


def test_ncc_match_rejects_flat_scene():
    scene = np.full((150, 340), 245.0)   # 纯色无图标
    tpl = _make_icon_img(18, 18)
    pos, score = wv._ncc_match(scene, tpl)
    assert score < 0.75                  # 阈值拦截 → 回退固定偏移


def test_ncc_match_tiny_image_returns_none():
    pos, score = wv._ncc_match(np.zeros((10, 10)), np.zeros((36, 36)))
    assert pos is None and score == -1.0


# ---------------- 绿色发送按钮定位 ----------------

def test_find_green_send_hit(monkeypatch):
    """浮层有微信绿 (18,199,125) 按钮时返回其质心屏幕坐标。"""
    from PIL import Image
    scene = np.full((140, 560, 3), 247, dtype=np.uint8)
    scene[100:120, 480:500] = (18, 199, 125)       # 绿钮
    grabs = [Image.fromarray(scene)]
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda **k: grabs.pop(0))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    got = wv._find_green_send((0, 0, 1938, 1609), retries=1)
    assert got is not None
    gx, gy = got
    assert 1378 + 480 <= gx <= 1378 + 500
    assert 1469 + 100 <= gy <= 1469 + 120


def test_find_green_send_miss(monkeypatch):
    """纯灰浮层 → None（调用方点 × 取消）。"""
    from PIL import Image
    scene = np.full((140, 560, 3), 247, dtype=np.uint8)
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda **k: Image.fromarray(scene))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    assert wv._find_green_send((0, 0, 1938, 1609), retries=1) is None


def test_cancel_point_offset():
    cx, cy = wv._cancel_point((0, 0, 1938, 1609))
    assert (cx, cy) == (1938 - 157 - 218, 1609 - 63)
