"""编排层：一次 tick = 抓图 → 解析 → 更新状态 → 判断。

对外只暴露一个 `Radar.tick()`，返回 `Tick`（信号 + 结论 + 原始帧），
方便接到 CLI、桌面角标、FastAPI 路由或 Electron 前端。

轮询间隔：**没有唯一的正确值，取决于谁在看**。
    - 端到端单帧实测 ~97ms（抓图 57 + 解析 30 + 状态 8，p95 121ms），
      与旧文档写的「解析 20-40ms」不同 —— 那只算了 analyze，没算 GDI 抓图。
    - 桌宠面板角标：后端建议 1500ms（`radar_api.POLL_HINT_MS`）。角标不需要
      亚秒级反应，而 1.5s 能把常驻 CPU 压到 500ms 轮询的一半以下。
    - CLI 常驻观察：500ms 起，看 gap 的秒数变化用。
    - 「该不该回」不靠高频：判断依据是 gap/连发/方向，几秒粒度足够；
      真正的下限由「对方正在输入」的可见性决定，500ms 已远超需要。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from . import capture as cap
from . import judge as jd
from . import layout as ly
from . import state as st


@dataclass
class Tick:
    signals: st.Signals
    verdict: jd.Verdict
    frame: cap.Frame | None
    layout: ly.Layout | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.error == ""


class Radar:
    """微信「该不该回」状态雷达。"""

    def __init__(self, use_llm: bool = False, debug: bool = False) -> None:
        self.use_llm = use_llm
        self.debug = debug
        self._state = st.RadarState()
        self._window: cap.WindowInfo | None = None
        self._roi: cap.Roi | None = None
        self._roi_window_rect: tuple[int, int, int, int] | None = None
        self._last_verdict_key: tuple | None = None

    # ------------------------------------------------------------ 内部

    def _ensure_window(self) -> cap.WindowInfo | None:
        """窗口句柄失效（微信重启）时重新找。"""
        w = self._window
        if w is not None:
            import ctypes

            if ctypes.windll.user32.IsWindow(w.hwnd):
                return w
            self._window = None
            self._roi = None
            self._roi_window_rect = None
        self._window = cap.find_wechat_window()
        return self._window

    def _grab_with_roi(self) -> tuple[cap.Frame | None, str]:
        win = self._ensure_window()
        if win is None:
            return None, "没找到微信窗口（微信是否在运行？）"

        img = cap.grab_window(win.hwnd)
        if img is None or img.size == 0:
            return None, "抓取窗口失败（PrintWindow 返回空）"
        if float(img.mean()) < 3.0:
            return None, "抓到全黑（微信可能已最小化；最小化时 PrintWindow 无效）"

        # ROI 只在窗口尺寸变化时重算：切会话时背景可能变，按尺寸缓存更稳
        if self._roi is None or self._roi_window_rect != win.rect:
            roi = cap.locate_chat_roi(img, debug=self.debug)
            if roi is None:
                return None, "聊天区 ROI 探测失败"
            self._roi = roi
            self._roi_window_rect = win.rect

        return cap.Frame(window=win, image=img, roi=self._roi), ""

    # ------------------------------------------------------------ 对外

    def tick(self) -> Tick:
        frame, err = self._grab_with_roi()
        if frame is None:
            sig = st.Signals()
            return Tick(sig, jd.Verdict(jd.UNKNOWN, 0, err), None, None, error=err)

        lay = ly.analyze(frame.image, frame.roi)
        sig = self._state.feed(frame.image, frame.roi, lay)
        verdict = jd.judge_signals(sig, use_llm=self.use_llm)
        return Tick(sig, verdict, frame, lay)

    def peek(self) -> tuple[cap.Frame | None, ly.Layout | None, str]:
        """抓一帧并解析，但**不喂状态机** —— 诊断用的「看一眼」不等于「观察一次」。

        为什么必须分开：`/probe` 这类接口如果复用 `tick()`，一次诊断就会推进
        `frames_seen`、覆盖上一帧指纹、并可能重置 `_stable` 与 `_observed_since`
        （后者直接决定冷启动的 `gap_lower_bound`）。也就是说**观测行为会改变被观测的东西**，
        排查「为什么 gap 不动」时反而把它搅乱。

        返回 `(frame, layout, error)`；失败时前两项为 None、第三项是人话原因。
        """
        frame, err = self._grab_with_roi()
        if frame is None:
            return None, None, err
        return frame, ly.analyze(frame.image, frame.roi), ""

    def invalidate_roi(self) -> None:
        self._roi = None
        self._roi_window_rect = None

    def reset(self) -> None:
        self._state.reset()
        self.invalidate_roi()
        self._last_verdict_key = None


def watch(
    interval: float = 0.5,
    use_llm: bool = False,
    debug: bool = False,
    on_change: Callable[[Tick], None] | None = None,
    max_seconds: float | None = None,
) -> None:
    """常驻轮询。结论或信号变化时才回调，避免刷屏。"""
    radar = Radar(use_llm=use_llm, debug=debug)
    t_start = time.time()
    last_key = None
    while True:
        tick = radar.tick()
        s, v = tick.signals, tick.verdict
        key = (v.action, s.last_side, s.streak, s.tail_meta, s.gap_lower_bound)
        if key != last_key:
            last_key = key
            if on_change is not None:
                on_change(tick)
        if max_seconds is not None and time.time() - t_start >= max_seconds:
            return
        time.sleep(interval)
