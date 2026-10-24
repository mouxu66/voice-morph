"""微信 4.x 的**文本**自动化：切会话 / 读输入框 / 写入并发送纯文本。

为什么与 wechat_voice 分开
--------------------------
`wechat_voice` 那条链路是「录音 → 虚拟声卡 → 按住 Alt → 发语音」，每一步都要求
微信在前台、真实点击渲染子窗口，且只发音频。自动托管要的是**另一条路**：

    · 切会话：`ListControl Name="会话"` 的 ListItem 支持 SelectionItemPattern.Select()，
      **后台**就能切（不点鼠标、不抢前台）；
    · 写文本：输入框 `EditControl ClassName="mmui::ChatInputField"` 支持 ValuePattern，
      SetValue 成功时「发送」按钮会从 IsEnabled=False 变 True —— 这是**唯一可靠的判据**；
    · 发送：`ButtonControl Name="发送" ClassName="mmui::XOutlineButton"` 支持 InvokePattern。

实测（2026-10-24，微信 4.1.13.12）：这三步都不需要前台。而输入框的 `Name` 恰好是
**当前会话标题**（如 "117"），正好用来校验「会话真的切过去了」。

为什么 SetValue 可能不够
------------------------
Qt Quick 的输入框有自己的内部状态机：SetValue 有时只改 UIA 的 Value 而不触发
「输入非空 → 发送按钮可用」。所以这里的判据不是「SetValue 没抛异常」，而是
**发送按钮真的变可用**；不成立就退回「前台化 + SendInput 逐字输入」。回退会抢一次
前台（用户能看到微信跳出来），因此会记进返回值 `method` 与托管日志。

边界（刻意不做的）
------------------
不碰剪贴板（会覆盖用户正在复制的内容）、不发语音/文件/图片/红包/转账/链接、
不点「+」面板、不删改用户已输入的内容（输入框里有草稿 → 直接放弃这一次）。
"""

from __future__ import annotations

import time

import chat_read
import wechat_uia

#: 与 wechat_uia.CLASS_INPUT 相同；本地再写一份是为了在 wechat_uia 改名时不至于静默失配
CLASS_INPUT = "mmui::ChatInputField"
CLASS_SEND_BUTTON = "mmui::XOutlineButton"
NAME_SEND = "发送"

#: 等「发送按钮变可用」与「输入框被清空」的窗口。实测 SetValue 后 <100ms 即变可用，
#: 给 1.2s 是为了容忍微信自己的输入防抖；发送后清空通常 <200ms，给 2.0s。
ENABLE_WAIT = 1.2
SENT_WAIT = 2.0

#: 扫描会话列表时的深搜上限（与 chat_read 一致，控件树异常时不至于卡死）
MAX_DEPTH = chat_read.MAX_DEPTH
MAX_NODES = chat_read.MAX_NODES

MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120


# ---------------------------------------------------------------- 控件定位


def _find_list(name: str):
    """按名字找 ListControl（"会话" / "消息"）。

    不用 `wechat_uia._find(Name=...)`：同名控件可能存在（比如按钮也叫"消息"），
    而 chat_read 的遍历已经证明按 `ControlTypeName == "ListControl" + Name` 才唯一。
    """
    root = wechat_uia._root()
    if root is None:
        return None
    found: list[object] = []
    seen = 0

    def walk(ctrl, depth: int) -> None:
        nonlocal seen
        if found or depth > MAX_DEPTH or seen > MAX_NODES:
            return
        seen += 1
        try:
            if ctrl.ControlTypeName == "ListControl" and ctrl.Name == name:
                found.append(ctrl)
                return
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
    except Exception:
        return None
    return found[0] if found else None


def _session_items() -> list[tuple[str, object]]:
    """[(会话标题, ListItem 控件)]，顺序与列表一致。"""
    lst = _find_list(chat_read.LIST_SESSIONS)
    if lst is None:
        return []
    out: list[tuple[str, object]] = []
    try:
        children = lst.GetChildren()
    except Exception:
        return []
    for c in children:
        try:
            if c.ControlTypeName != "ListItemControl":
                continue
            title = (c.Name or "").splitlines()
            out.append((title[0].strip() if title else "", c))
        except Exception:
            continue
    return out


def selected_chat() -> str:
    """当前选中会话的标题（读不到返回空串）。"""
    lst = _find_list(chat_read.LIST_SESSIONS)
    if lst is None:
        return ""
    return chat_read._selected_chat(lst)  # 同一套「哪一项 IsSelected」判据


def _input_ctrl():
    return wechat_uia._find(ClassName=CLASS_INPUT)


def _send_button():
    return wechat_uia._find(ClassName=CLASS_SEND_BUTTON, Name=NAME_SEND)


def _input_value(ctrl) -> str:
    """输入框当前文字。ValuePattern 取不到时返回 None（调用方据此放弃）。"""
    try:
        pat = ctrl.GetValuePattern()
        return pat.Value or ""
    except Exception:
        return None


def input_state() -> dict:
    """输入框快照：当前会话名 / 已有文字 / 发送按钮是否可用。

    `Name` 是微信给输入框标的**当前会话标题**（实测），用它校验切会话是否成功。
    """
    ctrl = _input_ctrl()
    if ctrl is None:
        return {"ok": False, "chat": "", "value": "", "can_send": False, "error": "找不到微信输入框（窗口最小化或 UIA 未激活）"}
    try:
        chat = ctrl.Name or ""
    except Exception:
        chat = ""
    value = _input_value(ctrl)
    if value is None:
        return {"ok": False, "chat": chat, "value": "", "can_send": False, "error": "输入框不支持 ValuePattern"}
    btn = _send_button()
    can_send = False
    has_btn = btn is not None
    if btn is not None:
        try:
            can_send = bool(btn.IsEnabled)
        except Exception:
            can_send = False
    return {"ok": True, "chat": chat, "value": value, "can_send": can_send, "has_send_button": has_btn, "error": ""}


# ---------------------------------------------------------------- 切会话


def select_session(title: str, timeout: float = 1.5) -> dict:
    """后台切到指定会话，并用输入框 `Name` 校验成功。

    为什么校验 `Name` 而不是「ListItem.IsSelected」：选中态可能滞后一拍，
    而输入框 Name 是**这一刻真正在编辑哪个会话**，这才是有意义的事实。
    """
    title = (title or "").strip()
    if not title:
        return {"ok": False, "chat": "", "error": "会话名为空"}
    items = _session_items()
    if not items:
        return {"ok": False, "chat": "", "error": "读不到会话列表（窗口最小化或 UIA 未激活）"}
    target = next((c for t, c in items if t == title), None)
    if target is None:
        return {"ok": False, "chat": "", "error": f"会话列表里没有「{title}」（可能被滚出可视区）"}
    try:
        target.GetSelectionItemPattern().Select()
    except Exception as exc:  # noqa: BLE001 —— 控件不支持就换不了会话语义
        return {"ok": False, "chat": "", "error": f"选中会话失败：{exc}"}
    deadline = time.time() + max(timeout, 0.2)
    while time.time() < deadline:
        cur = input_state()
        if cur.get("ok") and cur.get("chat") == title:
            return {"ok": True, "chat": title, "error": ""}
        time.sleep(0.15)
    cur = input_state()
    return {
        "ok": False,
        "chat": cur.get("chat", ""),
        "error": f"没切到「{title}」（输入框还停在「{cur.get('chat', '')}」）",
    }


# ---------------------------------------------------------------- 发送文本


def _clear_input(ctrl=None) -> None:
    ctrl = ctrl or _input_ctrl()
    if ctrl is None:
        return
    try:
        ctrl.GetValuePattern().SetValue("")
    except Exception:
        pass


def _wait_can_send(timeout: float) -> bool:
    deadline = time.time() + max(timeout, 0.2)
    while time.time() < deadline:
        btn = _send_button()
        if btn is not None:
            try:
                if btn.IsEnabled:
                    return True
            except Exception:
                pass
        time.sleep(0.1)
    return False


def _type_unicode(text: str) -> None:
    """前台逐字注入（回退路径）。用 KEYEVENTF_UNICODE，绕开输入法/键盘布局。"""
    import wechat_voice  # 惰性：复用同一套 ctypes 结构，避免这里再定义一份

    kbd, inp_t, user32 = wechat_voice._KBDINPUT, wechat_voice._INPUT, wechat_voice._user32()
    input_keyboard = wechat_voice._INPUT_KEYBOARD
    ctypes = wechat_voice._ctypes
    unicode_flag = 0x0004
    keyup = 0x0002
    for ch in text:
        for flags in (unicode_flag, unicode_flag | keyup):
            inp = inp_t()
            inp.type = input_keyboard
            inp.ki = kbd(0, ord(ch), flags, 0, None)
            user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))
            time.sleep(0.005)


def send_text(text: str, expect_chat: str = "") -> dict:
    """把一段纯文本写进当前会话并发送。**永远返回 dict，不抛异常。**

    返回 `{ok, chat, method, error}`。`method` 取：
        value     —— SetValue 直接生效（全程后台，推荐路径）
        typing    —— 回退到前台逐字输入（会抢一次前台）
    失败时**不会**留下半截输入：会清空输入框再返回（用户回来时看到的仍是干净状态）。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "chat": "", "method": "", "error": "回复内容为空"}
    if "\n" in text or "\r" in text:
        return {"ok": False, "chat": "", "method": "", "error": "回复不能含换行（会自动拆成多条消息）"}

    st = input_state()
    if not st.get("ok"):
        return {"ok": False, "chat": st.get("chat", ""), "method": "", "error": st.get("error") or "输入框不可用"}
    if expect_chat and st["chat"] != expect_chat:
        return {
            "ok": False,
            "chat": st["chat"],
            "method": "",
            "error": f"会话不对：期望「{expect_chat}」，输入框停在「{st['chat']}」",
        }
    if st["value"].strip():
        return {
            "ok": False,
            "chat": st["chat"],
            "method": "",
            "error": "输入框里已有未发送的草稿，不覆盖用户内容",
        }

    ctrl = _input_ctrl()
    method = "value"
    try:
        ctrl.GetValuePattern().SetValue(text)
    except Exception:
        pass
    if not _wait_can_send(ENABLE_WAIT):
        # 回退：Qt 没吃下 SetValue。前台化后逐字注入。
        method = "typing"
        _clear_input(ctrl)
        try:
            import wechat_voice

            wechat_voice._foreground_wechat()
            ctrl.SetFocus()
            time.sleep(0.1)
            _type_unicode(text)
        except Exception as exc:  # noqa: BLE001 —— 回退失败就收摊，不能把半截文字留在输入框
            _clear_input(ctrl)
            return {"ok": False, "chat": st["chat"], "method": method, "error": f"回退输入失败：{exc}"}
        if not _wait_can_send(ENABLE_WAIT):
            _clear_input(ctrl)
            return {
                "ok": False,
                "chat": st["chat"],
                "method": method,
                "error": "写进去了但「发送」按钮仍不可用，已清空输入框",
            }

    btn = _send_button()
    if btn is None:
        _clear_input(ctrl)
        return {"ok": False, "chat": st["chat"], "method": method, "error": "找不到「发送」按钮，已清空输入框"}
    try:
        btn.GetInvokePattern().Invoke()
    except Exception as exc:  # noqa: BLE001
        _clear_input(ctrl)
        return {"ok": False, "chat": st["chat"], "method": method, "error": f"点发送失败：{exc}"}

    deadline = time.time() + SENT_WAIT
    while time.time() < deadline:
        if not (input_state().get("value") or "").strip():
            return {"ok": True, "chat": st["chat"], "method": method, "error": ""}
        time.sleep(0.15)
    return {
        "ok": False,
        "chat": st["chat"],
        "method": method,
        "error": "点了发送但输入框没清空，可能没发出去（不再重试，避免重复发送）",
    }


# ---------------------------------------------------------------- 滚动与扫描


def scroll_messages(pages: int) -> dict:
    """滚消息列表。正数 = 往上翻（更早），负数 = 往下翻回底部。

    ⚠️ 滚轮只能发给**前台**窗口，所以这里会把微信切到前台（用户会看到它跳出来）。
    因此自动托管默认 `history_pages=0`（不翻历史），只有用户显式开启才走这条。
    """
    if not pages:
        return {"ok": True, "pages": 0, "error": ""}
    lst = _find_list(chat_read.LIST_CHAT)
    if lst is None:
        return {"ok": False, "pages": 0, "error": "找不到消息列表"}
    rect = wechat_uia.rect_of(lst)
    if rect is None:
        return {"ok": False, "pages": 0, "error": "消息列表没有可见矩形"}
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    try:
        import wechat_voice

        wechat_voice._foreground_wechat()
        wechat_voice._mouse_move_abs(cx, cy)
        step = WHEEL_DELTA if pages > 0 else -WHEEL_DELTA
        for _ in range(min(abs(int(pages)), 8)):
            # 一格滚轮 = 3 行；连发 3 次接近「一页」
            for _ in range(3):
                wechat_voice._send_input_mouse(MOUSEEVENTF_WHEEL, 0, 0, step)
                time.sleep(0.05)
            time.sleep(0.35)
    except Exception as exc:  # noqa: BLE001 —— 滚动失败不该让整轮托管崩掉
        return {"ok": False, "pages": pages, "error": f"滚动失败：{exc}"}
    return {"ok": True, "pages": pages, "error": ""}


def scan_sessions(limit: int = 20) -> dict:
    """当前会话列表（含未读/免打扰/置顶），供托管页选白名单。"""
    lst = _find_list(chat_read.LIST_SESSIONS)
    if lst is None:
        return {"ok": False, "sessions": [], "error": "读不到会话列表（窗口最小化或 UIA 未激活）"}
    names = chat_read._item_names(lst)
    sessions = chat_read.parse_sessions(names, limit=min(max(limit, 1), 40))
    return {"ok": True, "sessions": sessions, "selected": chat_read._selected_chat(lst), "error": ""}
