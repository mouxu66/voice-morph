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

⚠️ **2026-10-14 起判据已收敛为单一来源**：`m2_server/wechat_voice.py` 的
`_green_mask()` / `_green_column_clusters()` / `_pick_green_button()`。
本工具此前自带一份同源实现（就是它量出了 §2.19 的 0.97 半径偏差），
现在**改为直接调生产函数**，于是"工具说 OK、线上点偏"这种漂移不可能再发生。
`G_THRESHOLD` 等常量保留只为向后兼容旧输出格式，不再用于判定。
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

# --- 判据：唯一来源 = m2_server/wechat_voice.py（见文件头注释）---
sys.path.insert(0, str(ROOT / "m2_server"))
from wechat_voice import (  # noqa: E402
    MIN_BUTTON_W,
    MIN_GREEN_PIXELS,
    _green_column_clusters,
    _green_mask,
    _pick_green_button,
)

G_THRESHOLD = 150   # 仅作旧输出格式的展示，不参与判定
R_MAX = 120
B_MIN, B_MAX = 80, 180
# 浮层区域的取法（右下角）
REGION_W, REGION_H = 560, 140
MIN_HITS = MIN_GREEN_PIXELS


def green_mask(arr: np.ndarray) -> np.ndarray:
    """薄封装：真正的判据在 wechat_voice（此处转成 int 数组以兼容旧调用）。"""
    return _green_mask(arr)


def clusters(xs: np.ndarray) -> list[tuple[int, int]]:
    """按列把绿像素切成连续块 —— 直接转发生产实现。"""
    return _green_column_clusters(xs)


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
    # —— 与生产同源：直接用 _pick_green_button 的结果，不再自己挑一遍
    btn = next((c for c in reversed(cl) if c[1] - c[0] + 1 >= MIN_BUTTON_W), None)
    if btn:
        res["button"] = btn
        res["button_x"] = (btn[0] + btn[1]) // 2
    # 生产路径实际会给的点（单源，避免"工具算一套、线上算另一套"）
    prod = _pick_green_button(green_mask(arr))
    res["prod_point"] = prod
    if btn:
        # offset 的语义：**旧判据（全局质心）** 相对钮心的偏差 —— 保留它，
        # 因为这个工具的职责就是"量旧偏差有多大"，§2.19 的 −19px 就是这么来的。
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
        # 新的生产判据落在哪（修复后应当恒为 CENTER）
        if prod:
            p_ratio = abs(prod[0] - res["button_x"]) / radius
            res["prod_verdict"] = "CENTER" if p_ratio <= 0.5 else (
                "EDGE" if p_ratio <= 1.0 else "OUTSIDE")
        else:
            res["prod_verdict"] = "NO-BUTTON"
    else:
        res["verdict"] = "NO-BUTTON"
        res["prod_verdict"] = "NO-BUTTON"
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
    prod = VERDICT_TEXT.get(r.get("prod_verdict", "?"), "?")
    return (f"{name:34s} 绿像素 {r['hits']:5d} 质心 {str(r['centroid']):>12s} "
            f"钮 [{r.get('button')}] 偏差 {r.get('offset')}px (占半径 {r.get('off_ratio')}) {mark}\n"
            f"{'':34s} └ 修复后生产判据 → {r.get('prod_point')} {prod}")


def offline(target: Path, only_hits: bool) -> int:
    files = sorted(target.glob("*.png")) if target.is_dir() else [target]
    bad = 0
    bad_prod = 0
    tally: dict[str, int] = {}
    prod_tally: dict[str, int] = {}
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
        prod_tally[r.get("prod_verdict", "none")] = prod_tally.get(r.get("prod_verdict", "none"), 0) + 1
        if r.get("verdict") in ("OUTSIDE", "NO-BUTTON"):
            bad += 1
        if r.get("prod_verdict") in ("OUTSIDE", "NO-BUTTON"):
            bad_prod += 1
    print(f"\n汇总：{len(files)} 张")
    print(f"  旧判据（全局质心）：{tally}")
    print(f'    其中不稳定/点空：{bad} 张（EDGE 算「悬但可能命中」，不计入失败）')
    print(f"  新判据（最右够宽连通块，2026-10-14 落地）：{prod_tally}")
    print(f"    其中不稳定/点空：{bad_prod} 张")
    # 退出码看**新**判据 —— 工具的存在意义是体检当前生产路径
    return 1 if bad_prod else 0


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
