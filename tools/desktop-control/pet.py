#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桌宠（Electron）交互助手 —— 定位窗口 → 局部截图（给我看）→ 局部坐标点击。

为什么需要它：桌宠是 Electron/Chromium 窗口，UIA 树近乎空壳（只有空 Pane），
所以拿不到「千问变声」按钮这类控件的矩形，只能走「截图 + 窗口相对坐标」。
但**窗口相对坐标对弹跳/移动免疫**：宠物会 Q 弹、引导气泡收起会改窗口尺寸，
绝对坐标随时过期，相对坐标只要窗口内的 CSS 布局不变就一直有效。

用法（在 D:\\变声 下）：
    .venv/Scripts/python.exe tools/desktop-control/pet.py rect
    .venv/Scripts/python.exe tools/desktop-control/pet.py look [--margin 30] [--zoom 2] [--grid 25]
    .venv/Scripts/python.exe tools/desktop-control/pet.py click 111 380 [--sleep 0.6]

`look` 输出一张图，上面叠了**窗口相对坐标**的网格（与 `click` 参数同一坐标系），
所以照着图读数就能点。所有坐标都是物理像素（点击用同一套约定）。
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import os
import subprocess
import sys
import time
from ctypes import wintypes

from PIL import Image, ImageDraw

try:  # 这台机器的控制台是 GBK，中文/emoji 直接 print 会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "tools", "desktop-control", "out")
DC = os.path.join(ROOT, "tools", "desktop-control", "dc.ps1")
PET_TITLE = "桌宠"

_user32 = ctypes.windll.user32
try:
    _user32.SetProcessDPIAware()  # 与 dc.ps1 一致：拿到物理像素
except Exception:
    pass


def pet_rect() -> tuple[int, int, int, int]:
    """桌宠窗口的 (x, y, w, h)，物理像素。"""
    hwnd = _user32.FindWindowW(None, PET_TITLE)
    if not hwnd:
        raise SystemExit("找不到桌宠窗口（标题「%s」）——应用没开或桌宠关了" % PET_TITLE)
    r = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        raise SystemExit("GetWindowRect 失败")
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def run_dc(*args: str) -> tuple[int, str]:
    p = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", DC, *args],
        # PowerShell 重定向输出走控制台代码页（本机 GBK/936），按 gbk 解码才不是乱码
        capture_output=True, text=True, encoding="gbk", errors="replace",
    )
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()


def full_shot() -> str:
    rc, out = run_dc("shot")
    if rc != 0:
        raise SystemExit("截图失败: " + out)
    files = glob.glob(os.path.join(OUT, "shot-*.png"))
    if not files:
        raise SystemExit("截图目录里没有 shot-*.png")
    return max(files, key=os.path.getmtime)


def cmd_rect(_args) -> int:
    x, y, w, h = pet_rect()
    print("桌宠窗口 rect=(%d,%d) size=%dx%d" % (x, y, w, h))
    return 0


def cmd_look(args) -> int:
    x, y, w, h = pet_rect()
    shot = full_shot()
    im = Image.open(shot).convert("RGB")
    m = args.margin
    # 默认看整个窗口；--region 只看其中一块（窗口相对坐标 dx,dy,w,h），用来放大看某个按钮
    bx, by, bw, bh = 0, 0, w, h
    if args.region:
        bx, by, bw, bh = (int(v) for v in args.region.split(","))
    box = (max(0, x + bx - m), max(0, y + by - m),
           min(im.width, x + bx + bw + m), min(im.height, y + by + bh + m))
    crop = im.crop(box)
    if args.zoom != 1:
        crop = crop.resize((crop.width * args.zoom, crop.height * args.zoom), Image.LANCZOS)

    d = ImageDraw.Draw(crop)
    step = args.grid
    if step > 0:
        # 网格标签用【窗口相对坐标】（窗口左上角 = 0,0），与 click 参数同一坐标系
        x0, x1 = bx - m, bx + bw + m
        y0, y1 = by - m, by + bh + m
        for gx in range(x0 - (x0 % step), x1 + 1, step):
            px = (gx - bx + m) * args.zoom
            if not (0 <= px < crop.width):
                continue
            major = gx % (step * 2) == 0
            d.line([(px, 0), (px, crop.height)], fill=(0, 120, 255) if major else (170, 205, 235), width=1)
            if major:
                d.text((px + 2, 2), str(gx), fill=(200, 30, 30))
        for gy in range(y0 - (y0 % step), y1 + 1, step):
            py = (gy - by + m) * args.zoom
            if not (0 <= py < crop.height):
                continue
            major = gy % (step * 2) == 0
            d.line([(0, py), (crop.width, py)], fill=(0, 120, 255) if major else (170, 205, 235), width=1)
            if major:
                d.text((2, py + 2), str(gy), fill=(200, 30, 30))

    name = "pet-%s.jpg" % time.strftime("%H%M%S")
    path = os.path.join(OUT, name)
    crop.save(path, quality=88)
    print("桌宠窗口 rect=(%d,%d) size=%dx%d  截图=%s  尺寸=%dx%d zoom=%d"
          % (x, y, w, h, os.path.relpath(path, ROOT), crop.width, crop.height, args.zoom))
    return 0


def set_cursor(x: int, y: int) -> None:
    if not _user32.SetCursorPos(int(x), int(y)):
        raise SystemExit("SetCursorPos 失败 (%d,%d)" % (x, y))


def glide_to(x: int, y: int, steps: int = 8, delay: float = 0.02) -> None:
    """分几步把光标移过去，而不是瞬移。

    为什么不能一步到位：桌宠窗口默认 `setIgnoreMouseEvents(true, {forward:true})`，
    页面靠 forwarded mousemove 做命中测试再申请可交互。一次瞬移只产生一个移动消息，
    实测不足以稳定触发 mouseenter（悬停不出面板，点击还会穿到底下窗口）。
    分步移动模拟真实鼠标，页面能拿到一串 mousemove —— 这才是人类操作的样子。
    """
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    p = POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    for i in range(1, steps + 1):
        set_cursor(round(p.x + (x - p.x) * i / steps), round(p.y + (y - p.y) * i / steps))
        time.sleep(delay)


GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_FRAMECHANGED = 0x0001, 0x0002, 0x0004, 0x0020

_swlp = getattr(_user32, "SetWindowLongPtrW", _user32.SetWindowLongW)
_gwlp = getattr(_user32, "GetWindowLongPtrW", _user32.GetWindowLongW)
_swlp.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
_swlp.restype = ctypes.c_ssize_t
_gwlp.argtypes = [wintypes.HWND, ctypes.c_int]
_gwlp.restype = ctypes.c_ssize_t


def cdp_eval(expr: str):
    """在桌宠渲染进程里求值 JS（需应用带 --remote-debugging-port 启动）。

    这是「不看像素”看真相」的途径：DOM 矩形、input.value、面板 display 都能直接问。
    """
    import json as _json
    import urllib.request

    import websockets.sync.client

    port = int(os.environ.get("VM_CDP_PORT", "9222"))
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
        targets = _json.load(r)
    cands = [t for t in targets if "pet.html" in (t.get("url") or "")]
    if not cands:
        raise SystemExit("CDP 里没找到 pet.html（应用是不是没带 --remote-debugging-port 启动？）")
    with websockets.sync.client.connect(cands[0]["webSocketDebuggerUrl"], max_size=8 * 1024 * 1024) as ws:
        ws.send(_json.dumps({"id": 1, "method": "Runtime.evaluate",
                             "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
        while True:
            msg = _json.loads(ws.recv())
            if msg.get("id") == 1:
                break
    res = msg.get("result", {})
    if "exceptionDetails" in res:
        raise SystemExit("JS 异常: " + _json.dumps(res["exceptionDetails"], ensure_ascii=False)[:800])
    return res.get("result", {}).get("value")


def cmd_dom(args) -> int:
    val = cdp_eval(args.expr)
    if isinstance(val, (dict, list)):
        import json as _json
        print(_json.dumps(val, ensure_ascii=False, indent=2))
    else:
        print(val)
    return 0


def cmd_hoverel(args) -> int:
    """把光标移到某元素的**真实中心**（DOM 矩形 → 物理坐标）。

    比“看截图估像素”可靠得多：dpr=1.5 时 CSS ↔ 物理差 1.5 倍，估出来的坐标
    很容易差半行（2026-09-18 就因此点错过按钮）。
    """
    r = cdp_eval("JSON.stringify(document.querySelector(%s).getBoundingClientRect())" % repr(args.selector))
    import json as _json
    box = _json.loads(r)
    wx, wy, _w, _h = pet_rect()
    dpr = cdp_eval("devicePixelRatio")
    tx = wx + round((box["x"] + box["width"] / 2) * dpr)
    ty = wy + round((box["y"] + box["height"] / 2) * dpr)
    if args.click:
        glide_to(tx, ty, steps=1)
        time.sleep(0.3)
        rc, out = run_dc("click", "-X", str(tx), "-Y", str(ty))
        print("click %s 中心 → 屏幕(%d,%d) rc=%d" % (args.selector, tx, ty, rc))
    else:
        glide_to(tx, ty, steps=args.steps)
        print("hover %s 中心 → 屏幕(%d,%d)" % (args.selector, tx, ty))
    time.sleep(args.sleep)
    return 0


def cmd_exstyle(args) -> int:
    hwnd = _user32.FindWindowW(None, PET_TITLE)
    ex = _gwlp(hwnd, GWL_EXSTYLE)
    print("桌宠 EXSTYLE=0x%08X  WS_EX_TRANSPARENT(点击穿透)=%s" % (ex, bool(ex & WS_EX_TRANSPARENT)))
    return 0


def cmd_unwrap(args) -> int:
    """外部摘掉桌宠窗口的 WS_EX_TRANSPARENT（点击穿透）。

    为什么要这步（实测根因，2026-09-18）：桌宠平时是
    `setIgnoreMouseEvents(true, {forward:true})`。Electron 的 forward 靠低级鼠标钩子转发移动，
    **合成的（injected）鼠标移动不会被转发**，于是页面永远收不到 mousemove、
    也就永远不调 setIgnoreMouse(false) —— 面板不出来，注入的点击还直接穿透到底下的窗口。
    先摘掉 WS_EX_TRANSPARENT，鼠标消息就能正常送到桌宠；
    而鼠标一落到角色上，页面自己的 mousemove 处理就会主动 setIgnoreMouse(false)，
    之后它自己保持可交互（和真人操作完全一样的路径）。
    """
    hwnd = _user32.FindWindowW(None, PET_TITLE)
    if not hwnd:
        raise SystemExit("找不到桌宠窗口")
    before = _gwlp(hwnd, GWL_EXSTYLE)
    after = (before | WS_EX_TRANSPARENT) if args.restore else (before & ~WS_EX_TRANSPARENT)
    _swlp(hwnd, GWL_EXSTYLE, after)
    _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                         SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
    now = _gwlp(hwnd, GWL_EXSTYLE)
    print("EXSTYLE 0x%08X → 0x%08X（穿透=%s）" % (before, now, bool(now & WS_EX_TRANSPARENT)))
    return 0


def post_mouse(hwnd: int, dx: int, dy: int, click: bool) -> None:
    """把鼠标消息**直接投给桌宠窗口**（P/Invoke PostMessage），绕开命中测试。

    为什么需要这条路径：桌宠平时是 `setIgnoreMouseEvents(true,{forward:true})`，
    实测 forwarded mousemove 并不能可靠地让页面触发 mouseenter、也没把窗口切回可交互，
    于是「悬停出面板 / 点击不穿透」都时灵时不灵。PostMessage 不看命中测试、
    也不看 WS_EX_TRANSPARENT，消息只会落到这个窗口——顺带好处：
    永远不可能点到它底下的 Freebuff/主窗口。
    lParam 用的是**客户区坐标**；桌宠是无边框窗，客户区 ≈ 窗口相对坐标。
    """
    WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
    MK_LBUTTON = 0x0001
    lp = ((dy & 0xFFFF) << 16) | (dx & 0xFFFF)
    _user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
    if click:
        time.sleep(0.08)
        _user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
        time.sleep(0.05)
        _user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)


def cmd_rawmove(args) -> int:
    hwnd = _user32.FindWindowW(None, PET_TITLE)
    if not hwnd:
        raise SystemExit("找不到桌宠窗口")
    post_mouse(hwnd, args.dx, args.dy, click=False)
    time.sleep(args.sleep)
    print("rawmove → 桌宠窗口直接收 WM_MOUSEMOVE 客户区(%d,%d)" % (args.dx, args.dy))
    return 0


def cmd_rawclick(args) -> int:
    hwnd = _user32.FindWindowW(None, PET_TITLE)
    if not hwnd:
        raise SystemExit("找不到桌宠窗口")
    post_mouse(hwnd, args.dx, args.dy, click=True)
    time.sleep(args.sleep)
    print("rawclick → 桌宠窗口客户区(%d,%d)" % (args.dx, args.dy))
    return 0


def cmd_hover(args) -> int:
    """把光标真移到窗口相对坐标上并停住（不点击）。

    桌宠的快捷键面板是 mouseenter 触发的，且窗口默认点击穿透；
    所以「先移过去、等渲染器把窗口切成可交互」是点击能生效的前提。
    """
    x, y, _w, _h = pet_rect()
    glide_to(x + args.dx, y + args.dy, steps=args.steps)
    time.sleep(args.sleep)
    print("hover 窗口相对(%d,%d) → 停 %.2fs" % (args.dx, args.dy, args.sleep))
    return 0


def cmd_click(args) -> int:
    x, y, _w, _h = pet_rect()
    tx, ty = x + args.dx, y + args.dy
    # 先落点：让渲染器的 mousemove/命中测试跑完、把窗口从「点击穿透」切回可交互，
    # 否则这一下会直接穿到底下的窗口（Freebuff 之类）上。
    glide_to(tx, ty, steps=args.steps)
    time.sleep(args.hover)
    rc, out = run_dc("click", "-X", str(tx), "-Y", str(ty))
    last = out.splitlines()[-1] if out else ""
    print("click 窗口相对(%d,%d) → 屏幕(%d,%d) rc=%d %s" % (args.dx, args.dy, tx, ty, rc, last))
    time.sleep(args.sleep)
    return 0 if rc == 0 else 1


def ps_encoded(script: str) -> None:
    """用 -EncodedCommand 跑 PowerShell：绕开中文/emoji 在命令行参数上的编码坑。"""
    import base64
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    p = subprocess.run(["powershell.exe", "-NoProfile", "-EncodedCommand", enc],
                       capture_output=True, text=True, encoding="gbk", errors="replace")
    if p.returncode != 0:
        raise SystemExit("PowerShell 失败: " + (p.stdout or "") + (p.stderr or ""))


def cmd_paste(args) -> int:
    """把文本写进剪贴板再 Ctrl+V（Electron 无 UIA ValuePattern，只能粘贴输入）。

    中文/emoji 走文件 + UTF-8 读取，不走命令行参数。
    """
    txt = args.text
    if args.newline:
        txt += "\n"
    tmp = os.path.join(OUT, "_clip.txt")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(txt)
    ps_encoded("Set-Clipboard -Value ([IO.File]::ReadAllText('%s', [Text.Encoding]::UTF8))" % tmp.replace("'", "''"))
    time.sleep(0.15)
    rc, out = run_dc("keys", "-Keys", "^v")
    print("paste %d 字 rc=%d %s" % (len(txt), rc, out.splitlines()[-1] if out else ""))
    time.sleep(args.sleep)
    return 0 if rc == 0 else 1


def cmd_keys(args) -> int:
    rc, out = run_dc("keys", "-Keys", args.keys)
    print("keys %s rc=%d %s" % (args.keys, rc, out.splitlines()[-1] if out else ""))
    time.sleep(args.sleep)
    return 0 if rc == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="桌宠窗口定位 / 截图 / 相对坐标点击")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("rect", help="打印桌宠窗口矩形").set_defaults(func=cmd_rect)

    p_look = sub.add_parser("look", help="截桌宠窗口区域并叠加相对坐标网格")
    p_look.add_argument("--margin", type=int, default=30)
    p_look.add_argument("--zoom", type=int, default=2)
    p_look.add_argument("--grid", type=int, default=25)
    p_look.add_argument("--region", default="", help="只看窗口内一块：dx,dy,w,h（窗口相对坐标）")
    p_look.set_defaults(func=cmd_look)

    p_hover = sub.add_parser("hover", help="把光标移到窗口相对坐标并停住（触发 mouseenter 面板）")
    p_hover.add_argument("dx", type=int)
    p_hover.add_argument("dy", type=int)
    p_hover.add_argument("--sleep", type=float, default=1.0)
    p_hover.add_argument("--steps", type=int, default=8, help="分几步移过去（1 = 瞬移）")
    p_hover.set_defaults(func=cmd_hover)

    p_click = sub.add_parser("click", help="按窗口相对坐标点击（先悬停等窗口切回可交互）")
    p_click.add_argument("dx", type=int)
    p_click.add_argument("dy", type=int)
    p_click.add_argument("--hover", type=float, default=0.45, help="点击前先停住的秒数")
    p_click.add_argument("--steps", type=int, default=8, help="分几步移过去（1 = 瞬移）")
    p_click.add_argument("--sleep", type=float, default=0.6)
    p_click.set_defaults(func=cmd_click)

    p_paste = sub.add_parser("paste", help="把文本写进剪贴板并 Ctrl+V（Electron 无法用 UIA 填值）")
    p_paste.add_argument("text")
    p_paste.add_argument("--newline", action="store_true")
    p_paste.add_argument("--sleep", type=float, default=0.6)
    p_paste.set_defaults(func=cmd_paste)

    p_el = sub.add_parser("hoverel", help="把光标移到元素真实中心（DOM 矩形换算，比估像素准）")
    p_el.add_argument("selector")
    p_el.add_argument("--click", action="store_true", help="移过去后真点一下")
    p_el.add_argument("--steps", type=int, default=6)
    p_el.add_argument("--sleep", type=float, default=0.6)
    p_el.set_defaults(func=cmd_hoverel)

    p_dom = sub.add_parser("dom", help="在桌宠页面里求值 JS（需 --remote-debugging-port）")
    p_dom.add_argument("expr")
    p_dom.set_defaults(func=cmd_dom)

    p_ex = sub.add_parser("exstyle", help="看桌宠窗口的扩展样式（含是否点击穿透）")
    p_ex.set_defaults(func=cmd_exstyle)

    p_uw = sub.add_parser("unwrap", help="摘掉桌宠的点击穿透（--restore 还原）")
    p_uw.add_argument("--restore", action="store_true")
    p_uw.set_defaults(func=cmd_unwrap)

    p_rmove = sub.add_parser("rawmove", help="把 WM_MOUSEMOVE 直接投给桌宠窗口（客户区坐标）")
    p_rmove.add_argument("dx", type=int)
    p_rmove.add_argument("dy", type=int)
    p_rmove.add_argument("--sleep", type=float, default=1.0)
    p_rmove.set_defaults(func=cmd_rawmove)

    p_rclick = sub.add_parser("rawclick", help="把 WM_LBUTTONDOWN/UP 直接投给桌宠窗口（客户区坐标）")
    p_rclick.add_argument("dx", type=int)
    p_rclick.add_argument("dy", type=int)
    p_rclick.add_argument("--sleep", type=float, default=0.8)
    p_rclick.set_defaults(func=cmd_rawclick)

    p_keys = sub.add_parser("keys", help="向当前焦点窗口发按键（SendKeys 语法）")
    p_keys.add_argument("keys")
    p_keys.add_argument("--sleep", type=float, default=0.6)
    p_keys.set_defaults(func=cmd_keys)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
