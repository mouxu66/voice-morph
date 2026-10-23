"""命令行入口。

    python -m chat_radar probe              # 窗口 + 聊天区 ROI 探测
    python -m chat_radar once [--save]      # 抓一帧看结果
    python -m chat_radar watch [-i 0.5]     # 常驻轮询
    python -m chat_radar bench [-n 20]      # 测单帧耗时

`--save DIR` 会输出两张图：`frame.png`（整窗）与 `blocks.png`（画出判定框），
红=左、蓝=右、橙=居中。核对准确率时看这张图最快。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import capture as cap
from . import judge as jd
from . import layout as ly
from .radar import Radar


def _save_visual(frame: cap.Frame, lay: ly.Layout, out_dir: Path) -> list[Path]:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        print("  （未安装 Pillow，跳过可视化）")
        return []

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    full = Image.fromarray(frame.image)
    p = out_dir / "frame.png"
    full.save(p)
    paths.append(p)

    roi = frame.roi
    sub = full.crop((roi.left, roi.top, roi.right, roi.bottom)).convert("RGB")
    d = ImageDraw.Draw(sub)
    colors = {"left": (220, 40, 40), "right": (30, 90, 220), "center": (200, 120, 0)}
    for i, b in enumerate(lay.blocks, 1):
        c = colors.get(b.side, (120, 120, 120))
        d.rectangle([b.x0 - 2, b.y0 - 2, b.x1 + 2, b.y1 + 2], outline=c, width=2)
        d.text((4, max(0, b.y0 - 12)), f"{i}{b.kind[0]}{b.side[0]}", fill=c)
    p = out_dir / "blocks.png"
    sub.save(p)
    paths.append(p)
    return paths


def cmd_probe(args) -> int:
    cap.enable_dpi_awareness()
    win = cap.find_wechat_window()
    if win is None:
        print("没找到微信窗口。微信是否在运行？")
        return 1
    print(f"窗口: [{win.cls}] {win.title!r}  {win.width}x{win.height} @ ({win.left},{win.top})")

    img = cap.grab_window(win.hwnd)
    if img is None:
        print("抓取失败（PrintWindow 返回 None）")
        return 1
    print(f"位图: {img.shape[1]}x{img.shape[0]}  mean={img.mean():.1f}  black%={(img.mean(axis=2)<10).mean()*100:.1f}")
    if img.mean() < 3:
        print("→ 全黑：微信可能已最小化（最小化时 PrintWindow 无效）")
        return 1

    roi = cap.locate_chat_roi(img, debug=True)
    if roi is None:
        print("→ 聊天区 ROI 探测失败")
        return 1
    print(f"→ 聊天区: x={roi.left}..{roi.right} y={roi.top}..{roi.bottom} ({roi.width}x{roi.height})")

    if args.save:
        out = Path(args.save)
        out.mkdir(parents=True, exist_ok=True)
        (out / "roi.png").write_bytes(b"")
        try:
            from PIL import Image

            Image.fromarray(img).save(out / "window.png")
            Image.fromarray(roi.crop(img)).save(out / "roi.png")
            print(f"→ 已保存 {out/'window.png'} 与 {out/'roi.png'}")
        except Exception as e:
            print(f"  保存失败: {e}")
    return 0


def cmd_once(args) -> int:
    radar = Radar(use_llm=args.llm, debug=args.debug)
    tick = radar.tick()
    if not tick.ok:
        print(f"[错误] {tick.error}")
        return 1
    lay = tick.layout
    assert lay is not None
    print(
        f"聊天区 {lay.roi_w}x{lay.roi_h}｜{len(lay.blocks)} 块｜"
        f"解析 {lay.elapsed_ms:.1f}ms"
    )
    print(f"{'#':>2} {'类型':<6} {'方向':<7} {'y':>10} {'x':>12} {'宽':>4} {'亮度':>4}")
    print("-" * 56)
    for i, b in enumerate(lay.blocks, 1):
        print(
            f"{i:>2} {b.kind:<6} {b.side:<7} {b.y0:>4}-{b.y1:<5} "
            f"{b.x0:>5}-{b.x1:<6} {b.width:>4} {b.mean_l:>4.0f}"
        )

    print()
    print("信号:", json.dumps(tick.signals.as_dict(), ensure_ascii=False))
    print("结论:", json.dumps(tick.verdict.as_dict(), ensure_ascii=False))

    if args.save:
        paths = _save_visual(tick.frame, lay, Path(args.save))
        for p in paths:
            print("已保存", p)
    return 0


def cmd_watch(args) -> int:
    radar = Radar(use_llm=args.llm, debug=args.debug)
    live = args.live and sys.stdout.isatty()
    if not live:
        print(f"轮询中，间隔 {args.interval}s（Ctrl+C 退出）")
    last_key = None
    t0 = time.time()
    try:
        while True:
            tick = radar.tick()
            s, v = tick.signals, tick.verdict
            if not tick.ok:
                if last_key != ("err", tick.error):
                    last_key = ("err", tick.error)
                    print(f"[{time.strftime('%H:%M:%S')}] {tick.error}")
            else:
                gap = "?" if s.gap_seconds is None else f"{s.gap_seconds:.0f}s"
                if live:
                    # 单行刷新：gap 一直在涨，用户能直接看到「等了多少秒」在走
                    line = (
                        f"\r{time.strftime('%H:%M:%S')} "
                        f"{jd.ACTION_LABEL.get(v.action, v.action):<5} "
                        f"{v.score}/5 | {v.reason:<38} "
                        f"| 末条={s.last_side or '-':<5} 连发={s.streak} "
                        f"间隔={'≥' if s.gap_lower_bound else ' '}{gap:<6} "
                        f"{s.elapsed_ms:>3.0f}ms"
                    )
                    sys.stdout.write(line + "\x1b[K")
                    sys.stdout.flush()
                else:
                    key = (v.action, s.last_side, s.streak, s.tail_meta)
                    if key != last_key:
                        last_key = key
                        print(
                            f"[{time.strftime('%H:%M:%S')}] "
                            f"{jd.ACTION_LABEL.get(v.action, v.action):<5} "
                            f"| {v.score}/5 | {v.reason} "
                            f"| last={s.last_side} streak={s.streak} gap={gap} "
                            f"meta={s.tail_meta or '-'} | {s.elapsed_ms:.0f}ms"
                        )
            if args.seconds and time.time() - t0 >= args.seconds:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    if live:
        sys.stdout.write("\n")
    print("已停止")
    return 0


def cmd_bench(args) -> int:
    radar = Radar()
    radar.tick()  # 预热：第一次要算 ROI + CUDA/GDI 首次调用，不能计入统计
    times, errs = [], 0
    for _ in range(args.n):
        t0 = time.perf_counter()
        tick = radar.tick()
        times.append((time.perf_counter() - t0) * 1000)
        if not tick.ok:
            errs += 1
        time.sleep(0.05)
    if errs == args.n:
        print("全部失败：", radar.tick().error)
        return 1
    times.sort()
    print(
        f"n={len(times)} 失败={errs}｜"
        f"中位 {times[len(times)//2]:.1f}ms｜p95 {times[int(len(times)*0.95)]:.1f}ms｜"
        f"最小 {times[0]:.1f}ms｜最大 {times[-1]:.1f}ms"
    )
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m chat_radar", description="微信「该不该回」状态雷达")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("probe", help="探测窗口与聊天区矩形")
    sp.add_argument("--save", default="")
    sp.set_defaults(func=cmd_probe)

    so = sub.add_parser("once", help="抓一帧并打印结果")
    so.add_argument("--save", default="")
    so.add_argument("--llm", action="store_true", help="边界档调用千问（需 VM_RADAR_LLM_KEY）")
    so.add_argument("--debug", action="store_true")
    so.set_defaults(func=cmd_once)

    sw = sub.add_parser("watch", help="常驻轮询")
    sw.add_argument("-i", "--interval", type=float, default=0.8)
    sw.add_argument("--seconds", type=float, default=0)
    sw.add_argument("--live", action="store_true", help="单行刷新（终端里看 gap 每秒在涨）")
    sw.add_argument("--llm", action="store_true")
    sw.add_argument("--debug", action="store_true")
    sw.set_defaults(func=cmd_watch)

    sb = sub.add_parser("bench", help="测单帧耗时")
    sb.add_argument("-n", type=int, default=20)
    sb.set_defaults(func=cmd_bench)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
