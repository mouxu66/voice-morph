#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDP（Chrome DevTools Protocol）小客户端：在 Electron 渲染进程里求值 JS。

用途：桌宠面板是 Chromium 渲染的，Electron 不开辅助功能时 UIA 是空壳，
「截图 + 相对坐标」能点但看不到内部状态（sayEl.value 到底是不是空的、
按钮的 DOM 矩形在哪）。开 `--remote-debugging-port=9222` 后就能直接问页面。

用法：
    .venv/Scripts/python.exe tools/desktop-control/cdp.py list
    .venv/Scripts/python.exe tools/desktop-control/cdp.py eval "document.title" --match pet.html
    .venv/Scripts/python.exe tools/desktop-control/cdp.py eval --file some.js --match pet.html

注意：需要应用带 `--remote-debugging-port=9222` 启动（默认端口 9222，可用 --port 改）。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request

import websockets.sync.client

try:  # 这台机器控制台是 GBK
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def devtools_targets(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
        return json.load(r)


def pick(targets: list[dict], match: str) -> dict:
    cands = [t for t in targets if t.get("type") in ("page", "webview", "other", "background_page")]
    if match:
        cands = [t for t in cands if match in (t.get("url", "") + t.get("title", ""))]
    if not cands:
        raise SystemExit("没找到匹配的调试目标（--match %r）。现有：%s" % (match, [(t.get('title'), t.get('url')) for t in targets]))
    return cands[0]


def cmd_list(args) -> int:
    for t in devtools_targets(args.port):
        print("%-14s | %-28s | %s" % (t.get("type"), (t.get("title") or "")[:28], (t.get("url") or "")[:90]))
        print("    ws=%s" % t.get("webSocketDebuggerUrl"))
    return 0


class Session:
    """一次 CDP 会话。"""

    def __init__(self, url: str):
        self.ws = websockets.sync.client.connect(url, max_size=32 * 1024 * 1024)
        self._id = 0

    def call(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise SystemExit("%s 失败: %s" % (method, msg["error"]))
                return msg.get("result", {})

    def eval(self, expr: str):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=True, userGesture=True)
        if "exceptionDetails" in r:
            raise SystemExit("JS 异常: " + json.dumps(r["exceptionDetails"], ensure_ascii=False)[:800])
        return r.get("result", {}).get("value")

    def rect(self, selector: str) -> dict:
        return json.loads(self.eval("JSON.stringify(document.querySelector(%s).getBoundingClientRect())" % json.dumps(selector)))

    def click(self, selector: str):
        """在元素中心派发**可信**鼠标事件（走浏览器自己的输入管线，等价真人点）。

        为什么不用鼠标坐标硬点：桌宠窗口 dpr=1.5 且面板会随行数变化上下伸缩，
        物理坐标算得再准也可能差一行；而 CDP 派发直接用元素的 DOM 矩形，
        并且绕过 WS_EX_TRANSPARENT（点击穿透）——穿透拦住的是系统输入，拦不住这个。
        """
        b = self.rect(selector)
        x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
        if b["width"] == 0 or b["height"] == 0:
            raise SystemExit("元素 %s 当前不可见（矩形 0×0）" % selector)
        for t, extra in (("mouseMoved", {}), ("mousePressed", {"clickCount": 1}), ("mouseReleased", {"clickCount": 1})):
            self.call("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left",
                      buttons=1 if t != "mouseReleased" else 0, **extra)
        return x, y

    def insert_text(self, text: str):
        """像输入法那样往当前聚焦元素插字（不靠键盘焦点/系统剪贴板）。"""
        self.call("Input.insertText", text=text)

    _KEYS = {
        "ArrowDown": (40, "ArrowDown"),
        "ArrowUp": (38, "ArrowUp"),
        "Enter": (13, "Enter"),
        "Escape": (27, "Escape"),
        "Tab": (9, "Tab"),
        "Backspace": (8, "Backspace"),
        "Delete": (46, "Delete"),
    }

    def key(self, name: str):
        """派发**可信**按键：走渲染进程自己的默认动作（<select> 的上下键等），

        与 SendInput 的区别：系统级 SendInput 只能进前台窗口，管不到渲染进程里
        已聚焦的控件默认行为；CDP 这条等价真人按键但不受窗口焦点影响。
        """
        if name not in self._KEYS:
            raise SystemExit("未收录的按键 %r，可用：%s" % (name, "/".join(self._KEYS)))
        vk, key = self._KEYS[name]
        for t in ("rawKeyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", type=t, code=name, key=key,
                      windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
        return name


def with_session(port: int, match: str):
    import contextlib

    @contextlib.contextmanager
    def _cm():
        s = Session(pick(devtools_targets(port), match)["webSocketDebuggerUrl"])
        try:
            yield s
        finally:
            s.ws.close()
    return _cm()


def cmd_click(args) -> int:
    with with_session(args.port, args.match or "pet.html") as s:
        x, y = s.click(args.selector)
    print("click %s @ CSS(%.0f,%.0f)" % (args.selector, x, y))
    return 0


def cmd_type(args) -> int:
    with with_session(args.port, args.match or "pet.html") as s:
        s.click(args.selector)
        s.insert_text(args.text)
        val = s.eval("document.querySelector(%s).value" % json.dumps(args.selector))
    print("type → %s.value = %r" % (args.selector, val))
    return 0


def cmd_key(args) -> int:
    with with_session(args.port, args.match or "pet.html") as s:
        s.key(args.name)
    print("key %s dispatched" % args.name)
    return 0


def cmd_eval(args) -> int:
    src = args.expr
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            src = f.read()
    t = pick(devtools_targets(args.port), args.match or "")
    with websockets.sync.client.connect(t["webSocketDebuggerUrl"], max_size=32 * 1024 * 1024) as ws:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": src, "returnByValue": True, "awaitPromise": True, "userGesture": True},
        }))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                break
    res = msg.get("result", {})
    if "exceptionDetails" in res:
        print("JS 异常:", json.dumps(res["exceptionDetails"], ensure_ascii=False)[:2000])
        return 1
    val = res.get("result", {}).get("value")
    if isinstance(val, (dict, list)):
        print(json.dumps(val, ensure_ascii=False, indent=2))
    else:
        print(val)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Electron/Chromium 渲染进程 JS 求值（CDP）")
    ap.add_argument("--port", type=int, default=9222)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(func=cmd_list)
    p_click = sub.add_parser("click", help="在元素中心派发可信点击（CDP 输入管线）")
    p_click.add_argument("selector")
    p_click.add_argument("--match", default="")
    p_click.set_defaults(func=cmd_click)

    p_type = sub.add_parser("type", help="点进元素后插入文本（Input.insertText）")
    p_type.add_argument("selector")
    p_type.add_argument("text")
    p_type.add_argument("--match", default="")
    p_type.set_defaults(func=cmd_type)

    p_key = sub.add_parser("key", help="派发可信按键（ArrowDown/Enter/Escape…）")
    p_key.add_argument("name")
    p_key.add_argument("--match", default="")
    p_key.set_defaults(func=cmd_key)

    p_eval = sub.add_parser("eval")
    p_eval.add_argument("expr", nargs="?", default="")
    p_eval.add_argument("--file", default="")
    p_eval.add_argument("--match", default="")
    p_eval.set_defaults(func=cmd_eval)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
