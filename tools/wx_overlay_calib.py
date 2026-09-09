"""标定：用「截图差分」判定录音浮层是否出现，测出噪声与信号的量级差异。

微信输入框的绿色「发送(S)」按钮是常驻的，不能作为浮层判据（会假阳性）。
浮层出现会让右下角整块区域重绘，用灰度差分可以可靠区分。

流程：基线噪声 ×3 → 按下 → 信号采样 ×6 → 清理（点绿钮发送）。
"""
import sys
import time
from pathlib import Path

import numpy as np
from PIL import ImageGrab

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))
import wechat_voice as wv  # noqa: E402


def grab(rect):
    l, t, r, b = rect
    u = wv._user32()
    sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    box = (max(0, min(r, sw) - 560), max(0, min(b, sh) - 260), min(r, sw), min(b, sh))
    return np.asarray(ImageGrab.grab(bbox=box).convert("L")).astype(np.int16)


def diff(a, b):
    if a.shape != b.shape:
        return -1.0
    return float(np.abs(a - b).mean())


def main():
    hwnd = wv._foreground_wechat()
    wv._ensure_onscreen(hwnd)
    rect = wv._window_rect(hwnd)
    print("rect:", rect)

    print("\n--- 基线噪声（非录音态，相邻快照）---")
    base = grab(rect)
    noises = []
    for i in range(3):
        time.sleep(0.4)
        cur = grab(rect)
        d = diff(cur, base)
        noises.append(d)
        print(f"  noise{i}: {d:.3f}")
    print(f"  噪声最大: {max(noises):.3f}")

    point = wv._find_mic_icon(rect) or wv._mic_point(rect)
    print("\n按下话筒 @", point)
    res = wv._postmsg_mouse(point, down=True)
    if not res:
        print("按下失败")
        return

    print("\n--- 信号（按下后浮层出现）---")
    sigs = []
    for i in range(6):
        time.sleep(0.5)
        cur = grab(rect)
        d = diff(cur, base)
        sigs.append(d)
        print(f"  t{i}: {d:.3f}")
    print(f"  信号最大: {max(sigs):.3f}")
    print(f"\n建议阈值: {max(noises) * 2:.1f} ~ {min(sigs):.1f} 之间（取 {max(noises) * 3:.1f} 较稳）")

    # 清理
    print("\n清理中…")
    sp = wv._find_green_send(rect)
    if sp:
        wv._postmsg_mouse(sp, down=True)
        wv._postmsg_mouse(None, up=True, target=res[0], lparam=res[1])
        print("  已点发送 @", sp)
    else:
        cp = wv._cancel_point(rect)
        if cp:
            wv._postmsg_mouse(cp, down=True)
            wv._postmsg_mouse(None, up=True, target=res[0], lparam=res[1])
            print("  已点 × 取消 @", cp)
    time.sleep(1.2)
    print("清理后与基线差分:", f"{diff(grab(rect), base):.3f}")


if __name__ == "__main__":
    main()
