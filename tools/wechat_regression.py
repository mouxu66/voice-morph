"""微信语音发送 · 自动回归（一键给出一条结论：这条链路现在还好不好）

与 `tools/wechat_e2e_check.py` 的分工：

    wechat_e2e_check   **诊断**：14 项逐条打印，人看报告去定位问题
    本脚本              **回归**：发一条已知时长的语音，按三条硬判据判成败，
                        退出码 0/1 可直接给 CI、计划任务、或"我改完想确认一下"用

三条判据（来自 docs/犯错指南.md §3.3，缺一不可）：

    1. 终态      steps 里出现「已点…发送」这类终态——`outcome=ok` 只代表代码
                 路径走完，不代表微信真收到
    2. 时长      UIA 读到的真实秒数 ≈ 音频 + 1~2s（静音头/播放对齐的固有开销）。
                 显示 60" 就是「按住状态未解除」那个最贵的坑复发（§2.2）
    3. 清场      发送后检测不到录音浮层——挂起的录音会污染后续所有测试（§2.11）

用法：

    .venv/Scripts/python.exe tools/wechat_regression.py
    .venv/Scripts/python.exe tools/wechat_regression.py --dry-run      # 只准备，不发消息
    .venv/Scripts/python.exe tools/wechat_regression.py --secs 6 --repeat 3
    .venv/Scripts/python.exe tools/wechat_regression.py --with-tts     # 走 TTS+RVC 全链（慢）

定时跑（Windows 计划任务，每天 21:00；需要微信已登录且窗口可见）：

    schtasks /Create /TN "变声-微信回归" /SC DAILY /ST 21:00 /F ^
      /TR "D:\\变声\\.venv\\Scripts\\python.exe D:\\变声\\tools\\wechat_regression.py"

产物：outputs/wechat_regression.json（每轮的判据明细，便于对比历史）

⚠️ 前置条件（脚本会自检并在不满足时明确告诉你缺哪一条）：
   微信已登录、窗口可见不最小化、没有别的窗口抢焦点、VIRTUAL CABLE 已装。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "m2_server"))

OUT = ROOT / "outputs"          # 注意：cfg.OUTPUTS_DIR 可能被 VM_OUTPUTS_DIR 覆盖
REPORT = OUT / "wechat_regression.json"

# 时长判据的固有开销：LEAD + 播放对齐 + TAIL ≈ 1.2s（§2.12 实测 2.72s→4"、12.24s→14"）
DURATION_OVERHEAD = 1.2
DURATION_TOL = 2.0
# 微信语音最长 60s 自动截断（§2.9）——读到这个值就是坑复发了，不是"差一点"
TRUNCATED_SECS = 60.0

_TERMINAL_HINTS = ("已点", "已发送", "语音已发送")


# ---------------- 纯判据（无 IO，便于单测） ----------------

def parse_voice_secs(name: str | None) -> float | None:
    """从 `语音15"秒` 这类 UIA 名称里取秒数；取不到返回 None。

    微信只给整秒，所以判据必须带容差——别拿浮点相等去比。
    """
    if not name:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*[\"”]?\s*秒", name)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)", name)      # 兜底：只给数字的变体
    return float(m.group(1)) if m else None


def judge_terminal(steps: list[str] | None, outcome: str | None) -> tuple[bool, str]:
    """判据 1：steps 里要有发送终态。"""
    steps = steps or []
    hit = next((s for s in steps if any(h in s for h in _TERMINAL_HINTS)), None)
    if hit:
        return True, f"有终态：{hit}"
    if outcome == "ok":
        return False, f"outcome=ok 但 steps 里没有发送终态（steps={steps}）"
    return False, f"未走完发送流程（outcome={outcome}）"


def judge_duration(audio_secs: float, reported_secs: float | None) -> tuple[bool, str]:
    """判据 2：读到的语音秒数 ≈ 音频 + 固有开销。"""
    if reported_secs is None:
        return False, "读不到语音消息时长（UIA 未就绪或消息不存在）"
    if reported_secs >= TRUNCATED_SECS:
        return False, (f"显示 {reported_secs:.0f}\" —— 60s 截断复发：录音的按下状态"
                       f"没被解除（犯错指南 §2.2）")
    expected = audio_secs + DURATION_OVERHEAD
    if abs(reported_secs - expected) <= DURATION_TOL:
        return True, f"时长 {reported_secs:.0f}\"（音频 {audio_secs:.1f}s，期望 ≈{expected:.1f}s）"
    return False, (f"时长 {reported_secs:.0f}\" 与期望 {expected:.1f}s 偏差 "
                   f"{abs(reported_secs - expected):.1f}s（容差 {DURATION_TOL}s）"
                   f"—— 偏大通常是静音头过长/播放启动被录进去（§2.12）")


def judge_no_overlay(overlay_rect, green_send, *, uia_ready: bool) -> tuple[bool, str]:
    """判据 3：没有挂起的录音浮层（两种检测手段任一发现即失败）。"""
    if overlay_rect is not None:
        return False, f"仍检测到录音浮层 {overlay_rect}（挂起录音，先清场再重试）"
    if green_send is not None:
        return False, f"仍检测到绿色发送钮 {green_send}（挂起录音，先清场再重试）"
    how = "UIA + 像素" if uia_ready else "像素（UIA 未就绪）"
    return True, f"无挂起浮层（{how}）"


def overall(judgements: list[dict]) -> tuple[bool, str]:
    """把多条判据归纳成一句结论（给终端和退出码用）。"""
    bad = [j for j in judgements if not j["ok"]]
    if not bad:
        return True, "链路正常"
    return False, "链路异常：" + "；".join(f"{j['name']} — {j['detail']}" for j in bad)


# ---------------- 测试音频（确定性，不依赖 TTS/GPU） ----------------

def make_test_wav(out_path: Path, secs: float = 3.0, sr: int = 24000) -> Path:
    """合成一段"像说话"的音频：基频 + 谐波 + 4Hz 包络（避免纯正弦的怪响）。

    刻意不用 TTS/RVC：回归要能在任何时刻无 GPU、无模型的情况下跑，
    且时长必须**完全已知**，否则时长判据失去意义（--with-tts 才走真链路）。
    """
    import math
    import struct

    n = int(sr * secs)
    frames = bytearray()
    for i in range(n):
        t = i / sr
        env = 0.6 + 0.4 * math.sin(2 * math.pi * 4.0 * t)          # 4Hz 音节感
        s = (math.sin(2 * math.pi * 180 * t)
             + 0.5 * math.sin(2 * math.pi * 360 * t)
             + 0.25 * math.sin(2 * math.pi * 540 * t))
        v = max(-1.0, min(1.0, 0.28 * env * s))
        frames += struct.pack("<h", int(v * 32767))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return out_path


# ---------------- 真机流程 ----------------

def _env_check() -> tuple[bool, str]:
    """只读前置：微信窗口可见 + 后端 UIA 可用。缺啥直接说缺啥。"""
    import wechat_voice as wv
    try:
        import wechat_uia as u
    except Exception as exc:      # noqa: BLE001
        return False, f"wechat_uia 导入失败：{exc}"

    hwnd = wv._find_wechat_hwnd()
    if not hwnd:
        return False, "找不到微信窗口（微信没开？没登录？）"
    if wv._foreground_wechat() != hwnd:
        return False, "微信不是前台窗口（自动化必须在前台跑，别锁屏/最小化）"
    ready = u.uia_ready() or u.ensure_active()
    return True, f"微信窗口就绪；UIA {'就绪' if ready else '不可用（将走像素降级链路）'}"


def _one_round(wav: Path, audio_secs: float, *, with_tts: bool) -> dict:
    """发一条并跑三条判据。返回本轮明细。"""
    import wechat_voice as wv
    try:
        import wechat_uia as u
    except Exception:      # noqa: BLE001
        u = None

    res = wv._do_send(wv.SendVoiceReq(wav=wav.name))
    steps = list(res.get("steps") or [])
    outcome = res.get("outcome")

    reported = None
    if u is not None:
        try:
            reported = parse_voice_secs(u.latest_voice_message())
        except Exception:      # noqa: BLE001
            reported = None

    overlay = None
    try:
        if u is not None:
            overlay = u.overlay_rect(timeout=0.2)
    except Exception:      # noqa: BLE001
        overlay = None
    green = None
    try:
        rect = wv._window_rect(wv._foreground_wechat())
        green = wv._find_green_send(rect, retries=1)
    except Exception:      # noqa: BLE001
        green = None

    def _j(name: str, verdict: tuple[bool, str]) -> dict:
        return {"name": name, "ok": verdict[0], "detail": verdict[1]}

    judgements = [
        _j("终态", judge_terminal(steps, outcome)),
        _j("时长", judge_duration(audio_secs, reported)),
        _j("清场", judge_no_overlay(overlay, green, uia_ready=u is not None)),
    ]
    ok, summary = overall(judgements)
    return {"wav": wav.name, "audio_s": round(audio_secs, 2), "outcome": outcome,
            "reported_s": reported, "steps": steps, "judgements": judgements,
            "ok": ok, "summary": summary}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="微信语音发送链路自动回归")
    ap.add_argument("--secs", type=float, default=3.0, help="测试音频时长（默认 3s；须 >1s，微信最短 1 秒）")
    ap.add_argument("--repeat", type=int, default=1, help="连发几次（验稳定性，默认 1）")
    ap.add_argument("--dry-run", action="store_true", help="只做前置检查与音频准备，不发消息")
    ap.add_argument("--with-tts", action="store_true",
                    help="用后端 TTS+RVC 生成音频（验全链路；需 GPU，慢，且时长不可精确预知）")
    ap.add_argument("--keep-wav", action="store_true", help="保留测试音频（默认保留，便于人工试听）")
    args = ap.parse_args(argv)

    if args.secs < 1.5:
        print("[前置] 音频时长必须 >1s（微信最短 1 秒，太短直接提示「说话时间太短」不发）")
        return 2

    OUT.mkdir(exist_ok=True)
    print(f"=== 微信发送链路自动回归 · 音频 {args.secs}s × {args.repeat} 轮 ===")

    if args.with_tts:
        print("[音频] 走 TTS+RVC 全链（时长不可精确预知，时长判据放宽为「不大于音频+4s」）")
        import requests
        r = requests.post("http://127.0.0.1:8000/api/tts",
                          json={"text": "大家好，这是我的新声音，你觉得怎么样？",
                                "voice_id": "kangaroo", "rvc_voice": "kangaroo_v2"},
                          timeout=600)
        r.raise_for_status()
        wav = OUT / r.json()["wav"]
    else:
        wav = make_test_wav(OUT / f"regress_{args.secs:g}s.wav", args.secs)
    import wechat_voice as wv
    audio_secs = wv._wav_duration(wav)
    print(f"[音频] {wav.name}（实测 {audio_secs:.2f}s）")

    ok, detail = _env_check()
    print(f"[前置] {'OK' if ok else 'FAIL'} — {detail}")
    if not ok:
        print("\n结果：无法开跑（先满足上面那条前置条件）")
        return 1
    if args.dry_run:
        print("\n--dry-run：前置与音频都就绪，未发送消息")
        return 0

    rounds = []
    for i in range(args.repeat):
        print(f"\n--- 第 {i + 1}/{args.repeat} 轮发送 ---")
        # 每轮开跑前清场：强 UP + 还原样式（§2.11，失败会留下挂起录音污染后续）
        try:
            for _ in range(3):
                wv._send_input_mouse(0x0004)
            wv._exstyle_restore_if_needed()
        except Exception:      # noqa: BLE001
            pass
        r = _one_round(wav, audio_secs, with_tts=args.with_tts)
        rounds.append(r)
        for j in r["judgements"]:
            print(f"  [{'PASS' if j['ok'] else 'FAIL'}] {j['name']} — {j['detail']}")
        if not r["ok"]:
            print(f"  本轮结论：{r['summary']}")

    # 汇总：任一轮失败即整体失败（回归工具不能"平均一下就算过"）
    failed = [i + 1 for i, r in enumerate(rounds) if not r["ok"]]
    passed = not failed
    report = {"ts": time.strftime("%F %T"), "secs": args.secs, "rounds": rounds,
              "passed": passed, "failed_rounds": failed}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")

    print("\n================ 回归汇总 ================")
    print(f"  轮数 {len(rounds)}，通过 {len(rounds) - len(failed)}，失败 {len(failed)}"
          + (f"（第 {failed} 轮）" if failed else ""))
    print(f"  明细：{REPORT}")
    if passed:
        print("  结论：链路正常")
        return 0
    print("  结论：链路异常 —— 先按 docs/犯错指南.md §3.3 的三条判据定位")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
