"""聊天区布局解析：把像素变成「谁发的、是什么类型」的消息块。

不用 OCR、不用模型、不碰微信内存，纯 numpy。实测 117 群 11/11 全对（171ms）。

原理
----
微信 4.x 聊天区的消息块在水平方向上有很强的规律：
    左消息：[头像]  gap≈22px  [昵称/气泡]     → 内容整体靠左
    右消息：[气泡]  [头像]                    → 内容整体靠右
    居中：  时间戳、系统提示（"XXX领取了你的红包"）

所以对聊天区做「行投影」切成行段，每段再求 x 范围，就能判方向。

三个必须踩对的细节
------------------
1. **亮度阈值不能设太低。** 用 140 时，`10:51` 这类时间戳和灰色系统提示
   （亮度 160-190）全部漏检；放到 205 才全部检出。这两项恰好是判「隔了多久」
   和「没人等你回」的唯一依据，漏了整套判断就废。
2. **x 范围不能用 min/max。** 左侧边缘的孤立噪声点会把中心拉偏，把时间戳
   误判成「左」。要取**最长连续列段**。
3. **列方向要合并头像和文字。** 头像（x≈33）与文字（x≈108）之间空隙 22px，
   间隙阈值设 12 会被拆成两块，streak（对方连发几条）就会翻倍。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .capture import Roi

#: 前景阈值：比「聊天区背景」暗多少（0-765 的 3 倍灰度刻度）才算有东西。
#: 用相对背景的差值而不是绝对亮度，这样深色主题、自定义聊天背景都不用改代码。
#: 实测浅色主题背景 750（250×3）：文字 180、浅灰时间戳 516、绿气泡 490 全部命中。
FG_DROP = 120

#: 深色主题判定：背景 3 倍灰度低于这个值就认为背景是暗的，前景反转为「比背景亮」。
DARK_BG = 380

#: 行段之间的最小空白（px），超过就断开。文字行间距约 6-8px，取 5 不会拆散多行正文。
ROW_GAP = 5

#: 列方向合并间隙（px）。头像与文字之间 22px，取 36 可合并又不至于跨到另一侧。
COL_GAP = 36

#: 消息块最小高度/宽度（px），低于此值当噪声丢弃。
MIN_H = 8
MIN_W = 6

#: 相邻同向块合并的最大垂直间距（px）。多行正文换行时会被行投影拆开，需要合回去。
MERGE_GAP = 26

#: 方向判定的双阈值：看 x0 是否贴左、x1 是否贴右，而不是只看中心点。
#: 只看中心会被「居中的长系统提示」和「短右消息」同时骗到。
EDGE_LEFT = 0.25
EDGE_RIGHT = 0.75

LEFT, RIGHT, CENTER = "left", "right", "center"

#: 居中块的细分：时间戳/系统提示都居中，靠宽度区分。
#: "10:51" 实测 41-44px；"李河璋领取了你的红包" 实测 143px。取 90 为界。
TS_MAX_W = 90


@dataclass
class Block:
    """一条（可能是多行的）消息块。坐标相对聊天区 ROI。"""

    y0: int
    y1: int
    x0: int
    x1: int
    side: str  # left / right / center
    kind: str = ""  # text / media / ts / sys / unknown
    fill: float = 0.0  # 前景填充率 0-1，实心大块多为图片/视频
    mean_l: float = 0.0  # 前景平均亮度，浅灰=时间戳/系统提示
    lines: int = 1  # 合并了几个月行段

    @property
    def height(self) -> int:
        return self.y1 - self.y0 + 1

    @property
    def width(self) -> int:
        return self.x1 - self.x0 + 1

    def __repr__(self) -> str:  # 便于 CLI 表格输出
        return (
            f"Block({self.kind:<7} {self.side:<6} "
            f"y={self.y0}-{self.y1} x={self.x0}-{self.x1} "
            f"w={self.width} L={self.mean_l:.0f})"
        )


@dataclass
class Layout:
    """一帧聊天区的解析结果。"""

    blocks: list[Block] = field(default_factory=list)
    roi_w: int = 0
    roi_h: int = 0
    elapsed_ms: float = 0.0

    @property
    def chat_blocks(self) -> list[Block]:
        """只保留真正的聊天消息（去掉时间戳与系统提示）。"""
        return [b for b in self.blocks if b.side in (LEFT, RIGHT)]

    @property
    def last_chat(self) -> Block | None:
        return self.chat_blocks[-1] if self.chat_blocks else None

    @property
    def tail_centered(self) -> list[Block]:
        """末尾连续的居中块（时间戳/系统提示）——用来回答「最后一条是不是系统消息」。"""
        out: list[Block] = []
        for b in reversed(self.blocks):
            if b.side == CENTER:
                out.append(b)
            else:
                break
        return out


def _longest_run(mask: np.ndarray, max_gap: int) -> tuple[int, int] | None:
    """mask 里最长的连续段（允许 max_gap 的间隙），返回 (起, 止) 闭区间。

    向量化实现：一次性找所有断裂点，再比长度。逐元素循环在这上面会慢 4-5 倍。
    """
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return None
    if idx.size == 1:
        return int(idx[0]), int(idx[0])
    breaks = np.flatnonzero(np.diff(idx) > max_gap)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [idx.size - 1]))
    lens = idx[ends] - idx[starts]
    k = int(lens.argmax())
    return int(idx[starts[k]]), int(idx[ends[k]])


def _row_runs(rowcnt: np.ndarray, min_count: int = 3, gap: int = ROW_GAP) -> list[tuple[int, int]]:
    """行投影 → 连续行段。"""
    active = rowcnt >= min_count
    runs: list[tuple[int, int]] = []
    i, n = 0, active.size
    while i < n:
        if not active[i]:
            i += 1
            continue
        j = i
        last_on = i
        while j < n:
            if active[j]:
                last_on = j
                j += 1
            else:
                # 允许 gap 个空行；超过则收段
                k = j
                while k < n and not active[k] and k - j < gap:
                    k += 1
                if k < n and active[k]:
                    j = k
                else:
                    break
        runs.append((i, last_on))
        i = last_on + 1
    return runs


def _classify_side(x0: int, x1: int, w: int) -> str:
    """双阈值判方向：贴左 / 贴右 / 居中。"""
    l_ratio = x0 / w
    r_ratio = x1 / w
    if l_ratio < EDGE_LEFT and r_ratio < EDGE_RIGHT:
        return LEFT
    if r_ratio > EDGE_RIGHT and l_ratio > EDGE_LEFT:
        return RIGHT
    return CENTER


def _classify_kind(block: Block, roi_w: int) -> str:
    if block.side == CENTER:
        # 时间戳：窄、浅灰、单行；系统提示：宽、浅灰、可能带小图标
        if block.width <= TS_MAX_W:
            return "ts"
        return "sys"
    # 实心大块 = 图片/视频：实测视频缩略图 fill=97.5%、高 234px；
    # 文字行 fill 只有 10-14%（笔画之间大量空隙），所以阈值设在 0.9 且要求高度，
    # 否则「彩色气泡整块进前景」的新判据会把长文字泡误标成 media。
    if block.fill > 0.9 and block.height > 60:
        return "media"
    return "text"


def analyze(arr: np.ndarray, roi: Roi) -> Layout:
    """解析聊天区，返回消息块列表。arr 是整窗 RGB，roi 是聊天区矩形。"""
    import time

    t0 = time.perf_counter()

    # 全程整数运算，且只做一次 axis=2 归约。
    #
    # 两次改写的原因（都是实测出来的）：
    #   ① 最早用 `astype(np.int16)` + `mean(axis=2)`，单帧 210ms；
    #   ② 改成 `sum(axis=2,uint16)` + `max/min(axis=2)` 求饱和度，仍要 117ms，
    #      因为 numpy 的 uint8 max/min 归约是逐列比较，2.78M 像素要 85ms。
    #   ③ 最终：**不再排彩色**，改用「相对背景的亮度差」当唯一判据。
    #      彩色气泡（绿/蓝/红包）整块进入前景，方向判得比只框文字更准，
    #      而且省掉 85ms。这是「更快」和「更准」同时改善的一次改写。
    sub = roi.crop(arr)[:, :, :3]
    H, W = sub.shape[:2]

    s3 = sub.sum(axis=2, dtype=np.uint16)  # 3 × 灰度，范围 0-765
    # 用直方图众数估计背景：背景总是面积最大的那个色阶，比中位数更抗「大图占屏」
    hist = np.bincount(s3.ravel(), minlength=766)
    bg3 = int(np.argmax(hist))
    # 深色主题前景比背景亮，浅色主题前景比背景暗
    fg = s3 > bg3 + FG_DROP if bg3 < DARK_BG else s3 < bg3 - FG_DROP

    rowcnt = fg.sum(axis=1)
    runs = _row_runs(rowcnt)
    runs = [(s, e) for s, e in runs if e - s + 1 >= MIN_H]

    raw: list[Block] = []
    for s, e in runs:
        band = fg[s : e + 1, :]
        seg = _longest_run(band.any(axis=0), COL_GAP)
        if seg is None:
            continue
        x0, x1 = seg
        if x1 - x0 + 1 < MIN_W:
            continue
        side = _classify_side(x0, x1, W)
        cell = fg[s : e + 1, x0 : x1 + 1]
        # 前景像素的平均灰度：s3 存的是 RGB 之和，除以 3 还原成 0-255
        mean_l = (float(s3[s : e + 1, x0 : x1 + 1][cell].mean()) / 3.0) if cell.any() else 0.0
        b = Block(
            y0=int(s),
            y1=int(e),
            x0=int(x0),
            x1=int(x1),
            side=side,
            fill=float(cell.mean()) if cell.size else 0.0,
            mean_l=mean_l,
        )
        raw.append(b)

    # 合并被行投影拆开的多行消息：同向（非居中）且垂直间距小
    merged: list[Block] = []
    for b in raw:
        if (
            merged
            and b.side == merged[-1].side
            and b.side in (LEFT, RIGHT)
            and b.y0 - merged[-1].y1 <= MERGE_GAP
        ):
            m = merged[-1]
            m.y1 = b.y1
            m.x0 = min(m.x0, b.x0)
            m.x1 = max(m.x1, b.x1)
            m.fill = max(m.fill, b.fill)
            m.mean_l = b.mean_l
            m.lines += 1
            # 合并后方向可能变化（如头像+长文），重算
            m.side = _classify_side(m.x0, m.x1, W)
            continue
        merged.append(b)

    for b in merged:
        b.kind = _classify_kind(b, W)

    return Layout(
        blocks=merged,
        roi_w=W,
        roi_h=H,
        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
    )
