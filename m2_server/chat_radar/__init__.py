"""chat_radar —— 微信「该不该回」状态雷达（纯像素，不读文字，不碰微信内存）。

三层结构：
    L0 感知  capture.py + layout.py   抓图 → 行投影 → 谁发的 / 几条 / 是不是系统提示
    L1 判断  state.py + judge.py      四个带时间的几何量 → 0-5 的紧急度
    L2 生成  不在本包内               用户点「怎么回」时才需要读文字，交给 qwen-vl-ocr

快速开始：
    cd m2_server
    python -m chat_radar probe          # 看探测到的窗口与聊天区矩形
    python -m chat_radar once           # 抓一帧，打印识别结果 + 判断
    python -m chat_radar watch          # 常驻轮询，状态变化时打印

为什么不用 VLM 读屏做判断：实测 Qwen3-VL-4B 在 117 群上「左右位置 0/4 对、
时间戳 0/2 对」，而像素行投影 11/11 全对、快 33 倍（171ms vs 5.76s）。
VLM 只保留「认字」这一件事，放在 L2。
"""

from .capture import (
    Frame,
    Roi,
    WindowInfo,
    capture_frame,
    enable_dpi_awareness,
    find_wechat_window,
    grab_window,
    locate_chat_roi,
)
from .judge import ACTION_LABEL, Verdict, judge_by_rule, judge_signals
from .layout import Block, Layout, analyze
from .radar import Radar, Tick, watch
from .state import RadarState, Signals

__all__ = [
    "ACTION_LABEL",
    "Block",
    "Frame",
    "Layout",
    "Radar",
    "RadarState",
    "Roi",
    "Signals",
    "Tick",
    "Verdict",
    "WindowInfo",
    "analyze",
    "capture_frame",
    "enable_dpi_awareness",
    "find_wechat_window",
    "grab_window",
    "judge_signals",
    "judge_by_rule",
    "locate_chat_roi",
    "watch",
]
