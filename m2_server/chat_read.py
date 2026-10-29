"""当前微信会话的只读视图 + 本地规则分析（桌宠「分析聊天」按钮的后端）。

为什么是 UIA 不是截图 OCR
--------------------------
`wechat_uia` 把微信 4.x 的 Qt accessibility gate 打开后，聊天区就是一个
`list "消息"`，每条消息是 ListItem，Name 就是正文：

    list-item "23:21"        ← 时间分隔行
    list-item "语音4\"秒"     ← 语音（带真实时长）
    list-item "图片"          ← 图片/文件类
    list-item "偷偷吃一"      ← 普通文字

读一次 ~50ms，不占显存、不做 OCR。而 Qwen3-VL-4B 读整屏要 3.4s（GPU）/
8.4s（CPU），且本机实测它判断「谁说的 / 几点」准确率极低 —— 这两类事实
信息正是 UIA 的强项（见 `docs/交接-AI必读.md` 的分层）。

只读边界
--------
不点击、不切会话、不发消息。**默认不滚动**：只读「当前已打开会话」已经渲染
出来的消息（UIA 只暴露可见 ListItem），所以它是「看一眼」，不是「拉完整历史」。

要往上多读几屏时走 `read_chat(..., screens=N)`（2026-10-29 新增）：在聊天区发
真实滚轮事件、边滚边收集、读完**滚回底部**并还原鼠标。它仍然是「只读」，但已经
不是「零副作用」—— 会短暂动一下鼠标和滚动位置，所以必须由用户显式选择，不进轮询。
发言人归属拿不到（ListItem 只有气泡内容），交给雷达的 last_side/streak；
群聊里的 @ 以正文形式出现，按文本提示。
"""

from __future__ import annotations

import ctypes
import logging
import re
import time
from ctypes import wintypes

import wechat_uia

logger = logging.getLogger(__name__)

#: 微信的 UIA 树在窗口控件的第 16 层才出现消息列表（2026-10-23 实测 4.1.13.12），
#: 给到 30 层留余量；节点数封顶防止异常树把接口拖死。
MAX_DEPTH = 30
MAX_NODES = 6000

#: 找不到列表时再走一遍的次数（第 2 次会**强制重开 UIA gate**，见 `_locate_lists`）。
_WALK_ATTEMPTS = 2
_WALK_RETRY_DELAY_S = 0.15

# ---------------------------------------------------------------- 深读（滚动补齐）
#
# 为什么需要自己发滚轮：消息列表是 `mmui::RecyclerListView`（虚拟列表），
# `GetPattern(ScrollPattern)` 返回 False —— 没有 UIA 滚动接口可用，只能发真实滚轮
# （2026-10-29 在 4.2.0.11 实测）。
MAX_SCREENS = 5              # 最多往上多读 5 屏（再多用户也读不完，且每屏要 ~1s）
NOTCHES_PER_SCREEN = 3       # 一「屏」≈ 3 格滚轮（实测 2 格就换掉一半可见消息）
WHEEL_DELTA = 120
MOUSEEVENTF_WHEEL = 0x0800
MAX_DEEP = 80                # 深读最多回传多少条（提示词与面板都吃不消更多）
_SCROLL_SETTLE_S = 0.3       # 滚完等 Qt 重排/物化新 ListItem


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _cursor_pos() -> tuple[int, int]:
    pt = _POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def _window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def _scroll_chat(ctrl, up: bool, notches: int = NOTCHES_PER_SCREEN) -> bool:
    """在聊天区发真实滚轮事件；成功返回 True。

    ⚠️ 护栏（2026-10-29 定，别删）：滚轮事件送给的是**光标下的窗口**，不是前台
    窗口。微信一旦被别的窗口盖住，这一下就会滚到用户正在读的浏览器/编辑器上 ——
    这不是理论风险，是本项目「自动操作桌面」最容易伤到用户的一类事故。
    所以先用 `WindowFromPoint` 确认光标下确实是微信进程的窗口，否则直接放弃。

    滚完把光标放回原处：用户没有要求「鼠标跳到聊天区」，那只是我们的实现细节。
    """
    box = wechat_uia.rect_of(ctrl)
    if not box:
        return False
    x = (box[0] + box[2]) // 2
    y = (box[1] + box[3]) // 2
    u = ctypes.windll.user32
    u.WindowFromPoint.restype = wintypes.HWND
    u.WindowFromPoint.argtypes = [_POINT]
    hwnd = int(u.WindowFromPoint(_POINT(x, y)) or 0)
    _hwnd, wx_pid = wechat_uia._wechat_hwnd_pid()
    if not hwnd or not wx_pid or _window_pid(hwnd) != int(wx_pid):
        return False
    saved = _cursor_pos()
    u.SetCursorPos(x, y)
    try:
        delta = WHEEL_DELTA if up else -WHEEL_DELTA
        for _ in range(max(1, int(notches))):
            u.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
            time.sleep(0.05)
    finally:
        u.SetCursorPos(saved[0], saved[1])
    return True


def _msg_key(m: dict) -> tuple[str, str, str]:
    return (str(m.get("kind") or ""), str(m.get("text") or ""), str(m.get("side") or ""))


def merge_history(batches: list[list[dict]]) -> list[dict]:
    """把「一屏一批」的消息拼成一条时间线；`batches[0]` 是最新那一屏。

    相邻两批**必然重叠**（往上滚一屏，屏底还留着几条上一屏的消息），靠
    「旧批的后缀 == 新批的前缀」对齐。对不上就整批接上 —— 宁可多几条重复，
    也不能把用户的话吞掉。返回仍是**旧 → 新**（与 `parse_messages` 一致）。
    """
    if not batches:
        return []
    acc = list(batches[0])
    for older in batches[1:]:
        older = list(older)
        if not older:
            continue
        acc_keys = [_msg_key(m) for m in acc]
        overlap = 0
        for k in range(min(len(older), len(acc_keys)), 0, -1):
            if [_msg_key(m) for m in older[-k:]] == acc_keys[:k]:
                overlap = k
                break
        acc = older[: len(older) - overlap] + acc
    return acc

#: 聊天区那份列表叫「消息」，左侧会话列表叫「会话」——同名会取错。
LIST_CHAT = "消息"
LIST_SESSIONS = "会话"

#: 时间分隔行：23:21 / 昨天 23:32 / 星期一 09:10 / 7/30 / 2026年9月1日 …
_TIME_RE = re.compile(
    r"^(?:\d{1,2}:\d{2}"
    r"|昨天(?:\s*\d{1,2}:\d{2})?"
    r"|前天(?:\s*\d{1,2}:\d{2})?"
    r"|(?:星期|周)[一二三四五六日天](?:\s*\d{1,2}:\d{2})?"
    r"|\d{1,2}/\d{1,2}(?:\s*\d{1,2}:\d{2})?"
    r"|\d{4}年\d{1,2}月\d{1,2}日(?:\s*\d{1,2}:\d{2})?)$"
)

#: 语音消息名：`语音4"秒`（微信 4.x 的实际写法，引号是半角直引号；
#: 兼容全角/双引号与省略「秒」的写法）。
_VOICE_RE = re.compile(r'^语音\s*(\d+(?:\.\d+)?)\s*["″“”]?\s*秒?$')

_MEDIA_LABELS = (
    "图片", "视频", "文件", "链接", "位置", "名片",
    "动画表情", "转账", "红包", "语音通话", "视频通话",
)

_SYSTEM_HINTS = ("撤回", "拒收", "已添加", "开启了好友验证", "加入了群聊", "退出了群聊")

#: 会话列表里的未读标记：`[3条]`。注意 `[语音] 6"` 不是未读，必须是「数字+条」。
_UNREAD_RE = re.compile(r"^\[(\d+)条\]$")

#: 问句判据：句尾问号，或以疑问词开头（微信里口语提问常常不带问号）。
_Q_TAIL = re.compile(r"[?？]\s*$")
_Q_HEAD = re.compile(
    r"^(?:为什么|咋|怎么|啥|什么|哪|谁|几点|多久|是不是|能不能|可不可以|要不要|有没有|在吗|人呢|你[在看]?)"
)


def message_kind(name: str) -> str:
    """一条 ListItem 名称 → `time` / `voice` / `media` / `system` / `text`。"""
    if _TIME_RE.match(name):
        return "time"
    if _VOICE_RE.match(name):
        return "voice"
    for label in _MEDIA_LABELS:
        if name == label or name.startswith("[" + label + "]"):
            return "media"
    if any(h in name for h in _SYSTEM_HINTS):
        return "system"
    return "text"


def parse_messages(
    names: list[str],
    limit: int = 40,
    sides: list[str] | None = None,
    speakers: list[str] | None = None,
    speaker_ids: list[int] | None = None,
) -> list[dict]:
    """UIA ListItem 名称 → 消息 dict，保留**末尾** limit 条（含时间行）。

    保留末尾而不是开头：用户在聊的永远是最近几条，往上翻要靠滚动（改微信状态），
    不在只读边界内。

    `sides` 与 `names` **逐位对齐**（多一个是给不出 side 的降级路径）；给了就带上
    `side` 字段 —— 回复建议靠它区分「我」和「对方」，没有这个标记时模型会把群友
    之间的话也当成对自己说的（2026-10-24 评测里 9 组踩了 2 组）。

    `speakers` 与 `names` 逐位对齐，是**群聊里的发言人昵称**（来自 `label_names`
    的昵称条识别）。它和 `side` 是两个维度：`side` 只分「我 / 非我」，`speakers`
    才分得清群友之间谁是谁。认不出的位置是空串，**不写占位符** —— 前端要能区分
    「没识别出来」和「名字就叫未知」。
    """
    items: list[dict] = []
    pairs = list(zip(names, sides, strict=False)) if sides is not None and len(sides) == len(names) else None
    spk = speakers if speakers is not None and len(speakers) == len(names) else None
    spk_id = speaker_ids if speaker_ids is not None and len(speaker_ids) == len(names) else None
    for idx, raw in enumerate(names):
        name = (raw or "").strip()
        if not name:
            continue
        kind = message_kind(name)
        item: dict = {"kind": kind, "text": name}
        if kind == "voice":
            m = _VOICE_RE.match(name)
            if m:
                item["seconds"] = float(m.group(1))
        side = pairs[idx][1] if pairs is not None else ""
        if side:
            item["side"] = side
        sid = spk_id[idx] if spk_id is not None else 0
        if sid:
            item["speaker"] = sid
        who = spk[idx] if spk is not None else ""
        if who:
            item["name"] = who
        items.append(item)
    if limit > 0:
        items = items[-limit:]
    return items


def parse_sessions(names: list[str], limit: int = 8) -> list[dict]:
    """左侧会话列表 → 每条会话的标题 / 未读数 / 预览 / 时间。

    实测名称是多行拼接（4.1.13.12）：

        117\\n[3条] \\n李河璋: 偷偷吃一\\n23:21
        i志愿\\n已置顶\\n[1条] \\n2026年…\\n昨天 08:58\\n消息免打扰

    「已置顶 / 消息免打扰」是状态标记不是内容，先剔掉再按位置取。
    2026-10-24：这两个标记不再只是丢弃 —— `muted` / `pinned` 现在是显式字段，
    自动托管必须知道「这个会话免打扰」（免打扰的群不该替用户回）。
    """
    out: list[dict] = []
    for raw in names:
        lines = [x.strip() for x in (raw or "").splitlines() if x.strip()]
        if not lines:
            continue
        title = lines[0]
        muted = "消息免打扰" in lines
        pinned = "已置顶" in lines
        rest = [x for x in lines[1:] if x not in ("已置顶", "消息免打扰")]
        unread = 0
        if rest:
            m = _UNREAD_RE.match(rest[0])
            if m:
                unread = int(m.group(1))
                rest = rest[1:]
        preview = rest[0] if rest else ""
        when = rest[-1] if len(rest) > 1 else ""
        out.append(
            {
                "chat": title,
                "unread": unread,
                "preview": preview[:80],
                "time": when,
                "muted": muted,
                "pinned": pinned,
            }
        )
        if len(out) >= limit:
            break
    return out


def _item_rows(ctrl) -> list[dict]:
    """消息列表的每一行：`{text, top, bottom}`（屏幕坐标，给下面的左右判定用）。

    为什么要把矩形一起带出来：UIA 的 ListItem **没有发送方**（群聊也一样），
    连子节点都没有（2026-10-24 实测 4.1.15.13）。左右只能从气泡的横向位置判，
    而判之前得先知道每条消息占的是哪一行。
    """
    rows: list[dict] = []
    try:
        children = ctrl.GetChildren()
    except Exception:
        return rows
    for c in children:
        try:
            if c.ControlTypeName != "ListItemControl":
                continue
            r = c.BoundingRectangle
            rows.append({"text": c.Name or "", "top": r.top, "bottom": r.bottom})
        except Exception:
            continue
    return rows


def _item_names(ctrl) -> list[str]:
    return [r["text"] for r in _item_rows(ctrl)]


def _row_side(band, width: int) -> str:
    """一条消息行的图像切片 → left / right / center / unknown。

    判据是**墨迹的横向重心**：左气泡贴左、右气泡贴右、时间行居中。
    `unknown` 不是失败兜底，而是一条必须保留的状态 —— 猜错方向的代价是
    让模型把对方的话当成「我说的」，比不标更糟（见 docs/评测-117群-回复质量）。
    """
    import numpy as np

    lum = band.min(axis=2)
    cols = np.where((lum < 190).sum(axis=0) >= 2)[0]
    # 掐掉左右边缘：右侧滚动条、左侧头像边线都会把重心拽向中间。
    cols = cols[(cols > 8) & (cols < width - 40)]
    if cols.size == 0:
        return "unknown"
    cx = (cols.min() + cols.max()) / 2.0
    if cx < width * 0.40:
        return "left"
    if cx > width * 0.60:
        return "right"
    return "center"


def label_sides(rows: list[dict], frame=None) -> list[str]:
    """给每条消息标「谁发的」：right=我 / left=对方 / center=时间行 / unknown=判不出。

    不用雷达的整帧切块：那套是「按投影把整帧切成块」，块与 UIA 行不是一一对应
    （一条消息可能切出两段、相邻消息可能并成一块，实测 8 行对 11 块）。
    这里直接用 UIA 给的行矩形去切图 —— 天然一一对应，不必再对齐。

    `frame` 可注入（测试用）。抓不到窗口时全部 unknown，不抛。
    """
    if not rows:
        return []
    # 时间行按正文就能认，不必看像素（也免得浅灰文字被误判成「居中 = 时间」）
    known = ["center" if _TIME_RE.match((r.get("text") or "").strip()) else "" for r in rows]
    if frame is None:
        try:
            from chat_radar import capture as cap

            frame = cap.capture_frame()
        except Exception:
            frame = None
    if frame is None:
        return [k or "unknown" for k in known]
    win, roi, img = frame.window, frame.roi, frame.image
    width = roi.right - roi.left
    out: list[str] = []
    for r, k in zip(rows, known, strict=False):
        if k:
            out.append(k)
            continue
        text = (r.get("text") or "").strip()
        if not text:
            out.append("unknown")
            continue
        try:
            y0 = max(int(r["top"]) - win.top, roi.top) + 1
            y1 = min(int(r["bottom"]) - win.top, roi.bottom) - 1
            if y1 - y0 < 4:
                out.append("unknown")
                continue
            out.append(_row_side(img[y0:y1, roi.left:roi.right], width))
        except Exception:
            out.append("unknown")
    return out


def _frame():
    """抓一帧微信窗口（给 side 和 name 共用，避免同一次读图抓两遍像素）。"""
    try:
        from chat_radar import capture as cap

        return cap.capture_frame()
    except Exception:
        return None


def label_speakers(
    rows: list[dict],
    frame=None,
    sides: list[str] | None = None,
) -> tuple[list[int], list[str]]:
    """给每条消息标「谁说的」—— 返回 `(speaker id, 昵称)`，与 rows 逐位对齐。

    UIA 拿不到发送方（见 `_item_rows` 的说明），但每条消息的矩形顶部就是昵称行，
    所以只能从像素读（`chat_radar.names`）。

    **id 和名字来历不同，别混**：
      - `id` 来自像素指纹，纯本地毫秒级，同一个人连发多条拿到同一个 id。
        所以就算名字还没读出来，面板也能先显示「3 个人在说话」。
      - `name` 来自视觉模型（`names.refresh`），2~3s 一批，可能还是空串。
        空串表示「还没读过」，不是「这个人叫空」。

    跳过两类，省时间也避免读错：
      - 时间分隔行：那一行没有昵称，硬读会把 `12:06` 当成发言人；
      - `side == "right"`：用户自己发的消息右侧没有昵称条。

    几何按 DPI 比例缩放（窗口逻辑宽 vs 位图宽），不能写死像素 ——
    125% 缩放下位图是 1.5 倍宽，写死的 dx 会切进头像。
    """
    if not rows:
        return [], []
    ids = [0] * len(rows)
    names = [""] * len(rows)
    try:
        from chat_radar import names as nz
    except Exception:
        return ids, names
    if frame is None:
        frame = _frame()
    if frame is None:
        return ids, names

    win, roi, img = frame.window, frame.roi, frame.image
    scale = (img.shape[1] / win.width) if win.width else 1.0
    oy = int(win.top * scale)  # 窗口顶在屏幕上的物理偏移（WindowInfo 是逻辑值）
    dx = int(nz.NICK_DX * scale)
    hh = int(nz.NICK_H * scale)
    ww = int(nz.NICK_W * scale)
    x0 = roi.left + dx
    x1 = min(img.shape[1], x0 + ww)

    bands: list = []
    at: list[int] = []
    for i, r in enumerate(rows):
        text = (r.get("text") or "").strip()
        if not text or _TIME_RE.match(text):
            continue
        if sides is not None and i < len(sides) and sides[i] == "right":
            continue
        try:
            y0 = int(r["top"]) - oy
            band = img[max(0, y0 - 1) : y0 + hh, x0:x1]
        except Exception:
            continue
        if band.size:
            bands.append(band)
            at.append(i)

    for k, (sid, name) in enumerate(nz.identify(bands)):
        ids[at[k]] = sid
        names[at[k]] = name
    return ids, names


def read_names(*, timeout: float = 90.0) -> dict:
    """显式读一次昵称：拼图 → Ornith 视觉 → 回填缓存。

    与 `read_chat` 分开是刻意的：`read_chat` 要快（面板刷新，~150ms），
    而这一步要 2~3s 并且会占用 6G 显存。**只在用户主动点「总结/分析」时调**，
    别放进任何自动刷新路径 —— 会和实时变声抢显存。
    """
    from chat_radar import names as nz

    chat_ctrl, sessions, err = _locate_lists()
    if err:
        return {"ok": False, "error": err}
    rows = _item_rows(chat_ctrl)
    frame = _frame()
    if frame is None:
        return {"ok": False, "error": "抓不到微信窗口"}
    sides = label_sides(rows, frame=frame)
    bands = _bands_for(rows, frame, sides)
    got, why = nz.refresh(bands, timeout=timeout)
    out: dict = {
        "ok": True,
        "chat": _selected_chat(sessions) if sessions is not None else "",
        "learned": got,
        "people": _people(bands),
    }
    if why:
        out["note"] = why
    return out


def _bands_for(rows: list[dict], frame, sides: list[str] | None = None) -> list:
    """按与 `label_speakers` 相同的几何裁出昵称条（给 refresh 用）。"""
    from chat_radar import names as nz

    win, roi, img = frame.window, frame.roi, frame.image
    scale = (img.shape[1] / win.width) if win.width else 1.0
    oy = int(win.top * scale)
    dx = int(nz.NICK_DX * scale)
    hh = int(nz.NICK_H * scale)
    x0 = roi.left + dx
    x1 = min(img.shape[1], x0 + int(nz.NICK_W * scale))
    out = []
    for i, r in enumerate(rows):
        text = (r.get("text") or "").strip()
        if not text or _TIME_RE.match(text):
            continue
        if sides is not None and i < len(sides) and sides[i] == "right":
            continue
        try:
            y0 = int(r["top"]) - oy
            band = img[max(0, y0 - 1) : y0 + hh, x0:x1]
        except Exception:
            continue
        if band.size:
            out.append(band)
    return out


def _people(bands) -> list[dict]:
    """当前会话里出现过的人（按说话条数降序）。"""
    from chat_radar import names as nz

    counts: dict[int, int] = {}
    for sid, _name in nz.identify(bands):
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
    return [
        {"id": sid, "name": nz.name_of(sid), "n": n}
        for sid, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def _selected_chat(ctrl) -> str:
    """当前选中会话的标题。拿不到就返回空串（前端显示「当前会话」）。"""
    try:
        children = ctrl.GetChildren()
    except Exception:
        return ""
    for c in children:
        try:
            pat = c.GetSelectionItemPattern()
            if pat and pat.IsSelected:
                first = (c.Name or "").splitlines()
                return first[0].strip() if first else ""
        except Exception:
            continue
    return ""


def _window_error() -> str:
    """`_root()` 拿不到根控件时的**分因**说明。

    为什么不能只说「读不到微信窗口（没开，或 UIA 未激活）」：2026-10-24 实测，
    微信收进托盘时同一块桌宠面板给出两个互相矛盾的说法 —— 发送预检说
    「它还在运行、多半被关进了托盘：请点开微信主窗口」，而「分析聊天」说「没开」。
    用户只会信第一句，于是认定「分析聊天坏了」。

    `wechat_proc.find_wechat_hwnd()` 早就按「进程没开 / 收进托盘 / 还在登录页」分了因，
    这里直接复用它抛出来的原文 —— 文案的唯一真相源留在一处，别再编第二句。
    """
    if not wechat_uia.UIA_ENABLED:
        return "微信 UIA 被关闭（VM_WECHAT_UIA=0）：分析聊天读不到消息，改回 1 再重启后端"
    try:
        import wechat_proc

        # 窗口在的时候它正常返回（这里只借它分因的报错文案）。
        wechat_proc.find_wechat_hwnd()
    except Exception as exc:  # noqa: BLE001 —— RuntimeError 是预期分支：它带着人话原因
        msg = str(exc).strip()
        if msg:
            return msg
    # 走到这里说明窗口找得到、但 UIA 根控件拿不到：树还没物化 / COM 抖动。
    return "读不到微信控件树（窗口在，但 UIA 没物化）：把微信切到前台、点开一个聊天再点「重新分析」"


def _walk_lists(root) -> tuple[dict[str, object], int]:
    """遍历整棵树，取「消息 / 会话」两份 ListControl。返回 `(found, seen)`。

    单个节点的 COM 异常只当那一条分支断了（Qt 的 provider 在重绘/重排期间会短暂
    抛 `E_ELEMENTNOTAVAILABLE`，整棵树读出来像「什么都没打开」），不往外抛 ——
    由调用方按「没找到」再走一遍。
    """
    found: dict[str, object] = {}
    seen = 0

    def walk(ctrl, depth: int) -> None:
        nonlocal seen
        if depth > MAX_DEPTH or seen > MAX_NODES:
            return
        seen += 1
        try:
            if ctrl.ControlTypeName == "ListControl" and ctrl.Name in (LIST_CHAT, LIST_SESSIONS):
                found.setdefault(ctrl.Name, ctrl)
        except Exception:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for c in children:
            walk(c, depth + 1)

    walk(root, 0)
    return found, seen


def _locate_lists():
    """共用前置：窗口分因 → UIA 热激活 → 遍历找到「消息 / 会话」两个列表。

    返回 `(消息列表 ctrl, 会话列表 ctrl, 错误)`。错误非空时两个 ctrl 都是 None。
    """
    # ① 窗口层：没窗口（微信收进托盘 / 还在登录页）就分因报错，别去碰内存。
    hwnd, _pid = wechat_uia._wechat_hwnd_pid()
    if not hwnd:
        return None, None, _window_error()

    # ② gate 层 + ③ 遍历层：微信 4.x 的 Qt accessibility gate 关着时，UIA 树就是个**空壳**
    #    （遍历出来像「微信什么都没打开」）。冷启动时 active byte = 0；运行中 Qt 也会自己
    #    把它关回 0（2026-10-29 实测），所以「开一次就永久有效」不成立：
    #    第一遍正常走；没找到「消息」就 reset_state + ensure_active(force=True) 把 gate
    #    重开一次再走一遍。全程只写 1 个字节进微信内存，不点击、不抢焦点、不改微信状态。
    found: dict[str, object] = {}
    seen = 0
    walk_err = ""
    for attempt in range(_WALK_ATTEMPTS):
        if attempt:
            # 第 2 次：先强制把 gate 重开，再重走一遍。
            #
            # 为什么必须 force：`ensure_active()` 的 ready 是**按微信 PID 缓存**的，
            # Qt 自己把 accessibility gate byte 关回 0 之后，缓存照样说「就绪」，
            # 于是树成了空壳、一直报「找不到消息列表」，直到**别的进程**把它写回 1
            # —— 用户看到的就是「时灵时不灵，过一会儿自己又好了」
            # （2026-10-29 复现：写 0 → 同进程立刻读成空树 → force 写回 1 → 立即恢复）。
            # reset_state() 顺带丢掉本线程缓存的、那一棵已经作废的根控件。
            wechat_uia.reset_state()
            time.sleep(_WALK_RETRY_DELAY_S)
        if not wechat_uia.ensure_active(force=bool(attempt)):
            why = wechat_uia.activation_reason()
            return None, None, (
                f"读不到微信控件（{why}）：把微信窗口切到前台再点「重新分析」"
                if why
                else "读不到微信控件：把微信窗口切到前台再点「重新分析」"
            )

        root = wechat_uia._root()
        if root is None:
            return None, None, _window_error()
        try:
            found, seen = _walk_lists(root)
            walk_err = ""
        except Exception as exc:  # noqa: BLE001 —— COM 抖动是已知现象，降级不抛
            found, walk_err = {}, f"遍历微信控件失败：{exc}"
        if found.get(LIST_CHAT) is not None:
            return found[LIST_CHAT], found.get(LIST_SESSIONS), ""

        # ① 面积最大的窗可能**根本不是主窗**：微信的更新提示 / 图片预览 / 聊天信息
        #    是同进程、同类名、标题都叫「微信」的弹窗，主窗一最小化就轮到它。
        #    这时树上只有一个弹窗按钮、0 个列表（2026-10-29 实测 19 个控件 0 个列表）。
        #    根类名不是 MainWindow 就去拿真正的主窗再走一遍 —— 空壳树不走这条路
        #    （gate 关掉时根类名仍是 MainWindow），所以和下面的强制重开互不干扰。
        if attempt == 0 and getattr(root, "ClassName", "") != wechat_uia.CLASS_MAIN:
            alt = wechat_uia.main_window()
            if alt is not None:
                try:
                    found, seen = _walk_lists(alt)
                    walk_err = ""
                except Exception as exc:  # noqa: BLE001 —— 同上，COM 抖动降级
                    found, walk_err = {}, f"遍历微信控件失败：{exc}"
                if found.get(LIST_CHAT) is not None:
                    return found[LIST_CHAT], found.get(LIST_SESSIONS), ""

    # 走到这里 = 每一遍都没找到「消息」。留一行 warning 进 backend.log：
    # INFO 级日志不会落盘（没装 handler），这条必须能看见，下次复现才有证据。
    logger.warning(
        "[chat_read] %d 次遍历都没有「消息」列表：节点 %d 个，列表 %s",
        _WALK_ATTEMPTS, seen, sorted(found) or ["无"],
    )
    if walk_err and not found:
        return None, None, walk_err
    if not found:
        # 空壳树：gate 被关回去、重开也没救回来。别让用户去「打开一个聊天」——
        # 微信明明开着（2026-10-29 报障就是照着这句去反复点聊天窗口的）。
        return None, None, (
            f"读不到微信的控件树（窗口在，遍历到 {seen} 个控件却一个列表都没有，"
            "已强制重新激活一次）：把微信切到前台再点「重新分析」"
        )
    # 树是好的，只是没停在聊天界面（通讯录 / 设置 / 搜索页，或空的文件传输助手）。
    return None, None, "找不到「消息」列表：请先在微信里打开一个聊天（窗口这会儿可能停在通讯录/设置页）"


def collect() -> tuple[list[str], list[str], str, str]:
    """从活着的微信 UIA 树取原始名单（不带发送方）。

    返回 `(消息名, 会话名, 当前会话标题, 错误)`。错误为空表示成功。
    任何 UIA 异常都降级成错误文本 —— 这是只读视图，不该让接口 500。
    """
    chat, sessions, err = _locate_lists()
    if err:
        return [], [], "", err
    return (
        _item_names(chat),
        _item_names(sessions) if sessions is not None else [],
        _selected_chat(sessions) if sessions is not None else "",
        "",
    )


def collect_labeled() -> tuple[list[dict], list[str], str, str]:
    """同 `collect()`，但每条消息带上「谁发的」（`side`）和「谁」（`name`）。

    多出来的这一步只多抓一帧窗口像素（PrintWindow，不碰微信进程内存），
    实测 ~120ms；昵称条 OCR 走指纹缓存，冷启动一屏 ~0.1s。拿不到就整批标
    unknown / 空串 —— 降级而不是报错。

    `side` 和 `name` **共用同一帧**：分开抓会多一次 PrintWindow，而且两次之间
    窗口可能滚动，行会对不齐。
    """
    chat, sessions, err = _locate_lists()
    if err:
        return [], [], "", err
    rows = _item_rows(chat)
    frame = _frame()
    sides = label_sides(rows, frame=frame)
    sids, snames = label_speakers(rows, frame=frame, sides=sides)
    msgs = [
        {"text": r["text"], "side": s, "speaker": sid, "name": nm}
        for r, s, sid, nm in zip(rows, sides, sids, snames, strict=False)
    ]
    return (
        msgs,
        _item_names(sessions) if sessions is not None else [],
        _selected_chat(sessions) if sessions is not None else "",
        "",
    )


def read_chat(limit: int = 20, screens: int = 0) -> dict:
    """当前会话快照：标题 + 最近消息（带「谁说的」+ 发言人昵称）+ 会话列表（未读）。

    `screens` = 额外往上翻几屏（0 = 只看得到的那一屏，默认）。用户选「近 25/45 条」
    时前端传 2/4。翻屏是**有代价**的：每屏多一次 `collect_labeled`（~1s）+ 一次
    滚轮 + 等重排（~0.3s），所以只在用户显式要求时做，且读完一定滚回底部。
    """
    screens = max(0, min(int(screens or 0), MAX_SCREENS))
    msgs, sess_names, chat, err = collect_labeled()
    if err:
        return {"ok": False, "error": err}

    batches: list[list[dict]] = [msgs]
    scrolled = 0
    if screens:
        chat_ctrl = _locate_lists()[0]
        if chat_ctrl is not None:
            try:
                for _ in range(screens):
                    if not _scroll_chat(chat_ctrl, up=True):
                        break
                    time.sleep(_SCROLL_SETTLE_S)
                    more, _sess, _chat, more_err = collect_labeled()
                    if more_err or not more:
                        break
                    # 滚不动 = 已经到顶：Qt 会把同一批原样再给一遍。
                    if [_msg_key(m) for m in more] == [_msg_key(m) for m in batches[-1]]:
                        break
                    batches.append(more)
                    scrolled += 1
            finally:
                # 回到底部：多给一屏的余量，滚轮到底会被微信自己夹住（实测 12 格
                # 从第 4 屏精确回到原来那一屏）。
                _scroll_chat(chat_ctrl, up=False, notches=NOTCHES_PER_SCREEN * (scrolled + 2))
                time.sleep(_SCROLL_SETTLE_S)

    merged = merge_history(batches)
    names = [m["text"] for m in merged]
    sides = [m["side"] for m in merged]
    speakers = [m.get("name") or "" for m in merged]
    sids = [int(m.get("speaker") or 0) for m in merged]
    # 深读时 limit 不能按「一屏」算：用户选了 3 屏，20 条的上限会把刚滚出来的全切掉。
    cap = min(max(limit, 5), 60)
    if scrolled:
        cap = min(max(limit, 12 * (scrolled + 1)), MAX_DEEP)
    return {
        "ok": True,
        "chat": chat,
        "messages": parse_messages(
            names,
            limit=cap,
            sides=sides,
            speakers=speakers,
            speaker_ids=sids,
        ),
        "sessions": parse_sessions(sess_names),
        "raw_n": len(names),
        "screens": screens,
        "screens_read": scrolled,
    }


def is_question(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and bool(_Q_TAIL.search(t) or _Q_HEAD.match(t))


def analyze(snap: dict, verdict: dict | None = None) -> dict:
    """本地规则分析：**只做能证实的判断**，不猜语义、不生成回复文案。

    产出四样：headline（一句话概述）、highlights（值得看一眼的点）、
    unread（哪些会话还有未读）、suggest（下一步该做什么，来自雷达结论 + 文本规则）。
    """
    msgs = snap.get("messages") or []
    bodies = [m for m in msgs if m.get("kind") != "time"]
    texts = [m for m in bodies if m.get("kind") == "text"]
    voices = [m for m in bodies if m.get("kind") == "voice"]
    medias = [m for m in bodies if m.get("kind") == "media"]
    cur = str(snap.get("chat") or "")
    # 未读分两堆：当前会话的未读就摆在眼前（往下翻就能看），不该算进「其他会话」；
    # 它只作为一条提示，不参与 headline 的计数。
    unread_all = [s for s in (snap.get("sessions") or []) if s.get("unread")]
    unread = [s for s in unread_all if s["chat"] != cur]
    cur_unread = sum(s["unread"] for s in unread_all if s["chat"] == cur)

    highlights: list[str] = []
    ats = [m["text"] for m in texts if "@" in m["text"]]
    if ats:
        highlights.append("有人在会话里 @：" + ats[-1][:24])
    if texts and is_question(texts[-1]["text"]):
        highlights.append("最后一条像是提问：「" + texts[-1]["text"][:24] + "」")
    if voices:
        highlights.append(f"含 {len(voices)} 条语音（要点开听）")
    if medias:
        highlights.append(f"含 {len(medias)} 条图片/文件类消息")
    if cur_unread:
        highlights.append(f"当前会话还有 {cur_unread} 条未读（往下翻）")
    deep = int(snap.get("screens_read") or 0)
    if len(bodies) >= 10:
        highlights.append(
            f"这次读到 {len(bodies)} 条（往上翻了 {deep} 屏）" if deep
            else f"这次读到 {len(bodies)} 条可见消息"
        )

    v = verdict or {}
    act = v.get("action") or ""
    label = v.get("label") or ""
    reason = v.get("reason") or ""
    if act in ("now", "soon"):
        suggest = {"action": "reply", "label": label or "该回了", "reason": reason}
    elif act == "wait":
        suggest = {"action": "wait", "label": label or "再等等", "reason": reason}
    elif texts and is_question(texts[-1]["text"]):
        # 直接摆在眼前的问题比「别的会话有未读」更该先处理。
        suggest = {"action": "reply", "label": "对方在问，建议回", "reason": texts[-1]["text"][:30]}
    elif unread:
        top = max(unread, key=lambda s: s["unread"])
        suggest = {
            "action": "check",
            "label": "有未读",
            "reason": f"{top['chat']} 还有 {top['unread']} 条没看",
        }
    else:
        suggest = {"action": "none", "label": "暂时不用回", "reason": "没看到在等你的问题"}

    unread_total = sum(s["unread"] for s in unread)
    parts = []
    if snap.get("chat"):
        parts.append(str(snap["chat"]))
    deep = int(snap.get("screens_read") or 0)
    parts.append(f"{len(bodies)} 条（翻了 {deep} 屏）" if deep else f"{len(bodies)} 条可见消息")
    if unread_total:
        parts.append(f"其他会话 {unread_total} 条未读")

    return {
        "headline": " · ".join(parts),
        "highlights": highlights,
        "unread": sorted(unread, key=lambda s: -s["unread"])[:4],
        "unread_total": unread_total,
        "suggest": suggest,
    }
