"""诊断：PostMessage 按下话筒后，录音浮层/绿钮到底出现在哪、什么颜色。

只读 + 一次按下，结束强制清理（点绿钮发送或点 × 取消），不留挂起录音。
用法：.venv/Scripts/python.exe tools/wx_overlay_probe.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import numpy as np  # noqa: E402
from PIL import ImageGrab  # noqa: E402

import wechat_voice as wv  # noqa: E402

OUT = ROOT / "outputs"


def scan(box, tag):
    """在 box 区域内找微信绿，返回 (质心屏幕坐标, 像素数, 颜色样例)。"""
    img = np.asarray(ImageGrab.grab(bbox=box).convert("RGB")).astype(int)
    green = (img[:, :, 1] > 150) & (img[:, :, 0] < 120) & (img[:, :, 2] > 80) & (img[:, :, 2] < 180)
    ys, xs = np.nonzero(green)
    if len(xs) > 30:
        cx, cy = int(xs.mean()) + box[0], int(ys.mean()) + box[1]
        sample = img[ys[len(ys) // 2], xs[len(xs) // 2]].tolist()
        return (cx, cy), len(xs), sample
    return None, len(xs), None


def main():
    hwnd = wv._foreground_wechat()
    wv._ensure_onscreen(hwnd)
    rect = wv._window_rect(hwnd)
    l, t, r, b = rect
    print("窗口 rect:", rect)
    point = wv._find_mic_icon(rect) or wv._mic_point(rect)
    print("话筒点:", point)

    # 大范围取域（比 _find_green_send 的 560x140 大得多，用来定位）
    wide = (max(0, r - 600), max(0, b - 520), r, b)
    narrow = (max(0, r - 560), max(0, b - 140), r, b)
    print("wide box:", wide, " narrow box(现代码):", narrow)

    print("\n[按下前] 基线扫描")
    print("  wide  :", scan(wide, "pre")[0:2])
    print("  narrow:", scan(narrow, "pre")[0:2])

    res = wv._postmsg_mouse(point, down=True)
    print("\nPostMessage DOWN ->", res is not None)
    if not res:
        print("按下失败，退出")
        return

    print("\n按下后轮询（每 0.5s，共 10 次）:")
    found = None
    for i in range(10):
        time.sleep(0.5)
        w, wn, ws = scan(wide, f"t{i}")
        n, nn, ns = scan(narrow, f"t{i}")
        print(f"  t{i}: wide={wn:5d}px @{w} | narrow={nn:5d}px @{n}")
        if w and wn > 50:
            found = w
            ImageGrab.grab(bbox=wide).save(OUT / f"wx_probe_wide_t{i}.png")
            break

    print("\n结论: 绿钮", "找到 " + str(found) if found else "未找到（浮层可能没出现）")

    # ---- 清理：优先点绿钮发送，找不到点 × 取消 ----
    print("\n清理中…")
    target = found or scan(wide, "clean")[0]
    if target:
        wv._postmsg_mouse(target, down=True)
        wv._postmsg_mouse(None, up=True, target=res[0], lparam=res[1])
        print("  已点绿钮发送 @", target)
    else:
        cp = wv._cancel_point(rect)
        if cp:
            wv._postmsg_mouse(cp, down=True)
            wv._postmsg_mouse(None, up=True, target=res[0], lparam=res[1])
            print("  已点 × 取消 @", cp)
    time.sleep(1.2)
    ImageGrab.grab(bbox=wide).save(OUT / "wx_probe_after_clean.png")
    left, n2, _ = scan(narrow, "post")
    print("清理后 narrow 绿像素:", n2, "（应接近 0）")


if __name__ == "__main__":
    main()
