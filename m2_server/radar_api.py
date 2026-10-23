"""微信「该不该回」状态雷达的 API 面（chat_radar 的 HTTP 壳）。

为什么只是一层薄壳
------------------
感知（像素行投影）与判断（规则引擎）全部在 `chat_radar/` 包里，且刻意**不依赖
FastAPI**（CLI、桌面角标、测试都能直接用）。本模块只做三件事：

    ① 持有一个进程级 `Radar` 单例（懒加载 + 锁）——桌面端可能开多个页面同时
       轮询，但 GDI 抓图这一步必须串行，否则两个 tick 互相抢窗口句柄没有意义；
    ② 把 `Tick`（信号 + 结论）序列化成 JSON；
    ③ 观察失败不当 500：微信没开 / 最小化是**正常状态**而不是服务坏了，
       返回 200 + `ok:false` + verdict=unknown，前端据此显示「观察中」。
       真正的服务器错误（如序列化炸了）才用 500。

为什么「该不该回」不给前端自由发挥的空间
----------------------------------------
`verdict.action` 是后端判定的枚举（none/wait/soon/now/unknown），前端只做展示。
判断层的规则与文案都钉在 chat_radar.judge 里 —— 桌宠改文案不改语义。

轮询建议：`poll_hint_ms` 由后端给出（1500ms）。理由在 radar.py 的模块注释：
500ms 轮询是「不漏正在输入」的上限，但桌面角标场景 1.5s 足够且 CPU 减半；
单次 tick 实测 ~100ms（p95 121ms），两个页面同时轮询也不会互相饿死。
"""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/radar", tags=["radar"])

#: 前端轮询间隔建议。写进响应而不是让前端写死：以后 L0 优化了或加了推送，
#: 只改这里一处，老前端自动跟上。
POLL_HINT_MS = 1500

#: 单例与锁。懒加载：import 本模块（含测试收集期）绝不去碰 GDI / user32。
#: RLock 而不是 Lock：`_tick_serialized()` 持锁调 `_radar()`，后者要再拿同一把锁
#: 才能建单例 —— 不可重入锁会当场死锁（probe/reset 两处同理）。
_RADAR = None
_RADAR_LOCK = threading.RLock()


def _radar():
    """进程级单例。首次调用才 import chat_radar —— 失败的信息原样抛给调用方。"""
    global _RADAR
    if _RADAR is None:
        with _RADAR_LOCK:
            if _RADAR is None:
                from chat_radar import Radar  # 惰性：CLI/测试收集期不付 GDI 成本

                _RADAR = Radar()
    return _RADAR


def reset_singleton() -> None:
    """丢弃单例 —— 只给测试用（用例之间必须各自拿到干净的 Radar）。"""
    global _RADAR
    with _RADAR_LOCK:
        _RADAR = None


def _tick_serialized() -> dict:
    """串行跑一次 tick 并序列化。锁住整个 tick：抓图 ~100ms，串行足够。"""
    start = time.perf_counter()
    with _RADAR_LOCK:
        tick = _radar().tick()
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    out: dict = {
        "ok": tick.ok,
        "verdict": tick.verdict.as_dict(),
        "signals": tick.signals.as_dict(),
        "elapsed_ms": elapsed_ms,
        "poll_hint_ms": POLL_HINT_MS,
    }
    if not tick.ok:
        out["error"] = tick.error
    return out


@router.get("/state")
def state():
    """抓一帧、算一次「该不该回」。微信没开 / 最小化时返回 `ok:false` + 人话原因。"""
    try:
        return _tick_serialized()
    except Exception as exc:  # noqa: BLE001 —— 单例构建/序列化的意外才到这
        raise HTTPException(status_code=500, detail=f"雷达状态计算失败：{exc}") from exc


@router.post("/reset")
def reset():
    """清空观察状态（换会话 / 想重新观察时用）。下次 /state 从冷启动重新累计。"""
    with _RADAR_LOCK:
        _radar().reset()
    return {"ok": True}


@router.get("/probe")
def probe():
    """诊断：窗口矩形 + 聊天区 ROI + 最近一次识别出的块数。**不推进观察状态。**

    为什么不用 `tick()`（2026-10-23 修正）：tick 会喂状态机 —— 推进 `frames_seen`、
    覆盖上一帧指纹、还可能重置 `_stable` 与 `_observed_since`（冷启动的
    `gap_lower_bound` 就靠后者算）。结果就是「一诊断，gap 就从 0 重算」，
    排查问题时反而把现场搅乱。这里改走 `Radar.peek()`：抓图 + 解析，不喂状态机。
    """
    start = time.perf_counter()
    with _RADAR_LOCK:
        frame, layout, err = _radar().peek()
    elapsed_ms = round((time.perf_counter() - start) * 1000, 1)
    out: dict = {"ok": err == "", "elapsed_ms": elapsed_ms}
    if err:
        out["error"] = err
        return out
    assert frame is not None and layout is not None  # err 为空蕴含两者非空
    win, roi = frame.window, frame.roi
    out["window"] = {"title": win.title, "rect": list(win.rect)}
    out["roi"] = [roi.left, roi.top, roi.right, roi.bottom]
    out["n_blocks"] = len(layout.blocks)
    return out
