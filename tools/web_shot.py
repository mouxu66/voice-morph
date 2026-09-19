# -*- coding: utf-8 -*-
"""无头 Chrome 给指定前端路由出图 + 量横向溢出（改完前端自检用）。

用法（先起预览服务）：
    cd web && npx vite --port 5199
    python tools/web_shot.py --route /audition                 # 两主题 × 三档宽度
    python tools/web_shot.py --route /home --sizes 1280      # 只验一档
    python tools/web_shot.py --route /audition --themes dark

为什么要有它（`docs/犯错指南.md` §3.22 / §3.26）：
  - 别让用户当眼睛。截图 + `scrollWidth` 量测能挡住"看着挺对其实 768px 顶出视口"。
  - **必须先播种 localStorage**，否则被首启引导挡住，截出来的是引导页。
  - 路由是 **HashRouter**，所以要访问 `#/route`。
  - **`--window-size` 小于 ~900 时 headless 会自己缩放视口**（meta viewport 不参与）
    → 窄屏结论不可信，所以最小档默认就是 900。
  - 尺寸只能用**启动参数**给：CDP 的 `Emulation.setDeviceMetricsOverride` 与
    `captureScreenshot` 同用会**卡死**（实测两次 25s 超时，见
    §3.22 补充）；`captureBeyondViewport` 在长页上同样卡死。
  - 每个尺寸独立起一个 Chrome（不同 user-data-dir）：这样 window-size 才是干净的。

输出：`outputs/_web_shots/<route>-<宽>x<高>-<主题>.png`，并在 stdout 打印
每档的 scrollWidth/clientWidth（差值 >1px 即横向溢出，要修）。
"""
import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parent.parent
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
OUT = ROOT / "outputs" / "_web_shots"


def _targets(port: int) -> list:
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5))


class Tab:
    """一个极简 CDP 会话（够用就好，不引依赖）。"""

    def __init__(self, ws_url: str):
        self.ws = connect(ws_url, max_size=64 * 1024 * 1024)
        self.n = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv(timeout=timeout))
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result") or {}
        raise TimeoutError(method)

    def close(self) -> None:
        self.ws.close()


class NotLoaded(RuntimeError):
    """页面没加载成功（浏览器错误页 / 空壳）。这不是瞬时故障，重试没意义。"""


def _assert_app_loaded(info: dict, base: str) -> None:
    """确认截到的真是应用页面，而不是浏览器的错误页 / 空壳。

    为什么要这一步（2026-09-19 自己踩的）：预览服务没起来时，Chrome 会渲染一张
    "无法访问此网站"的错误页 —— 它同样有尺寸、同样不横向溢出、同样能截图，
    于是工具报了 **「全部通过」**。**假绿比没有工具更危险**：
    它让"今天没验"看起来像"验过了"。宁可在这里直接报错。
    """
    text = str(info.get("text") or "")
    heads = " ".join(str(h) for h in (info.get("heads") or []))
    bad_marks = ("无法访问此网站", "This site can't be reached", "ERR_CONNECTION",
                 "ERR_NAME_NOT_RESOLVED", "拒绝连接", "404 Not Found")
    for mark in bad_marks:
        if mark in text or mark in heads:
            raise NotLoaded(f"页面没加载成功（截到的是浏览器错误页：{mark}）—— "
                               f"先确认 {base} 上的预览服务真的起来了")
    if info.get("root_children", 1) == 0:
        raise RuntimeError("SPA 根节点是空的：应用没挂载（可能是路由/脚本报错），"
                           "别把这张图当验收结果")


def _launch(port: int, width: int, height: int) -> subprocess.Popen:
    prof = ROOT.parent / "tmp" / f"webshot-{port}"
    log = ROOT.parent / "tmp" / f"webshot-{port}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen([
        str(CHROME), "--headless=new", f"--remote-debugging-port={port}",
        f"--user-data-dir={prof}", f"--window-size={width},{height}",
        "--no-first-run", "--disable-gpu", "--hide-scrollbars", "about:blank",
    ], stdout=fh, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            if _targets(port):
                return proc
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError(f"Chrome({port}) 没起来："
                       + log.read_text(encoding="utf-8", errors="replace")[-600:])


def _shoot(port: int, base: str, route: str, width: int, height: int,
           theme: str, settle: float) -> tuple[Path, dict]:
    tab = Tab(_targets(port)[0]["webSocketDebuggerUrl"])
    try:
        tab.call("Page.enable")
        tab.call("Runtime.enable")
        # 关掉动效再截：氛围层在跑 rAF 时 captureScreenshot 会一直等不到稳定帧
        # （实测：亮色 3/3 成功、暗色 9/9 超时，就是暗色氛围层更重）。页面
        # styles/index.css 里有 prefers-reduced-motion 分支，模拟它即可。
        tab.call("Emulation.setEmulatedMedia", {"features": [
            {"name": "prefers-reduced-motion", "value": "reduce"},
        ]})
        seed = (f"localStorage.setItem('vm_first_launch_done','1');"
                f"localStorage.setItem('vm-theme','{theme}');"
                f"localStorage.setItem('vm-simple-mode','1');")
        tab.call("Page.addScriptToEvaluateOnNewDocument", {"source": seed})
        tab.call("Page.navigate", {"url": f"{base}/#{route}"})
        time.sleep(settle)
        probe = tab.call("Runtime.evaluate", {"returnByValue": True, "expression": (
            "JSON.stringify({sw: document.documentElement.scrollWidth,"
            "cw: document.documentElement.clientWidth,"
            "sh: document.documentElement.scrollHeight,"
            "root_children: (document.getElementById('root')||document.body).children.length,"
            "heads: [...document.querySelectorAll('h1,h2')].map(e=>e.textContent),"
            "text: document.body.innerText.slice(0,600)})")})
        info = json.loads(probe["result"]["value"])
        _assert_app_loaded(info, base)
        shot = tab.call("Page.captureScreenshot", {"format": "png"})
        OUT.mkdir(parents=True, exist_ok=True)
        tag = route.strip("/").replace("/", "_") or "root"
        f = OUT / f"{tag}-{width}x{height}-{theme}.png"
        f.write_bytes(base64.b64decode(shot["data"]))
        return f, info
    finally:
        tab.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="无头 Chrome 页面走查（截图 + 溢出量测）")
    ap.add_argument("--route", default="/home", help="前端路由，如 /audition")
    ap.add_argument("--base", default="http://127.0.0.1:5199", help="预览服务地址")
    ap.add_argument("--sizes", default="900,1280,1600", help="宽度档位（逗号分隔）")
    ap.add_argument("--height", type=int, default=1200,
                    help="视口高度；太高时 headless 分配渲染表面易失败（本机实测）")
    ap.add_argument("--themes", default="light,dark")
    ap.add_argument("--settle", type=float, default=5.0, help="导航后等待秒数")
    ap.add_argument("--port", type=int, default=9300)
    args = ap.parse_args()

    if not CHROME.exists():
        print(f"找不到 Chrome：{CHROME}")
        return 2
    widths = [int(x) for x in args.sizes.split(",") if x.strip()]
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    print(f"路由 {args.route} · 宽度 {widths} · 主题 {themes}\n")

    port = args.port
    results = []
    overflow_bad = 0
    missed = 0
    for theme in themes:
        for width in widths:
            port += 1
            # captureScreenshot 在本机会偶发不返回（C 盘页面文件紧张时 Chrome
            # 分配渲染表面会失败）。重试是必要的，不是偷懒：一次失败就放弃，
            # 会让人误以为"页面坏了"。
            shot = None
            last_err = None
            for attempt in range(3):
                proc = _launch(port, width, args.height)
                err = None
                try:
                    shot = _shoot(port, args.base, args.route, width, args.height,
                                  theme, args.settle)
                    break
                except NotLoaded as exc:
                    # 不是瞬时故障：别重试三次装样子，直接说清楚
                    print(f"!! {exc}")
                    return 2
                except Exception as exc:      # noqa: BLE001
                    err = last_err = exc
                finally:
                    proc.terminate()
                    time.sleep(1)
                port += 1
                # 注意 e/as 在 except 块结束后会被删除，必须先绑成普通变量再打印
                print(f"  {width}px/{theme} 第 {attempt + 1} 次失败：{err} —— 重试")
            if shot is None:
                print(f"{width}x{args.height}-{theme} 三次都没出图：{last_err}")
                missed += 1
                continue
            f, info = shot
            overflow = info["sw"] - info["cw"]
            if overflow > 1:
                overflow_bad += 1
            results.append((f, info))
            print(f"{f.name}  scroll={info['sw']}x{info['sh']}  "
                  f"横向溢出={overflow}px" + ("  ← 要修" if overflow > 1 else ""))

    for theme in themes:
        hit = next((i for f, i in results if f.name.endswith(f"-{theme}.png")), None)
        if hit:
            print(f"\n--- 标题层级（{theme}）---\n{hit['heads']}")
    # 「没出图」与「溢出」是两回事，分开报 —— 混在一起会让人以为页面布局坏了
    if overflow_bad:
        print(f"\n!! {overflow_bad} 档横向溢出，要修")
    if missed:
        print(f"!! {missed} 档没出图（Chrome 截图超时，不是布局问题；可重跑）")
    if not overflow_bad and not missed:
        print("\n全部通过：无横向溢出、每档都出图")
    return 1 if (overflow_bad or missed) else 0


if __name__ == "__main__":
    sys.exit(main())
