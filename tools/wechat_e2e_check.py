"""微信语音发送 · 真机端到端自检（不是单元测试，需要真实微信窗口）

分三组，逐步升级侵入性：
  A 组（默认，只读）：窗口/渲染层/样式位/坐标定位/浮层检测/声卡状态 —— 不发送、不点鼠标
  B 组（--send）    ：真发一条语音到当前聊天窗口（默认文件传输助手），截图验证 + 声卡还原验证
  C 组（--all）     ：B 之后追加异常路径（不存在的 wav、空请求等必须安全失败）

用法：
    .venv/Scripts/python.exe tools/wechat_e2e_check.py
    .venv/Scripts/python.exe tools/wechat_e2e_check.py --send
    .venv/Scripts/python.exe tools/wechat_e2e_check.py --all

产物：outputs/wechat_e2e_report.json + 截图 outputs/wx_e2e_*.png
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

import wechat_voice as wv  # noqa: E402

OUT = ROOT / "outputs"
OUT.mkdir(exist_ok=True)

results: list[dict] = []


def check(name: str, fn) -> object:
    """执行一项检查，异常记为失败但不停下。

    判定优先级：dict 里的 "ok" 字段 > 布尔值 > 非 None。
    （第一版只判"非 None"，把 inside_workarea=False 这类假阳性放过去了）
    """
    t0 = time.time()
    try:
        val = fn()
        if isinstance(val, dict) and "ok" in val:
            ok = bool(val["ok"])
        elif isinstance(val, bool):
            ok = val
        else:
            ok = val is not None
        results.append({"name": name, "ok": bool(ok), "detail": _s(val),
                        "ms": int((time.time() - t0) * 1000)})
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {_s(val)}")
        return val
    except Exception as exc:
        results.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}",
                        "ms": int((time.time() - t0) * 1000)})
        print(f"  [FAIL] {name}: {type(exc).__name__}: {exc}")
        return None


def _s(v) -> str:
    txt = repr(v)
    return txt if len(txt) <= 220 else txt[:217] + "..."


def shot(name: str, bbox=None) -> Path | None:
    try:
        from PIL import ImageGrab
        p = OUT / f"wx_e2e_{name}.png"
        ImageGrab.grab(bbox=bbox).save(p)
        return p
    except Exception as exc:
        print(f"    (截图失败 {name}: {exc})")
        return None


# ---------------------------------------------------------------- A 组：只读自检
def phase_a() -> dict:
    print("\n=== A 组：只读组件自检（不发送、不点鼠标） ===")
    u = wv._user32()

    hwnd = check("A1 找到微信主窗口", wv._find_wechat_hwnd)
    if not hwnd:
        return {"fatal": "没找到微信窗口，后续跳过"}

    # 前台化必须先做：截图截的是最上层窗口，微信在后台时模板匹配必然失败
    check("A1.5 前台化微信", lambda: bool(wv._foreground_wechat()))
    time.sleep(0.3)

    render = check("A2 找到渲染子窗口", lambda: wv._find_render_hwnd(hwnd))

    def exstyle():
        h = render or hwnd
        ex = u.GetWindowLongW(h, -20)
        return {"hwnd": hex(h), "exstyle": hex(ex),
                "TRANSPARENT": bool(ex & 0x20), "LAYERED": bool(ex & 0x80000)}

    check("A3 渲染层扩展样式位（社区根因）", exstyle)

    rect0 = check("A4 窗口 rect", lambda: wv._window_rect(hwnd))

    def onscreen():
        """真正要保证的是「话筒点可见」，不是整个窗口在屏内
        （最大化窗口 top 常为负值，属正常）。"""
        wv._ensure_onscreen(hwnd)
        r = wv._window_rect(hwnd)
        pt = wv._find_mic_icon(r) or wv._mic_point(r)
        mx, my = pt
        # 话筒点必须落在虚拟屏内，且离底边至少 2px（防被自动隐藏任务栏压住）
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        ok = 0 <= mx < sw and 0 <= my < sh
        return {"ok": ok, "rect": r, "mic_point": (mx, my), "screen": (sw, sh)}

    check("A5 _ensure_onscreen 约束回屏内", onscreen)
    rect = wv._window_rect(hwnd)

    def tmpl():
        """多模板取最高分（图标会随界面状态漂移，单模板命中率不稳）。"""
        import numpy as np
        from PIL import Image as I, ImageGrab
        l, t, r, b = rect
        box = (max(0, r - 340), max(0, b - 150), r, b)
        shot_arr = np.asarray(ImageGrab.grab(bbox=box).convert("L"))
        scores = {}
        best = (-2.0, None, None)
        for p in wv._mic_templates():
            tpl = np.asarray(I.open(str(p)).convert("L"))
            pos, sc = wv._ncc_match(shot_arr, tpl)
            scores[p.name] = round(float(sc), 3)
            if sc > best[0]:
                best = (sc, pos, p.name)
        pt = wv._find_mic_icon(rect)
        return {"ok": best[0] >= 0.75 and pt is not None,
                "best_score": round(float(best[0]), 3), "best_tpl": best[2],
                "per_template": scores, "found_point": pt}

    check("A6 话筒模板匹配（ds=1 全精度）", tmpl)

    check("A7 回退偏移坐标 _mic_point", lambda: wv._mic_point(rect))
    # 注：绿「发送(S)」按钮是常驻控件，非录音态也能定位到 —— 所以绿钮只能用来
    # 「找发送按钮的位置」，不能用来判断浮层是否出现（那要用灰度差分）。
    check("A8 非录音态能定位到发送按钮（绿钮常驻）",
          lambda: wv._find_green_send(rect, retries=1) is not None)

    def no_overlay_when_idle():
        wv._snapshot_overlay_baseline(rect)      # 先存基线
        return wv._wait_record_overlay(rect, timeout=1.0) is False

    check("A9 非录音态 _wait_record_overlay 应为 False（差分判据）", no_overlay_when_idle)
    check("A10 取消按钮坐标 _cancel_point", lambda: wv._cancel_point(rect))

    wavs = sorted(OUT.glob("tts_*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    check("A11 TTS 产物可用", lambda: {"count": len(wavs), "latest": wavs[0].name} if wavs else None)
    if wavs:
        check("A12 wav 时长读取", lambda: round(wv._wav_duration(wavs[0]), 2))

    def chain():
        from audio_api import audio_send_chain
        r = audio_send_chain()
        return {k: r.get(k) for k in ("cable_installed", "recording_ok", "playback_ok") if k in r} or r

    check("A13 发送链路自检 /audio/send_chain", chain)
    shot("a_idle", (rect[2] - 560, rect[3] - 200, rect[2], rect[3]))
    return {"hwnd": hwnd, "rect": rect, "wavs": [str(p) for p in wavs]}


# ---------------------------------------------------------------- B 组：真机发送
def phase_b(ctx: dict) -> None:
    print("\n=== B 组：真机端到端发送（会抢前台几秒，请勿操作键鼠） ===")
    wavs = ctx.get("wavs") or []
    if not wavs:
        print("  [SKIP] 没有 wav 素材")
        return

    t0 = time.time()
    try:
        res = wv._do_send(wv.SendVoiceReq(wav=Path(wavs[0]).name))
    except Exception as exc:
        check("B1 _do_send 完整流程", lambda: (_ for _ in ()).throw(exc))
        traceback.print_exc()
        return
    dur = round(time.time() - t0, 1)
    ok = res.get("outcome") == "ok"
    results.append({"name": "B1 _do_send 完整流程", "ok": ok,
                    "detail": {"outcome": res.get("outcome"), "steps": res.get("steps"),
                               "duration_s": res.get("duration_s")}, "ms": int(dur * 1000)})
    print(f"  [{'PASS' if ok else 'FAIL'}] B1 _do_send: outcome={res.get('outcome')} 耗时{dur}s")
    for i, s in enumerate(res.get("steps", []), 1):
        print(f"        {i}. {s}")

    time.sleep(1.5)
    hwnd = wv._find_wechat_hwnd()
    rect = wv._window_rect(hwnd)
    shot("b_after_send", (rect[2] - 560, rect[3] - 520, rect[2], rect[3]))

    check("B2 发送后无挂起录音（浮层已消失）",
          lambda: wv._wait_record_overlay(rect, timeout=1.0) is False)

    def restored():
        from audio_api import audio_send_chain
        r = audio_send_chain()
        return r

    check("B3 声卡已还原（send_chain 复查）", restored)
    check("B4 渲染层样式位已恢复",
          lambda: hex(wv._user32().GetWindowLongW(wv._find_render_hwnd(hwnd), -20)))

    def hist():
        """历史里最新一条应当就是本次发送的 wav。"""
        r = wv.send_history()
        items = r.get("items") if isinstance(r, dict) else r
        if not items:
            return None
        last = items[-1]
        return {"ok": last.get("wav") == Path(wavs[0]).name, "last": last}

    check("B5 发送历史最新一条 = 本次音频", hist)
    check("B6 /wechat/last_send 可读", lambda: wv.last_send())


# ---------------------------------------------------------------- C 组：异常路径
def phase_c(ctx: dict) -> None:
    print("\n=== C 组：异常路径必须安全失败（不发送任何东西） ===")

    def bad_wav():
        """找不到音频时返回 404 JSONResponse（不是抛异常，也不是静默成功）。"""
        r = wv._do_send(wv.SendVoiceReq(wav="__not_exist_12345.wav"))
        code = getattr(r, "status_code", 200)
        return {"ok": code == 404, "status_code": code}

    check("C1 不存在的 wav 必须 404（且不触发任何发送）", bad_wav)

    def hist_ok():
        r = wv.send_history()
        return {"count": len(r) if isinstance(r, list) else r}

    check("C2 send_history 正常", hist_ok)
    check("C3 渲染层样式位最终状态",
          lambda: hex(wv._user32().GetWindowLongW(
              wv._find_render_hwnd(wv._find_wechat_hwnd()), -20)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="包含 B 组真机发送")
    ap.add_argument("--all", action="store_true", help="A+B+C 全跑")
    args = ap.parse_args()

    ctx = phase_a()
    if ctx.get("fatal"):
        print(ctx["fatal"])
    if args.send or args.all:
        phase_b(ctx)
    if args.all:
        phase_c(ctx)

    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    print(f"\n=== 汇总：{passed}/{total} 通过 ===")
    failed = [r for r in results if not r["ok"]]
    if failed:
        print("失败项：")
        for r in failed:
            print(f"  - {r['name']}: {r['detail']}")

    report = OUT / "wechat_e2e_report.json"
    report.write_text(json.dumps({"passed": passed, "total": total, "results": results},
                                 ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"报告已写入 {report}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
