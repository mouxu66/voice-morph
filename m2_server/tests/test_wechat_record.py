"""微信语音发送 · 输入模拟改进的纯函数测试（mic/alt 双路径，不碰真实键鼠）。"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wechat_voice as wv


@pytest.fixture(autouse=True)
def _no_real_uia(monkeypatch):
    """单测禁止触碰真实微信：UIA 一律先视为不可用（个别用例自己覆盖成可用）。

    不加这道锁的话，`_trigger_record` 会真的通过 UIA 点击微信语音按钮、
    真起一个录音浮层（挂到 60s 自动截断），污染真机会话。
    """
    monkeypatch.setattr(wv, "_uia_ready", lambda: False)


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
    """mic 路径：SendInput 单击语音按钮 → _finish_record 点绿钮 ↑ 发送。"""
    kb_calls, mouse_calls, moves = [], [], []
    monkeypatch.setattr(wv, "RECORD_METHOD", "mic")
    monkeypatch.setattr(wv, "_send_input_kb", lambda *a: kb_calls.append(a))
    monkeypatch.setattr(wv, "_postmsg_mouse", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_mouse_left", lambda down: mouse_calls.append(down))
    monkeypatch.setattr(wv, "_mouse_move_abs", lambda x, y: moves.append((x, y)))
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: 999)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (0, 0, 1600, 900))
    monkeypatch.setattr(wv, "_find_mic_icon", lambda rect: None)  # 强制走 _mic_point
    monkeypatch.setattr(wv, "_find_render_hwnd", lambda hwnd: 888)
    monkeypatch.setattr(wv, "_exstyle_clear_transparent", lambda hwnd: 0x90120)
    monkeypatch.setattr(wv, "_exstyle_restore_if_needed", lambda: None)
    monkeypatch.setattr(wv, "_wait_record_overlay", lambda rect, timeout=6.0: True)
    wv._trigger_record()
    assert wv._record_via == "realclick"
    assert wv._exstyle_restore == (888, 0x90120)
    assert kb_calls == []
    assert mouse_calls == [True, False]   # 单击 = DOWN + UP
    assert moves == [(1600 + wv.MIC_OFFSET_X, 900 + wv.MIC_OFFSET_Y)]
    monkeypatch.setattr(wv, "_find_green_send", lambda rect, retries=4: (1800, 1530))
    monkeypatch.setattr(wv, "_cancel_point", lambda rect: None)
    assert wv._finish_record() is True
    assert moves[-1:] == [(1800, 1530)]   # 先移到绿钮
    assert mouse_calls == [True, False, True, False]   # 再点绿钮发送


def test_trigger_mic_realclick_releases_on_overlay_timeout(monkeypatch):
    """SendInput 单击语音按钮后浮层始终不出现 → 抛错走降级，避免挂起录音。"""
    mouse_calls = []
    monkeypatch.setattr(wv, "RECORD_METHOD", "mic")
    monkeypatch.setattr(wv, "_postmsg_mouse", lambda *a, **k: None)  # 已废弃
    monkeypatch.setattr(wv, "_mouse_left", lambda down: mouse_calls.append(down))
    monkeypatch.setattr(wv, "_mouse_move_abs", lambda x, y: None)
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: 1)
    monkeypatch.setattr(wv, "_ensure_onscreen", lambda hwnd: None)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (0, 0, 1600, 900))
    monkeypatch.setattr(wv, "_find_mic_icon", lambda rect: (1443, 837))
    monkeypatch.setattr(wv, "_wait_record_overlay", lambda rect, timeout=6.0: False)
    monkeypatch.setattr(wv, "_find_render_hwnd", lambda hwnd: 555)
    monkeypatch.setattr(wv, "_exstyle_clear_transparent", lambda hwnd: 0x20)
    monkeypatch.setattr(wv, "_exstyle_restore_if_needed", lambda: None)
    with pytest.raises(RuntimeError, match="录音未能启动"):
        wv._trigger_record()
    assert mouse_calls == [True, False, False]   # 单击 + 超时兜底再松开
    assert wv._record_via is None


def test_trigger_and_finish_mic_drag_to_green(monkeypatch):
    """mic 路径：单击语音按钮起浮层 → _finish_record 点绿钮 ↑ 发送。"""
    mouse_calls, moves = [], []
    monkeypatch.setattr(wv, "RECORD_METHOD", "mic")
    monkeypatch.setattr(wv, "_postmsg_mouse", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_mouse_left", lambda down: mouse_calls.append(down))
    monkeypatch.setattr(wv, "_mouse_move_abs", lambda x, y: moves.append((x, y)))
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: 1)
    monkeypatch.setattr(wv, "_ensure_onscreen", lambda hwnd: None)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (0, 0, 1600, 900))
    monkeypatch.setattr(wv, "_find_mic_icon", lambda rect: (1443, 837))
    monkeypatch.setattr(wv, "_wait_record_overlay", lambda rect, timeout=6.0: True)
    monkeypatch.setattr(wv, "_find_render_hwnd", lambda hwnd: 555)
    monkeypatch.setattr(wv, "_exstyle_clear_transparent", lambda hwnd: 0x20)
    monkeypatch.setattr(wv, "_exstyle_restore_if_needed", lambda: None)

    wv._trigger_record()
    assert wv._record_via == "realclick"
    assert mouse_calls == [True, False]   # 单击 = DOWN + UP
    assert moves == [(1443, 837)]

    monkeypatch.setattr(wv, "_find_green_send", lambda rect, retries=4: (1800, 1530))
    monkeypatch.setattr(wv, "_cancel_point", lambda rect: None)  # 确保不发取消
    assert wv._finish_record() is True
    assert moves[-1:] == [(1800, 1530)]   # 先移到绿钮
    assert mouse_calls == [True, False, True, False]   # 然后点绿钮发送


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

def _fake_user32_screen(monkeypatch, size=(1938, 1609)):
    """最小 user32 桩：只回答屏幕尺寸（GetSystemMetrics(0)=宽, (1)=高）。

    `_find_green_send` 会把取域 clamp 到屏幕内（左边 = min(r, sw) - 560），
    所以屏幕尺寸会直接进结果。2026-09-13 CI 事故：下面的用例原来没桩它，
    断言里 `1378 + 480` 其实编码的是**开发机**屏幕（≥1938×1609）；CI runner 是
    1024×768 → box[0] 变成 464 → 拿到 953，红。桩掉之后与机器无关。
    """

    class FakeUser32:
        def GetSystemMetrics(self, index):
            return size[0] if index == 0 else size[1]

    fake = FakeUser32()
    monkeypatch.setattr(wv, "_user32", lambda: fake)
    return fake


def test_find_green_send_hit(monkeypatch):
    """浮层有微信绿 (18,199,125) 按钮时返回其质心屏幕坐标。"""
    from PIL import Image
    _fake_user32_screen(monkeypatch)                 # 屏幕尺寸必须固定，否则与机器绑定
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
    _fake_user32_screen(monkeypatch)
    scene = np.full((140, 560, 3), 247, dtype=np.uint8)
    monkeypatch.setattr("PIL.ImageGrab.grab", lambda **k: Image.fromarray(scene))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    assert wv._find_green_send((0, 0, 1938, 1609), retries=1) is None


def test_cancel_point_offset():
    cx, cy = wv._cancel_point((0, 0, 1938, 1609))
    assert (cx, cy) == (1938 - 157 - 218, 1609 - 63)


# ---------------- _ensure_onscreen 边界（2026-09-09 真机踩坑回归） ----------------

def _fake_user32_for_onscreen(monkeypatch, workarea, moves):
    """构造最小 user32 桩：记录 SetWindowPos 调用。"""

    class FakeUser32:
        def __init__(self):
            self.monitor = workarea
            self.moves = moves

        def MonitorFromWindow(self, hwnd, flag):
            return 1

        def GetMonitorInfoW(self, hmon, mi):
            import ctypes
            # mi 是 byref 包装，必须 cast 回 MONITORINFO 才能取字段
            info = ctypes.cast(mi, ctypes.POINTER(type(mi._obj))).contents
            l, t, r, b = self.monitor
            info.rcWork.left, info.rcWork.top = l, t
            info.rcWork.right, info.rcWork.bottom = r, b
            return 1

        def SetWindowPos(self, hwnd, _a, x, y, _w, _h, _f):
            self.moves.append((x, y))
            return 1

    fake = FakeUser32()
    monkeypatch.setattr(wv, "_user32", lambda: fake)
    return fake


def test_ensure_onscreen_pulls_bottom_in(monkeypatch):
    """底边超出工作区 → 上移，底边贴合工作区。"""
    moves: list = []
    _fake_user32_for_onscreen(monkeypatch, (0, 0, 2560, 1600), moves)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (639, 0, 1938, 1609))
    wv._ensure_onscreen(1)
    assert moves == [(639, -9)]          # 上移 9px，底边回到 1600


def test_ensure_onscreen_does_not_push_down_maximized(monkeypatch):
    """回归：最大化窗口 top 为负但底边正常时，不许为了拉回顶边把底边推出屏幕。

    实测事故：窗口 (639,-9,1938,1600) 被旧逻辑「修正」成 (639,0,1938,1609)，
    底边反而超出屏幕 9px，话筒区域被裁导致模板匹配失败（score -0.07）。
    """
    moves: list = []
    _fake_user32_for_onscreen(monkeypatch, (0, 0, 2560, 1600), moves)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (639, -9, 1938, 1600))
    wv._ensure_onscreen(1)
    assert moves == []                   # 一动不动


def test_ensure_onscreen_rescues_fully_offscreen_top(monkeypatch):
    """窗口整体在工作区上方之外 → 拉回。"""
    moves: list = []
    _fake_user32_for_onscreen(monkeypatch, (0, 0, 2560, 1600), moves)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (639, -1700, 1938, -100))
    wv._ensure_onscreen(1)
    assert moves == [(639, 0)]           # 顶边拉回工作区顶部


# ---------------- 录音浮层：绿钮判据（2026-09-09 真机验证） ----------------

def test_wait_record_overlay_true_when_green_present(monkeypatch):
    """绿钮出现 → 判定录音浮层已起。"""
    monkeypatch.setattr(wv, "_find_green_send", lambda rect, retries=1: (1844, 1531))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    assert wv._wait_record_overlay((0, 0, 1938, 1600), timeout=1.0) is True


def test_wait_record_overlay_false_when_green_absent(monkeypatch):
    """没有绿钮 → 判定浮层未起。"""
    monkeypatch.setattr(wv, "_find_green_send", lambda rect, retries=1: None)
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    assert wv._wait_record_overlay((0, 0, 1938, 1600), timeout=1.0) is False


def test_overlay_falls_back_to_green_when_no_baseline(monkeypatch):
    """截图不可用（基线为 None）→ 退化为绿钮判据，宁可放过不可漏掉。"""
    wv._overlay_baseline = None
    monkeypatch.setattr(wv, "_find_green_send", lambda rect, retries=1: (1863, 1436))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    assert wv._wait_record_overlay((0, 0, 1938, 1600), timeout=1.0) is True
    wv._overlay_baseline = None          # 还原，避免污染其他用例


# ================= 方案 A：UIA 优先 + 像素降级（2026-09-10） =================
#
# 真机实测依据：微信 4.1.13.12 热激活后可读到
#   mmui::XButton '发语音 ( 按住右 Alt )'  [1113,1525,1155,1567]
#   mmui::ChatVoiceRecordView              [891,1513,1242,1573]
#     ├─ mmui::XButton '取消'              [891,1519,933,1561]
#     └─ XMouseEventView '发送语音'        [1194,1519,1236,1561]
#   mmui::ChatVoiceItemView '语音15"秒'
# 以下用 fake 覆盖「UIA 命中 / UIA 失败回退像素」两条分支。

class FakeUia:
    """最小 UIA 假实现：只提供 wechat_voice 用到的接口。"""

    def __init__(self, ready=True, overlay=False, send_box=None, cancel_box=None,
                 voice_click=True, msg=None, boom=False, msgs=None):
        self.ready = ready
        self.overlay = overlay
        self.send_box = send_box
        self.cancel_box = cancel_box
        self.voice_click = voice_click
        self.msg = msg
        self.boom = boom
        self.msgs = msgs

    def _guard(self):
        if self.boom:
            raise RuntimeError("uia boom")

    def uia_ready(self):
        return self.ready

    def overlay_exists(self):
        self._guard()
        return self.overlay

    def overlay_rect(self):
        return (891, 1513, 1242, 1573) if self.overlay else None

    def click_voice_button(self):
        self._guard()
        return self.voice_click

    def send_button_rect(self):
        self._guard()
        return self.send_box

    def cancel_button_rect(self):
        self._guard()
        return self.cancel_box

    def latest_voice_message(self):
        self._guard()
        return self.msg

    def voice_messages(self):
        self._guard()
        if self.msgs is not None:
            return list(self.msgs)
        return [self.msg] if self.msg else []

    @staticmethod
    def duration_from_message(name):
        import wechat_uia as real
        return real.duration_from_message(name)


def test_uia_overlay_detected_without_pixel_probe(monkeypatch):
    """UIA 判定浮层成立时，不该再走像素截图。"""
    monkeypatch.setattr(wv, "_uia", FakeUia(overlay=True))
    monkeypatch.setattr(wv, "_uia_ready", lambda: True)
    called = []
    monkeypatch.setattr(wv, "_find_green_send",
                        lambda rect, retries=1: called.append(1) or None)
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    assert wv._wait_record_overlay((0, 0, 1938, 1600), timeout=1.0) is True
    assert called == []                     # 完全没碰像素链路


def test_uia_overlay_absent_falls_back_to_green(monkeypatch):
    """UIA 说没浮层，但绿钮在 → 仍判定浮层已起（双保险）。"""
    monkeypatch.setattr(wv, "_uia", FakeUia(overlay=False))
    monkeypatch.setattr(wv, "_uia_ready", lambda: True)
    monkeypatch.setattr(wv, "_find_green_send", lambda rect, retries=1: (1844, 1531))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    assert wv._wait_record_overlay((0, 0, 1938, 1600), timeout=1.0) is True


def test_uia_exception_degrades_to_pixel(monkeypatch):
    """UIA 抛异常不能中断流程：本轮到像素判据，后续轮不再调 UIA。"""
    uia = FakeUia(boom=True)
    monkeypatch.setattr(wv, "_uia", uia)
    monkeypatch.setattr(wv, "_uia_ready", lambda: True)
    monkeypatch.setattr(wv, "_find_green_send", lambda rect, retries=1: (1844, 1531))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    assert wv._wait_record_overlay((0, 0, 1938, 1600), timeout=1.0) is True


def test_uia_ready_false_when_module_missing(monkeypatch):
    monkeypatch.setattr(wv, "_uia", None)
    assert wv._uia_ready() is False


def test_uia_ready_swallows_exceptions(monkeypatch):
    monkeypatch.setattr(wv, "_uia", FakeUia(boom=True))
    assert wv._uia_ready() is False         # uia_ready 内部抛错 → False，不冒泡


def test_trigger_prefers_uia_click(monkeypatch):
    """UIA 点击成功且浮层起来 → 走 uia 路径，不碰鼠标、不摘样式位。"""
    mouse_calls, moves = [], []
    monkeypatch.setattr(wv, "RECORD_METHOD", "mic")
    monkeypatch.setattr(wv, "_uia", FakeUia(overlay=True))
    monkeypatch.setattr(wv, "_uia_ready", lambda: True)
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: 999)
    monkeypatch.setattr(wv, "_ensure_onscreen", lambda hwnd: None)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (0, 0, 1600, 900))
    monkeypatch.setattr(wv, "_mouse_left", lambda d: mouse_calls.append(d))
    monkeypatch.setattr(wv, "_mouse_move_abs", lambda x, y: moves.append((x, y)))
    monkeypatch.setattr(wv, "_wait_record_overlay", lambda rect, timeout=6.0: True)
    wv._trigger_record()
    assert wv._record_via == "uia"
    assert mouse_calls == [] and moves == []


def test_trigger_falls_back_to_pixel_when_uia_click_fails(monkeypatch):
    """UIA 点击不生效 → 回退「摘样式 + SendInput 单击」，行为与旧版一致。"""
    mouse_calls, moves = [], []
    monkeypatch.setattr(wv, "RECORD_METHOD", "mic")
    monkeypatch.setattr(wv, "_uia", FakeUia(voice_click=False))
    monkeypatch.setattr(wv, "_uia_ready", lambda: True)
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: 999)
    monkeypatch.setattr(wv, "_ensure_onscreen", lambda hwnd: None)
    monkeypatch.setattr(wv, "_window_rect", lambda hwnd: (0, 0, 1600, 900))
    monkeypatch.setattr(wv, "_find_mic_icon", lambda rect: None)
    monkeypatch.setattr(wv, "_find_render_hwnd", lambda hwnd: 888)
    monkeypatch.setattr(wv, "_exstyle_clear_transparent", lambda hwnd: 0x90120)
    monkeypatch.setattr(wv, "_exstyle_restore_if_needed", lambda: None)
    monkeypatch.setattr(wv, "_mouse_left", lambda d: mouse_calls.append(d))
    monkeypatch.setattr(wv, "_mouse_move_abs", lambda x, y: moves.append((x, y)))
    monkeypatch.setattr(wv, "_wait_record_overlay", lambda rect, timeout=6.0: True)
    wv._trigger_record()
    assert wv._record_via == "realclick"
    assert mouse_calls == [True, False]     # 单击（DOWN+UP）
    assert moves == [(1600 + wv.MIC_OFFSET_X, 900 + wv.MIC_OFFSET_Y)]


def test_finish_prefers_uia_send_button(monkeypatch):
    """发送钮用 UIA 矩形中心（1194,1519,1236,1561 → 1215,1540）。"""
    mouse_calls, moves = [], []
    monkeypatch.setattr(wv, "RECORD_METHOD", "mic")
    monkeypatch.setattr(wv, "_uia", FakeUia(send_box=(1194, 1519, 1236, 1561)))
    monkeypatch.setattr(wv, "_uia_ready", lambda: True)
    monkeypatch.setattr(wv, "_rect_ctx", (0, 0, 1600, 900))
    monkeypatch.setattr(wv, "_find_green_send", lambda rect, retries=4: (9999, 9999))
    monkeypatch.setattr(wv, "_mouse_left", lambda d: mouse_calls.append(d))
    monkeypatch.setattr(wv, "_mouse_move_abs", lambda x, y: moves.append((x, y)))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    assert wv._finish_record() is True
    assert moves[-1] == (1215, 1540)        # 用的 UIA 中心，不是绿钮 (9999,9999)
    assert mouse_calls == [True, False]


def test_finish_falls_back_to_green_when_uia_missing(monkeypatch):
    mouse_calls, moves = [], []
    monkeypatch.setattr(wv, "RECORD_METHOD", "mic")
    monkeypatch.setattr(wv, "_uia", FakeUia(send_box=None))
    monkeypatch.setattr(wv, "_uia_ready", lambda: True)
    monkeypatch.setattr(wv, "_rect_ctx", (0, 0, 1600, 900))
    monkeypatch.setattr(wv, "_find_green_send", lambda rect, retries=4: (1844, 1531))
    monkeypatch.setattr(wv, "_mouse_left", lambda d: mouse_calls.append(d))
    monkeypatch.setattr(wv, "_mouse_move_abs", lambda x, y: moves.append((x, y)))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    assert wv._finish_record() is True
    assert moves[-1] == (1844, 1531)
    assert mouse_calls == [True, False]


def test_cancel_point_prefers_uia(monkeypatch):
    """取消钮用 UIA 实测矩形 (891,1519,933,1561) → 中心 (912,1540)。"""
    monkeypatch.setattr(wv, "_uia", FakeUia(cancel_box=(891, 1519, 933, 1561)))
    monkeypatch.setattr(wv, "_uia_ready", lambda: True)
    assert wv._cancel_point((0, 0, 1600, 900)) == (912, 1540)


def test_cancel_point_falls_back_to_offset(monkeypatch):
    """UIA 不可用 → 退回老偏移（话筒左侧 218px）。"""
    monkeypatch.setattr(wv, "_uia", FakeUia(cancel_box=None))
    monkeypatch.setattr(wv, "_uia_ready", lambda: True)
    mx, my = wv._mic_point((0, 0, 1600, 900))
    assert wv._cancel_point((0, 0, 1600, 900)) == (mx - 218, my)


# ---------------- 发送结果校验（替代"截图看气泡"） ----------------

def test_uia_verify_sent_ok(monkeypatch):
    monkeypatch.setattr(wv, "_uia", FakeUia(msg='语音10"秒'))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    out = wv._uia_verify_sent('语音6"秒', 10.3)
    assert any("语音10" in s for s in out)
    assert not any("⚠" in s for s in out)


def test_uia_verify_sent_flags_unchanged(monkeypatch):
    """最新语音没变 → 说明这次可能没发出去（以前只有截图肉眼看才发现）。"""
    monkeypatch.setattr(wv, "_uia", FakeUia(msg='语音6"秒', msgs=['语音6"秒']))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    out = wv._uia_verify_sent('语音6"秒', 10.3, before_count=1)
    assert any("可能没发出去" in s for s in out)


def test_uia_verify_sent_same_text_but_count_grew(monkeypatch):
    """真机踩过的坑：连续两条时长相同的语音，UIA 文本完全一样（都是 `语音15"秒`），
    纯字符串对比会把"发送成功"误报成"没发出去"。必须用条数兜底。"""
    monkeypatch.setattr(wv, "_uia", FakeUia(msg='语音15"秒',
                                            msgs=['语音27"秒', '语音15"秒', '语音15"秒']))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    out = wv._uia_verify_sent('语音15"秒', 10.8, before_count=2)
    assert not any("可能没发出去" in s for s in out)
    assert any("语音15" in s for s in out)


def test_uia_verify_sent_flags_60s_truncation(monkeypatch):
    """时长被 60s 截断要能自动预警。"""
    monkeypatch.setattr(wv, "_uia", FakeUia(msg='语音60"秒'))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    out = wv._uia_verify_sent('语音6"秒', 10.3)
    assert any("60s 截断" in s for s in out)


def test_uia_verify_sent_read_failure(monkeypatch):
    monkeypatch.setattr(wv, "_uia", FakeUia(msg=None))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    out = wv._uia_verify_sent(None, 10.3)
    assert any("读不到" in s for s in out)


def test_uia_verify_sent_short_audio_not_flagged(monkeypatch):
    """短音频（<3s）不做时长预警——微信 1s 下限附近本就可能偏长。"""
    monkeypatch.setattr(wv, "_uia", FakeUia(msg='语音5"秒'))
    monkeypatch.setattr(wv.time, "sleep", lambda s: None)
    out = wv._uia_verify_sent(None, 2.0)
    assert not any("⚠" in s for s in out)
