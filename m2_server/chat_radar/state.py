"""帧间状态跟踪：把每一帧的静态布局，变成「时间」维度的信号。

layout 只知道「现在长什么样」，判断「该不该回」需要的是四个带时间的量：

| 信号 | 含义 | 来源 |
|---|---|---|
| `last_side` | 最后一条聊天消息是谁发的 | 本帧布局 |
| `streak` | 对方末尾连发了几条 | 本帧布局 |
| `gap_seconds` | 那条消息距今多久 | 帧间变化时刻 |
| `my_last_reply_age` | 我上次说话在多久前 | 上一次「右」事件时刻 |

其中后两个**必须跨帧累计**才能得到，这正是本模块存在的理由。

启动时不可能知道「对方三分钟前发的」，因为屏幕上看不出时间。
所以这里区分两种 gap：
    - 精确值：观察到过该条消息「出现」的那一刻 → `gap_lower_bound=False`
    - 下界值：程序启动时它已经在屏幕上了 → `gap_lower_bound=True`，表示「至少这么久」
这个区分很重要：把「至少 3 秒」当成「正好 3 秒」，会让规则在冷启动时误报。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from .layout import LEFT, RIGHT, Layout


@dataclass
class Signals:
    """一帧的判断输入。全部来自像素，不含任何文字。"""

    last_side: str | None = None
    last_kind: str = ""
    streak: int = 0
    gap_seconds: float | None = None
    gap_lower_bound: bool = False
    my_last_reply_age: float | None = None
    tail_meta: str = ""  # 末尾居中块的类型：ts / sys / ""
    changed: bool = False
    stable_frames: int = 0
    n_blocks: int = 0
    elapsed_ms: float = 0.0
    frames_seen: int = 0

    def as_dict(self) -> dict:
        return {
            "last_side": self.last_side,
            "last_kind": self.last_kind,
            "streak": self.streak,
            "gap_seconds": None if self.gap_seconds is None else round(self.gap_seconds, 1),
            "gap_lower_bound": self.gap_lower_bound,
            "my_last_reply_age": (
                None if self.my_last_reply_age is None else round(self.my_last_reply_age, 1)
            ),
            "tail_meta": self.tail_meta,
            "changed": self.changed,
            "stable_frames": self.stable_frames,
            "n_blocks": self.n_blocks,
            "frames_seen": self.frames_seen,
        }


def _band_fingerprint(frame_image: np.ndarray, roi, frac: float = 0.3) -> np.ndarray:
    """取聊天区底部条带的降采样灰度图，用作「内容变没变」的指纹。

    为什么用像素差分而不是块签名：块签名只看宽高，同一人发「哈哈」→「呵呵」
    宽高完全一样，签名会漏判。像素差分是 5ms 的事，且 100% 不漏。

    条带只取 ROI 内部（不含输入框），所以光标闪烁、输入法候选框都不会误触发。
    """
    sub = roi.crop(frame_image)
    h = sub.shape[0]
    top = max(0, h - int(h * frac))
    band = sub[top:, :, :3].sum(axis=2, dtype=np.uint16) // 3  # 整数灰度，避免 float
    # 4x4 均值池化：抗 1px 抖动，同时把比较成本压到 O(1)
    bh, bw = band.shape[0] // 4 * 4, band.shape[1] // 4 * 4
    if bh == 0 or bw == 0:
        return band.astype(np.int16)
    small = band[:bh, :bw].reshape(bh // 4, 4, bw // 4, 4).mean(axis=(1, 3))
    return small.astype(np.int16)


#: 指纹差异阈值：平均绝对差 > 1.2 灰度级算「变了」。
#: 实测静止画面抖动 < 0.3，新增一条消息 > 8，取 1.2 有足够余量。
CHANGE_THRESHOLD = 1.2

#: 连续多少帧不变才算「稳定」，避免抓到动画中间态。
STABLE_FRAMES = 2


@dataclass
class RadarState:
    """常驻状态机。每次轮询调用一次 `feed()`。"""

    change_threshold: float = CHANGE_THRESHOLD
    _prev_fp: np.ndarray | None = field(default=None, repr=False)
    _frames: int = 0
    _stable: int = 0
    _last_event_ts: float | None = None
    _last_event_side: str | None = None
    _last_right_ts: float | None = None
    _observed_since: float | None = None  # 当前末尾状态首次被观察到的时刻
    _observed_side: str | None = None

    def reset(self) -> None:
        self._prev_fp = None
        self._frames = 0
        self._stable = 0
        self._last_event_ts = None
        self._last_event_side = None
        self._last_right_ts = None
        self._observed_since = None
        self._observed_side = None

    def feed(
        self,
        frame_image: np.ndarray,
        roi,
        layout: Layout,
        now: float | None = None,
    ) -> Signals:
        now = time.time() if now is None else now
        self._frames += 1

        # ---- 1. 变没变 ----
        fp = _band_fingerprint(frame_image, roi)
        changed = False
        if self._prev_fp is not None and self._prev_fp.shape == fp.shape:
            changed = float(np.abs(fp - self._prev_fp).mean()) > self.change_threshold
        self._prev_fp = fp
        self._stable = 0 if changed else self._stable + 1

        last = layout.last_chat
        side = last.side if last else None

        # ---- 2. 变化时记录事件时刻 ----
        # 只有真的观察到「画面变了」才更新事件时刻。
        # 早先版本写成 `changed or _last_event_ts is None`，结果冷启动第 2 帧就把
        # 「首次观察时刻」当成了「消息出现时刻」，gap 永远从 0 开始算，
        # 于是一条已经放了 10 分钟的消息会被判成「刚发，再等等」——静默漏报。
        if changed:
            self._last_event_ts = now
            self._last_event_side = side
            if side == RIGHT:
                self._last_right_ts = now

        # ---- 3. 记录「该状态从何时开始被观察」 ----
        if side != self._observed_side:
            self._observed_side = side
            self._observed_since = now

        # ---- 4. 组装信号 ----
        gap: float | None = None
        lower_bound = False
        if side is not None:
            if self._last_event_side == side and self._last_event_ts is not None and not changed:
                gap = now - self._last_event_ts
            elif self._observed_since is not None:
                # 冷启动：屏幕上已经存在，只能给出「至少」
                gap = now - self._observed_since
                lower_bound = True

        streak = 0
        for b in reversed(layout.chat_blocks):
            if b.side == LEFT:
                streak += 1
            else:
                break

        tail_meta = ""
        for b in reversed(layout.blocks):
            if b.side == "center":
                tail_meta = b.kind
                break
            break

        return Signals(
            last_side=side,
            last_kind=last.kind if last else "",
            streak=streak,
            gap_seconds=gap,
            gap_lower_bound=lower_bound,
            my_last_reply_age=(None if self._last_right_ts is None else now - self._last_right_ts),
            tail_meta=tail_meta,
            changed=changed,
            stable_frames=self._stable,
            n_blocks=len(layout.blocks),
            elapsed_ms=layout.elapsed_ms,
            frames_seen=self._frames,
        )
