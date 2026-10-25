"""把聊天区里那张图「放大」读出来。

为什么要这一步：UIA 读到的图片消息只有「图片」两个字，而聊天区里的**缩略图只有
418×270**，字全糊成色块 —— 2026-10-25 实测，Qwen3-VL-4B 和 Ornith-9B 在这张
缩略图上都读不出任何活动名。唯一能拿到原图的路是：双击缩略图让微信弹出预览窗，
截下那个窗，再按 Esc 关掉。

实测（3864 群《万圣节狂欢活动》一周安排表）：
    缩略图 418×270        → 读不出（两个模型都一样）
    预览窗 2315×1550      → 能读出日期、活动名、数量门槛、总结栏

代价是**会抢焦点 1~2 秒并弹一下预览窗**。所以只在用户主动点「看图」时走这条路，
不做成自动的 —— 自动抢焦点会打断正在打的字。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "outputs" / "eval-3864" / "shots"

#: 双击后等预览窗出现的秒数。1.5s 是探针里跑通的值；
#: 机器慢或图很大时可以加，但没有必要做成配置项 —— 这只是个等待上限。
OPEN_WAIT = 1.8
#: 关预览窗后再等界面回稳（否则下一帧截图还停在预览上）。
CLOSE_WAIT = 0.5


def _top_level_windows(pid: int) -> dict[int, str]:
    """枚举某个进程的所有顶层可见窗口 → {hwnd: class_name}。

    用窗口**集合差**来认预览窗，而不是靠标题匹配：微信的图片预览窗在不同版本里
    标题不一样（有的空、有的写文件名），而「双击前后多出来的那个窗口」是稳定的判据。
    """
    out: dict[int, str] = {}
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _cb(hwnd, _lparam):
        wpid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, buf, 128)
            out[int(hwnd)] = buf.value or ""
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return out


def _rect_of(hwnd: int) -> tuple[int, int, int, int] | None:
    """窗口的屏幕矩形 (l, t, r, b)。最小化/零尺寸返回 None。"""
    r = wt.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    if r.right - r.left < 80 or r.bottom - r.top < 80:
        return None
    return int(r.left), int(r.top), int(r.right), int(r.bottom)


def _wechat_pid() -> int | None:
    """微信窗口所属进程 pid。

    ⚠️ `WindowInfo.rect` 是 `(left, top, width, height)`，**不是**右下角 ——
    拿 `rect[2] - rect[0]` 当窗口宽只在「微信最大化、left=0」时碰巧成立
    （探针那版就是这么写的，换个小窗就点偏）。这里一律按字段名取。
    `WindowInfo` 本身不带 pid，得回头问一次系统。
    """
    from chat_radar import capture as cap

    w = cap.find_wechat_window()
    if w is None:
        return None
    pid = wt.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(int(w.hwnd), ctypes.byref(pid))
    return int(pid.value) or None


def _biggest_left_block(frame):
    """聊天区里最大的那个**对方发的**图块。

    只取 left（对方）那侧：用户自己发的图不需要读 —— 他知道那是什么。
    高度门槛 140px 是「比一条文字消息明显高」的分界，避免双击到表情。
    """
    from chat_radar import layout as L

    res = L.analyze(frame.image, frame.roi)
    cands = [b for b in res.chat_blocks if b.side == "left" and b.height >= 140]
    if not cands:
        return None
    return max(cands, key=lambda x: x.height * x.width)


def grab_biggest_image(out_path: str | Path | None = None) -> tuple[str | None, str]:
    """双击最大的那张图 → 截预览窗 → 关窗。返回 (图片路径, 人话说明)。

    失败一律返回 (None, 原因)，不抛 —— 用户在界面上点了一下，得看到一句话，
    而不是一个异常。抢焦点的事必须**保证收尾**：预览窗没关掉会让用户莫名其妙。
    """
    from chat_radar import capture as cap
    from PIL import ImageGrab

    cap.enable_dpi_awareness()
    frame = cap.capture_frame()
    if frame is None:
        return None, "没抓不到微信窗口（可能最小化了）"
    block = _biggest_left_block(frame)
    if block is None:
        return None, "这一屏没有能放大的图"

    left, top, ww, wh = frame.window.rect
    # 位图 → 窗口内坐标再 → 屏幕坐标。比例拿 width/height 算，
    # 不能拿 rect（见 _wechat_pid 的注释）。
    sx = frame.image.shape[1] / max(1, ww)
    sy = frame.image.shape[0] / max(1, wh)
    roi = frame.roi
    bx = roi.left + (block.x0 + block.x1) / 2
    by = roi.top + (block.y0 + block.y1) / 2
    cx = int(left + bx / sx)
    cy = int(top + by / sy)

    pid = _wechat_pid()
    before = _top_level_windows(pid) if pid else {}

    import wechat_voice as wv

    wv._mouse_move_abs(cx, cy)
    time.sleep(0.08)
    for _ in range(2):
        wv._mouse_left(True)
        time.sleep(0.03)
        wv._mouse_left(False)
        time.sleep(0.06)
    time.sleep(OPEN_WAIT)

    shot = None
    note = ""
    try:
        after = _top_level_windows(pid) if pid else {}
        new_hwnds = [h for h in after if h not in before]
        # 预览窗通常是新出现的那个；找不到新窗口就退回全屏截图（探针里验证过也能用）。
        rect = None
        for h in new_hwnds:
            rr = _rect_of(h)
            if rr and (rr[2] - rr[0]) * (rr[3] - rr[1]) > 200000:
                rect = rr
                break
        if rect:
            shot = ImageGrab.grab(bbox=rect, all_screens=True)
        else:
            shot = ImageGrab.grab(all_screens=True)
            note = "没认出预览窗，已按整屏截"
    except Exception as exc:  # noqa: BLE001
        return None, f"截图失败：{type(exc).__name__}"
    finally:
        # 无论截没截到，都得把预览窗关掉 —— 它是被我们打开的。
        ctypes.windll.user32.keybd_event(0x1B, 0, 0, 0)
        time.sleep(0.05)
        ctypes.windll.user32.keybd_event(0x1B, 0, 2, 0)
        time.sleep(CLOSE_WAIT)

    if shot is None:
        return None, note or "截图失败"
    out = Path(out_path) if out_path else OUT_DIR / "grabbed.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    shot.convert("RGB").save(out, "PNG", optimize=True)
    w, h = shot.size
    return str(out), note or f"{w}×{h}"
