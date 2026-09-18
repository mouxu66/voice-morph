"""微信「绿色发送钮」判据体检工具（**只读**：不点任何东西、不改任何状态）。

回答一个具体问题：**像素链路点绿钮会不会点偏？**

背景（2026-09-18 复核）：`wechat_voice._find_green_send()` 用「右下 560x140 内
绿像素的**质心**」当发送钮位置，并且阈值只有 `>50 个像素`。但录音浮层的绿色不止
发送钮——波形虚线同样是绿色（实测把质心往左拉 18.4px，而钮半径只有 19px）。
UIA 可用时无所谓（UIA 直接给精确矩形），UIA 不可用（`VM_WECHAT_UIA=0`、或 Qt gate
热激活在微信重启后失效的窗口期）时这就是"点不点得中"的问题。

两种模式：

    # 离线重放：把历史截图喂进来（不需要微信在运行）
    .venv/Scripts/python.exe tools/wx_green_judge_check.py outputs/wx_longpress_5.png

    # 整个目录，只看有绿像素的
    .venv/Scripts/python.exe tools/wx_green_judge_check.py outputs --only-hits

    # 在线对比：对**当前**微信窗口调真实判据，再和 UIA 的发送钮矩形比（仍然不点）
    .venv/Scripts/python.exe tools/wx_green_judge_check.py --live

判据（与 wechat_voice 保持一致，改那边记得改这里）：
    绿 = G>150 且 R<120 且 80<B<180
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]

# Windows 控制台默认 GBK，打印非 GBK 字符会直接 UnicodeEncodeError 崩掉工具本身
# （✅/❌ 就是）。改成"UTF-8 + 遇错替换"，宁可字丑一点也不挂。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 与 m2_server/wechat_voice.py::_find_green_send 的色域一字不差
G_THRESHOLD = 150
R_MAX = 120
B_MIN, B_MAX = 80, 180
# 浮层区域的取法（右下角）
REGION_W, REGION_H = 560, 140
MIN_HITS = 50          # 判据要求的最小绿像素数
MIN_BUTTON_W = 20      # 合理的发送钮宽度下限（用来把按钮与波形虚线区分开）


def green_mask(arr: np.ndarray) -> np.ndarray:
    return ((arr[:, :, 1] > G_THRESHOLD) & (arr[:, :, 0] < R_MAX)
            & (arr[:, :, 2] > B_MIN) & (arr[:, :, 2] < B_MAX))


def clusters(xs: np.ndarray) -> list[tuple[int, int]]:
    """按列把绿像素切成连续块，返回 [(x0, x1), ...]。"""
    if not len(xs):
        return []
    cols = np.unique(xs)
    out, start, prev = [], int(cols[0]), int(cols[0])
    for c in cols[1:]:
        c = int(c)
        if c - prev > 2:                 # 允许 1px 断缝
            out.append((start, prev))
            start = c
        prev = c
    out.append((start, prev))
    return out


def analyse(arr: np.ndarray) -> dict:
    """返回判据在给定像素上的全部中间量（一切都是"如果点，会点在哪"）。"""
    m = green_mask(arr)
    ys, xs = np.nonzero(m)
    res = {"hits": int(len(xs)), "fires": bool(len(xs) >= MIN_HITS)}
    if not len(xs):
        return res
    res["bbox"] = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
    res["centroid"] = (int(xs.mean()), int(ys.mean()))     # ← 代码真正会点的地方
    cl = clusters(xs)
    res["clusters"] = cl
    # 发送钮 = 最右侧那个够宽的绿块（波形虚线在它左边）
    btn = next((c for c in reversed(cl) if c[1] - c[0] + 1 >= MIN_BUTTON_W), None)
    if btn:
        res["button"] = btn
        res["button_x"] = (btn[0] + btn[1]) // 2
        offset = res["centroid"][0] - res["button_x"]
        radius = max(1, (btn[1] - btn[0] + 1) / 2)
        res["offset"] = offset
        res["off_ratio"] = round(abs(offset) / radius, 2)
        # 三档：按钮是**圆的**，只看"落在外接矩形内"会给出假安全感——
        # 贴左/右边缘那一列上圆只有很小一段高度，实际上就是"赌一把"。
        ratio = abs(offset) / radius
        if ratio <= 0.5:
            res["verdict"] = "CENTER"      # 稳
        elif ratio <= 1.0:
            res["verdict"] = "EDGE"        # 落在钮内但贴边 = 悬
        else:
            res["verdict"] = "OUTSIDE"     # 已经出钮 = 点空气
    else:
        res["verdict"] = "NO-BUTTON"
    return res


VERDICT_TEXT = {
    "CENTER":    "CENTER 稳定命中钮心",
    "EDGE":      "EDGE 落在钮内但贴边（悬）",
    "OUTSIDE":   "OUTSIDE 已出钮＝点空气",
    "NO-BUTTON": "WARN 无成形的钮",
}


def fmt(name: str, r: dict) -> str:
    if not r["hits"]:
        return f"{name:34s} 绿像素 0（判据不触发）"
    mark = VERDICT_TEXT[r.get("verdict", "?")]
    return (f"{name:34s} 绿像素 {r['hits']:5d} 质心 {str(r['centroid']):>12s} "
            f"钮 [{r.get('button')}] 偏差 {r.get('offset')}px (占半径 {r.get('off_ratio')}) {mark}")


def offline(target: Path, only_hits: bool) -> int:
    files = sorted(target.glob("*.png")) if target.is_dir() else [target]
    bad = 0
    tally: dict[str, int] = {}
    for p in files:
        try:
            im = Image.open(p).convert("RGB")
        except Exception as e:
            print(f"{p.name:34s} 读不了: {e}")
            continue
        # 历史截图多是"右下角"裁剪片，按图片自身右下角取域（与线上一致的口径）
        box = (max(0, im.width - REGION_W), max(0, im.height - REGION_H), im.width, im.height)
        r = analyse(np.asarray(im.crop(box)).astype(int))
        if only_hits and not r["hits"]:
            continue
        print(fmt(p.name, r))
        tally[r.get("verdict", "none")] = tally.get(r.get("verdict", "none"), 0) + 1
        if r.get("verdict") in ("OUTSIDE", "NO-BUTTON"):
            bad += 1
    print(f"\n汇总：{len(files)} 张 → {tally}")
    print(f'其中不稳定/点空：{bad} 张（EDGE 算「悬但可能命中」，不计入失败）')
    return 1 if bad else 0


def live() -> int:
    """对当前微信窗口调真实判据，并与 UIA 的发送钮矩形对比（只截图，不点击）。"""
    sys.path.insert(0, str(ROOT / "m2_server"))
    import wechat_proc as wproc
    import wechat_voice as wv

    try:
        hwnd = wproc.find_wechat_hwnd()
    except Exception as e:      # 微信没开时它是抛错，不是返回 None
        print(f"微信未运行：在线模式跳过（离线模式不需要微信）—— {e}")
        return 0
    if not hwnd:
        print("微信未运行：在线模式跳过（离线模式不需要微信）")
        return 0
    rect = wv._window_rect(hwnd)
    print(f"微信窗口 rect = {rect}")
    pt = wv._find_green_send(rect)
    print(f"像素判据给的点 = {pt}")
    try:
        import wechat_uia as wuia
        box = wuia.send_button_rect() if wuia.uia_ready() else None
    except Exception as e:
        box = None
        print(f"UIA 不可用: {e}")
    if box:
        cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
        print(f"UIA 发送钮 rect = {box} → 中心 {cx, cy}")
        if pt:
            ok = box[0] <= pt[0] <= box[2] and box[1] <= pt[1] <= box[3]
            print("OK 像素判据落在 UIA 钮内" if ok else "BAD 像素判据偏出 UIA 钮外（这次会点空气）")
        else:
            print("WARN 像素判据没找到绿钮（浮层可能没起来，或走了 UIA 路径）")
    else:
        print("（没有 UIA 参照：多半是浮层没起来——本工具不点任何东西，"
              "要看浮层态请在录音持续期间再跑一次）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="微信绿色发送钮判据体检（只读）")
    ap.add_argument("target", nargs="?", default="outputs", help="PNG 文件或目录（默认 outputs）")
    ap.add_argument("--live", action="store_true", help="改测当前微信窗口（只截图，不点击）")
    ap.add_argument("--only-hits", action="store_true", help="离线模式只打印判据触发的那几张")
    a = ap.parse_args()
    if a.live:
        return live()
    t = Path(a.target)
    if not t.exists():
        print(f"找不到 {t}")
        return 2
    return offline(t, a.only_hits)


if __name__ == "__main__":
    raise SystemExit(main())
