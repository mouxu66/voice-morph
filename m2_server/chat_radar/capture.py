"""微信窗口抓取：PrintWindow 穿透遮挡 + 聊天区 ROI 自动定位。

两条实测结论决定了这个模块的写法（2026-10-23，RTX 5060 Laptop / 2560x1600 / 微信 4.1.x）：

1. **必须带 PW_RENDERFULLCONTENT(=2)**
   同一个 PrintWindow，flag=2 → mean=240.0 / 黑屏率 0.0%，还能穿透 WorkBuddy 的全屏遮挡；
   flag=0 → mean=5.8 / 黑屏率 97.7%，几乎全黑。
   这个参数差异会被误判成「Windows 抓不到 GPU 窗口」，实际只是没传 flag。

2. **ROI 必须自动探测，不能硬编码坐标**
   微信主窗口的布局是「左会话列表（背景 238）+ 右聊天区（背景 250）」，
   两者背景亮度差约 12，在列中位亮度上是清楚的台阶：
       列跳变实测 x=342（238 → 250），行跳变 y=130（标题栏下边）、y=1371（输入框上边）
   所以用「背景亮度台阶」定位，比任何固定像素坐标都稳，且窗口缩放后自动跟随。

不碰微信进程内存，不注入，不 Hook——只调 user32!PrintWindow 读像素。
"""

from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes as wt
from dataclasses import dataclass

import numpy as np

PW_RENDERFULLCONTENT = 2

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

_dpi_done = False


def enable_dpi_awareness() -> None:
    """进程级 DPI 感知：不设的话 125%/150% 缩放下 GetWindowRect 与位图尺寸对不上。"""
    global _dpi_done
    if _dpi_done:
        return
    # 先置位：这个设置一个进程只能生效一次，失败重试也没意义，
    # 不置位会导致每帧都白调两次 API。
    _dpi_done = True
    with contextlib.suppress(Exception):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
        return
    with contextlib.suppress(Exception):
        _user32.SetProcessDPIAware()
    _dpi_done = True


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    cls: str
    left: int
    top: int
    width: int
    height: int

    @property
    def rect(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)


@dataclass(frozen=True)
class Roi:
    """聊天区（消息区）矩形，坐标相对被抓的窗口位图。"""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def crop(self, arr: np.ndarray) -> np.ndarray:
        return arr[self.top : self.bottom, self.left : self.right]

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


# ---------------------------------------------------------------- 找窗口


def _enum_candidates() -> list[WindowInfo]:
    out: list[WindowInfo] = []

    def _cb(hwnd, _lparam):
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        tbuf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, tbuf, n + 1)
        title = tbuf.value

        cbuf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cbuf, 256)
        cls = cbuf.value

        r = wt.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        if w < 600 or h < 400:
            return True

        is_wechat_title = any(k in title for k in ("微信", "Weixin", "WeChat"))
        is_qt_window = "QWindowIcon" in cls
        if is_wechat_title or is_qt_window:
            out.append(WindowInfo(hwnd, title, cls, r.left, r.top, w, h))
        return True

    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    _user32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return out


def find_wechat_window() -> WindowInfo | None:
    """找微信主窗口。

    优先「标题含微信」+「Qt 窗口类」+ 面积最大——微信 4.x 主窗口类名形如
    `Qt51514QWindowIcon`（Qt 版本号会变，所以只匹配 `QWindowIcon` 后缀）。
    """
    cands = _enum_candidates()
    wechat = [w for w in cands if any(k in w.title for k in ("微信", "Weixin", "WeChat"))]
    pool = wechat or cands
    if not pool:
        return None
    return max(pool, key=lambda w: w.width * w.height)


# ---------------------------------------------------------------- 抓图


def grab_window(hwnd: int, flag: int = PW_RENDERFULLCONTENT) -> np.ndarray | None:
    """抓一个窗口的位图，返回 HxWx3 uint8（RGB）。失败返回 None。

    用 GetWindowDC + CreateCompatibleBitmap + PrintWindow + GetDIBits，
    全程 GDI，不需要 mss / pywin32 / DXGI。
    """
    enable_dpi_awareness()
    r = wt.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None

    hdc = _user32.GetWindowDC(hwnd)
    if not hdc:
        return None
    mdc = _gdi32.CreateCompatibleDC(hdc)
    bmp = _gdi32.CreateCompatibleBitmap(hdc, w, h)
    try:
        _gdi32.SelectObject(mdc, bmp)
        ok = _user32.PrintWindow(hwnd, mdc, flag)
        if not ok:
            return None

        bi = _BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -h  # 负高度 = 自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0

        buf = ctypes.create_string_buffer(w * h * 4)
        got = _gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        if got == 0:
            return None
        # BGRA → RGB
        arr = np.frombuffer(buf.raw, dtype=np.uint8).reshape(h, w, 4)
        return arr[:, :, 2::-1].copy()
    finally:
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(mdc)
        _user32.ReleaseDC(hwnd, hdc)


# ---------------------------------------------------------------- ROI 自动定位

#: 判定「背景台阶」的亮度差阈值。实测微信会话列表 238 vs 聊天区 250，差 12；
#: 取 4 是为了在深色主题/不同缩放下仍能触发，又不至于被抗锯齿边缘骗到。
_STEP_THRESHOLD = 4

#: 合理性区间：探出来的 ROI 落在这之外就认为探测失败，回退到比例估算。
_ROI_SANE = {
    "left": (0.02, 0.50),
    "top": (0.02, 0.35),
    "bottom": (0.45, 0.99),
}


def _sane(v: float, key: str) -> bool:
    lo, hi = _ROI_SANE[key]
    return lo <= v <= hi


def locate_chat_roi(arr: np.ndarray, debug: bool = False) -> Roi | None:
    """从窗口位图里找聊天区（消息区）矩形。

    思路：聊天区背景（实测 250）与会话列表背景（实测 238）不同，形成台阶。
        1. 列亮度 → 找「从会话列表进入聊天区」的那一列
        2. 行亮度（只在 [左边界, 右边框] 内统计）→ 从中间往上下扫 → 上/下边界
    全程 numpy 中位数，不受文字和气泡干扰（中位数天生抗离群值）。

    为什么不用「从右往左找第一个偏离背景的列」：
        聊天区里一旦出现一张大图，那几列的中位亮度会被图片拉低，
        扫描会停在图片左缘上——实测把左边界从 343 判成 568，整个 ROI 切掉一半。
        现在的判据要求「边界左侧是一段**均匀**的会话列表背景」（标准差 < 3），
        图片所在列方差大，不会被误认成会话列表。
    """
    if arr is None or arr.size == 0:
        return None
    H, W = arr.shape[:2]
    gray = arr[:, :, :3].mean(axis=2)

    # ---- 左右 ----
    colmed = np.median(gray, axis=0)
    chat_bg = float(np.median(colmed[int(W * 0.75) :]))
    is_bg = np.abs(colmed - chat_bg) <= _STEP_THRESHOLD

    left = None
    band = 40
    lo = max(band, int(W * 0.03))
    hi = int(W * 0.55)
    for x in range(lo, hi):
        if not is_bg[x] or (x > 0 and is_bg[x - 1]):
            continue  # 只接受「非背景 → 背景」的过渡
        left_band = colmed[x - band : x]
        right_band = colmed[x : x + band]
        if (
            float(left_band.std()) < 3.0  # 左侧是均匀的列表背景，不是图片
            and float(left_band.mean()) < chat_bg - _STEP_THRESHOLD
            and float(right_band.std()) < 6.0
            and float(right_band.mean()) > chat_bg - _STEP_THRESHOLD
        ):
            left = x
            break

    if left is None:
        # 回退：老办法（从右往左扫台阶），在没有会话列表的独立聊天窗里更适用
        for x in range(int(W * 0.60), 0, -1):
            if not is_bg[x]:
                left = x + 1
                break
    if left is None:
        left = int(W * 0.12)

    # 右边框：从最右往左找第一列「回到聊天区背景」的，再往右一列即内缘。
    # 注意是找「回到背景」而不是「偏离背景」——窗口最右几列是描边本身，
    # 找偏离会停在描边上，把十几 px 黑边留在 ROI 里，行投影会把它当成前景块。
    right = W
    for x in range(W - 1, int(W * 0.85), -1):
        if is_bg[x]:
            right = x + 1
            break

    if not _sane(left / W, "left"):
        left = int(W * 0.13)
    if right - left < W * 0.25:
        return None

    # ---- 上下 ----
    rowmed = np.median(gray[:, left:right], axis=1)
    msg_bg = float(np.median(rowmed[int(H * 0.30) : int(H * 0.55)]))

    top = None
    for y in range(int(H * 0.45), 0, -1):
        if abs(float(rowmed[y]) - msg_bg) > _STEP_THRESHOLD:
            top = y + 1
            break
    bottom = None
    for y in range(int(H * 0.55), H):
        if abs(float(rowmed[y]) - msg_bg) > _STEP_THRESHOLD:
            bottom = y
            break

    if top is None or not _sane(top / H, "top"):
        top = int(H * 0.08)
    if bottom is None or not _sane(bottom / H, "bottom"):
        bottom = int(H * 0.86)

    # 上下各留一点余量：台阶可能落在分隔线上，切进去会丢半行字
    top = max(0, top - 2)
    bottom = min(H, bottom + 1)
    if bottom - top < H * 0.2:
        return None

    roi = Roi(left=left, top=top, right=right, bottom=bottom)
    if debug:
        print(
            f"  [roi] 窗口 {W}x{H}｜聊天区 x={left}..{right} y={top}..{bottom}"
            f" ({roi.width}x{roi.height})｜chat_bg={chat_bg:.0f} msg_bg={msg_bg:.0f}"
        )
    return roi


# ---------------------------------------------------------------- 一站式


@dataclass
class Frame:
    """一帧抓取结果。"""

    window: WindowInfo
    image: np.ndarray  # 整窗 RGB
    roi: Roi


def capture_frame(debug: bool = False, window: WindowInfo | None = None) -> Frame | None:
    """抓一帧微信窗口并定位聊天区。找不到窗口 / 抓取失败 / ROI 探测失败都返回 None。"""
    win = window or find_wechat_window()
    if win is None:
        return None
    img = grab_window(win.hwnd)
    if img is None or img.size == 0:
        return None
    # 全黑 = 窗口最小化或 PrintWindow 失败（flag 传错时的典型症状）
    if float(img.mean()) < 3.0:
        return None
    roi = locate_chat_roi(img, debug=debug)
    if roi is None:
        return None
    return Frame(window=win, image=img, roi=roi)
