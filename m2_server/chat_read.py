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
信息正是 UIA 的强项（见 `docs/方案-微信状态雷达.md` 的分层）。

只读边界
--------
不点击、不切会话、不滚动、不发消息。只读「当前已打开会话」已经渲染出来的
消息（UIA 只暴露可见 ListItem），所以它是「看一眼」，不是「拉完整历史」。
发言人归属拿不到（ListItem 只有气泡内容），交给雷达的 last_side/streak；
群聊里的 @ 以正文形式出现，按文本提示。
"""

from __future__ import annotations

import re

import wechat_uia

#: 微信的 UIA 树在窗口控件的第 16 层才出现消息列表（2026-10-23 实测 4.1.13.12），
#: 给到 30 层留余量；节点数封顶防止异常树把接口拖死。
MAX_DEPTH = 30
MAX_NODES = 6000

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


def parse_messages(names: list[str], limit: int = 40, sides: list[str] | None = None) -> list[dict]:
    """UIA ListItem 名称 → 消息 dict，保留**末尾** limit 条（含时间行）。

    保留末尾而不是开头：用户在聊的永远是最近几条，往上翻要靠滚动（改微信状态），
    不在只读边界内。

    `sides` 与 `names` **逐位对齐**（多一个是给不出 side 的降级路径）；给了就带上
    `side` 字段 —— 回复建议靠它区分「我」和「对方」，没有这个标记时模型会把群友
    之间的话也当成对自己说的（2026-10-24 评测里 9 组踩了 2 组）。
    """
    items: list[dict] = []
    pairs = list(zip(names, sides, strict=False)) if sides is not None and len(sides) == len(names) else None
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


def _locate_lists():
    """共用前置：窗口分因 → UIA 热激活 → 遍历找到「消息 / 会话」两个列表。

    返回 `(消息列表 ctrl, 会话列表 ctrl, 错误)`。错误非空时两个 ctrl 都是 None。
    """
    # ① 窗口层：没窗口（微信收进托盘 / 还在登录页）就分因报错，别去碰内存。
    hwnd, _pid = wechat_uia._wechat_hwnd_pid()
    if not hwnd:
        return None, None, _window_error()

    # ② gate 层：微信 4.x 冷启动时 Qt accessibility gate 的 active byte = 0，
    #    此时 UIA 树是**空的**（遍历出来像「微信什么都没打开」）。发送链路会先
    #    `ensure_active()`，而「分析聊天」不走那条路 —— 于是**微信每重启一次**，
    #    第一次点按钮都只会看到「找不到消息列表」，直到用户碰巧发过一条语音才变好
    #    （犯错档案-微信 §5.1、犯错指南速查表第 15 条）。这里补上同一次热激活：
    #    只写 1 字节进微信内存 + 等 0.5s（已激活时按 PID 直接命中缓存，零开销），
    #    不点击、不抢焦点、不改微信任何状态。
    if not wechat_uia.ensure_active():
        why = wechat_uia.activation_reason()
        return None, None, (
            f"读不到微信控件（{why}）：把微信窗口切到前台再点「重新分析」"
            if why
            else "读不到微信控件：把微信窗口切到前台再点「重新分析」"
        )

    root = wechat_uia._root()
    if root is None:
        return None, None, _window_error()

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

    try:
        walk(root, 0)
    except Exception as exc:  # noqa: BLE001 —— COM 抖动是已知现象，降级不抛
        return None, None, f"遍历微信控件失败：{exc}"

    chat = found.get(LIST_CHAT)
    if chat is None:
        # 旧文案说「微信可能最小化了」—— 实测最小化（iconic）的窗口照样有这棵 UIA 树，
        # 真正缺「消息」列表的是「窗口在、但没停在聊天界面」（通讯录/设置/搜索页），
        # 或者当前会话是「文件传输助手」这类没有消息区的空会话。说错原因会让人去
        # 反复最小化/还原窗口，怎么试都没用。
        return None, None, "找不到「消息」列表：请先在微信里打开一个聊天（窗口这会儿可能停在通讯录/设置页）"
    return chat, found.get(LIST_SESSIONS), ""


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
    """同 `collect()`，但每条消息带上「谁发的」（`side`）。

    多出来的这一步只多抓一帧窗口像素（PrintWindow，不碰微信进程内存），
    实测 ~120ms；拿不到就整批标 unknown —— 降级而不是报错。
    """
    chat, sessions, err = _locate_lists()
    if err:
        return [], [], "", err
    rows = _item_rows(chat)
    sides = label_sides(rows)
    msgs = [{"text": r["text"], "side": s} for r, s in zip(rows, sides, strict=False)]
    return (
        msgs,
        _item_names(sessions) if sessions is not None else [],
        _selected_chat(sessions) if sessions is not None else "",
        "",
    )


def read_chat(limit: int = 20) -> dict:
    """当前会话快照：标题 + 最近消息（带「谁说的」）+ 会话列表（未读）。"""
    msgs, sess_names, chat, err = collect_labeled()
    if err:
        return {"ok": False, "error": err}
    names = [m["text"] for m in msgs]
    sides = [m["side"] for m in msgs]
    return {
        "ok": True,
        "chat": chat,
        "messages": parse_messages(names, limit=min(max(limit, 5), 60), sides=sides),
        "sessions": parse_sessions(sess_names),
        "raw_n": len(names),
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
    if len(bodies) >= 10:
        highlights.append(f"这次读到 {len(bodies)} 条可见消息")

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
    parts.append(f"{len(bodies)} 条可见消息")
    if unread_total:
        parts.append(f"其他会话 {unread_total} 条未读")

    return {
        "headline": " · ".join(parts),
        "highlights": highlights,
        "unread": sorted(unread, key=lambda s: -s["unread"])[:4],
        "unread_total": unread_total,
        "suggest": suggest,
    }
