"""chat_read 单测：UIA 名称 → 消息/会话语义，以及 /api/radar/chat 的降级。

全部用字符串样本，不碰真微信、不碰 GDI / UIA。样本取自 2026-10-23 在
117 群实测拿到的真实 ListItem 名称（见 docs 与 memory）。
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chat_read  # noqa: E402
import radar_api  # noqa: E402

# ---------------------------------------------------------------- 消息解析

CHAT_SAMPLE = [
    "23:03",
    "@瑜清\u2005",
    "红包领一",
    "收到",
    "23:15",
    '语音4"秒',
    "啥东西",
    "23:21",
    "图片",
    "偷偷吃一",
]


def test_message_kind_covers_real_uia_names():
    assert chat_read.message_kind("23:21") == "time"
    assert chat_read.message_kind("昨天 23:32") == "time"
    assert chat_read.message_kind("7/30") == "time"
    assert chat_read.message_kind('语音4"秒') == "voice"
    # 4.2.0.11 实测：没播过的语音带后缀（`语音3"秒未播放`），旧正则把这类当成文字
    assert chat_read.message_kind('语音3"秒未播放') == "voice"
    assert chat_read.message_kind('语音12"秒已播放') == "voice"
    assert chat_read.parse_messages(['语音3"秒未播放'])[0]['seconds'] == 3.0
    assert chat_read.message_kind("图片") == "media"
    assert chat_read.message_kind("[链接] 登录操作通知") == "media"
    assert chat_read.message_kind("对方撤回了一条消息") == "system"
    assert chat_read.message_kind("偷偷吃一") == "text"


def test_parse_messages_parses_voice_seconds_and_keeps_order():
    items = chat_read.parse_messages(CHAT_SAMPLE)
    kinds = [m["kind"] for m in items]
    assert kinds.count("time") == 3
    voice = next(m for m in items if m["kind"] == "voice")
    assert voice["seconds"] == 4.0
    # 树序即时间序：最后一条是「偷偷吃一」
    assert items[-1] == {"kind": "text", "text": "偷偷吃一"}


def test_parse_messages_keeps_tail_only():
    items = chat_read.parse_messages(CHAT_SAMPLE, limit=3)
    assert [m["text"] for m in items] == ["23:21", "图片", "偷偷吃一"]


def test_parse_messages_skips_empty_names():
    assert chat_read.parse_messages(["", "  ", "在吗"]) == [{"kind": "text", "text": "在吗"}]


# ---------------------------------------------------------------- 会话列表

SESSIONS_SAMPLE = [
    "i志愿\n已置顶\n[1条] \n2026年全省志愿服务工作推进会在广州召开\n昨天 08:58\n消息免打扰\n",
    "117\n[3条] \n李河璋: 偷偷吃一\n23:21\n",
    "文件传输助手\n[语音] 6\"\n昨天 23:32\n",
    "巴黎罗\n[1条] \n你不在 MVP 吗\n23:20\n",
]


def test_parse_sessions_extracts_unread_and_preview():
    out = chat_read.parse_sessions(SESSIONS_SAMPLE)
    assert [s["chat"] for s in out] == ["i志愿", "117", "文件传输助手", "巴黎罗"]
    assert out[0]["unread"] == 1  # 「已置顶」不是内容，已剔除
    assert out[1]["unread"] == 3
    assert out[1]["preview"] == "李河璋: 偷偷吃一"
    assert out[1]["time"] == "23:21"
    # `[语音] 6"` 是预览不是未读 —— 只有「数字+条」才算未读
    assert out[2]["unread"] == 0


def test_parse_sessions_limits():
    assert len(chat_read.parse_sessions(SESSIONS_SAMPLE, limit=2)) == 2


# ---------------------------------------------------------------- 问句 / 分析

def test_is_question():
    assert chat_read.is_question("你吃了吗")
    assert chat_read.is_question("啥东西")
    assert chat_read.is_question("在吗")
    assert chat_read.is_question("真的假的？")
    assert not chat_read.is_question("偷偷吃一")
    assert not chat_read.is_question("")


def _snap(**over):
    base = {
        "ok": True,
        "chat": "117",
        "messages": chat_read.parse_messages(CHAT_SAMPLE),
        "sessions": chat_read.parse_sessions(SESSIONS_SAMPLE),
    }
    base.update(over)
    return base


def test_analyze_reports_question_mention_and_unread():
    out = chat_read.analyze(_snap(), {"action": "wait", "label": "再等等", "reason": "刚发完"})
    assert out["headline"].startswith("117")
    # 当前会话（117）的 3 条未读就在眼前，不算「其他会话」
    assert "其他会话 2 条未读" in out["headline"]
    assert out["unread_total"] == 2
    assert [s["chat"] for s in out["unread"]] == ["i志愿", "巴黎罗"]
    assert any("@瑜清" in h for h in out["highlights"])
    # 最后一条是「偷偷吃一」，不是问句 —— 不能因为上文出现过「啥东西」就判成提问
    assert not any("提问" in h for h in out["highlights"])
    assert any("当前会话还有 3 条未读" in h for h in out["highlights"])
    # wait 优先于「有未读」：雷达说再等，就不该催着去看别的会话
    assert out["suggest"]["action"] == "wait"


def test_analyze_prefers_radar_reply_verdict():
    out = chat_read.analyze(_snap(), {"action": "now", "label": "该回了", "reason": "对方等了 3 分钟"})
    assert out["suggest"] == {"action": "reply", "label": "该回了", "reason": "对方等了 3 分钟"}


def test_analyze_without_verdict_falls_back_to_text_rules():
    """雷达不可用时：最后一条是问句 → 建议回；否则只提示未读。"""
    asked = _snap(
        messages=chat_read.parse_messages(["23:21", "啥东西"]),
        sessions=[],
    )
    out = chat_read.analyze(asked, None)
    assert out["suggest"]["action"] == "reply"
    assert "啥东西" in out["suggest"]["reason"]

    quiet = _snap(messages=[{"kind": "text", "text": "收到"}], sessions=[])
    out2 = chat_read.analyze(quiet, None)
    assert out2["suggest"]["action"] == "none"
    assert out2["unread_total"] == 0


# ---------------------------------------------------------------- 快照读取

def _labeled(names, sides=None):
    """把名字列表变成 collect_labeled 的返回形状（sides 缺省全 unknown）。"""
    ss = sides or ["unknown"] * len(names)
    return ([{"text": n, "side": s} for n, s in zip(names, ss, strict=False)], SESSIONS_SAMPLE, "117", "")


def test_read_chat_shapes_collect_result(monkeypatch):
    monkeypatch.setattr(chat_read, "collect_labeled", lambda: _labeled(CHAT_SAMPLE))
    snap = chat_read.read_chat(limit=6)
    assert snap["ok"] is True
    assert snap["chat"] == "117"
    assert snap["raw_n"] == len(CHAT_SAMPLE)
    assert [m["text"] for m in snap["messages"]] == CHAT_SAMPLE[-6:]
    assert snap["sessions"][1]["chat"] == "117"


def test_read_chat_carries_sender_side(monkeypatch):
    """消息要带 side：模型靠它区分「我 / 对方」（2026-10-24 评测的 2 组错位就栽在这）。"""
    monkeypatch.setattr(
        chat_read,
        "collect_labeled",
        lambda: _labeled(["啥东西", "收到"], ["left", "right"]),
    )
    snap = chat_read.read_chat()
    assert [m["side"] for m in snap["messages"]] == ["left", "right"]


def test_read_chat_degrades_to_error_not_exception(monkeypatch):
    monkeypatch.setattr(chat_read, "collect_labeled", lambda: ([], [], "", "读不到微信窗口"))
    snap = chat_read.read_chat()
    assert snap == {"ok": False, "error": "读不到微信窗口"}


def test_parse_messages_carries_speaker_id_and_name():
    """面板消息行第一格「谁说的」直接吃这两个字段（`anaMsgRow`）。

    两者的来历不同，别混：`speaker` 是像素指纹的 id（本地毫秒级，同一个人连发
    多条拿到同一个 id），`name` 要等 `/names` 让视觉模型读过才有值。
    **id 有、name 空 = 「还没读过」，不是「这个人叫空」** —— 所以空名字不写字段，
    前端才能用 id 造一个「群友3」的临时别名，而不是显示一个空格。
    """
    rows = chat_read.parse_messages(
        ["啥东西", "收到", "晚上吃啥"], speakers=["红姜、", "", ""], speaker_ids=[3, 4, 0]
    )
    assert [m.get("speaker") for m in rows] == [3, 4, None]
    assert rows[0]["name"] == "红姜、"
    assert "name" not in rows[1], "空名字不许写占位符（前端要能区分没读过）"
    assert "speaker" not in rows[2], "id=0 表示这条认不出人，同样不该落字段"


# ---------------------------------------------------------------- 谁说的（左右判定）
# UIA 的 ListItem 只有正文、没有发送方（连子节点都没有），左右只能看气泡位置。
# 这三个用例锁住判据本身，防止以后「顺手调阈值」把它调成瞎猜。


def _band(width=1000, height=40, ink=None):
    import numpy as np

    a = np.full((height, width, 3), 250, dtype=np.uint8)
    for x0, x1 in (ink or []):
        a[:, x0:x1] = 0
    return a


def test_row_side_follows_ink_centroid():
    assert chat_read._row_side(_band(ink=[(30, 60)]), 1000) == "left"
    assert chat_read._row_side(_band(ink=[(900, 950)]), 1000) == "right"
    assert chat_read._row_side(_band(ink=[(480, 520)]), 1000) == "center"
    assert chat_read._row_side(_band(), 1000) == "unknown"


def test_row_side_ignores_right_scrollbar_and_left_edge():
    """右侧滚动条、左侧头像边线不能把重心拽到中间（实测会把右气泡判成 center）。"""
    band = _band(ink=[(30, 60), (985, 995)])
    assert chat_read._row_side(band, 1000) == "left"


def test_label_sides_without_frame_marks_time_rows_only():
    rows = [{"text": "23:21", "top": 0, "bottom": 10}, {"text": "啥东西", "top": 10, "bottom": 20}]
    assert chat_read.label_sides(rows, frame=None) == ["center", "unknown"]


def test_label_sides_reads_sides_from_frame():
    import types

    rows = [
        {"text": "啥东西", "top": 10, "bottom": 40},
        {"text": "收到", "top": 60, "bottom": 90},
        {"text": "23:21", "top": 100, "bottom": 120},
    ]
    import numpy as np

    img = np.full((200, 1000, 3), 250, dtype=np.uint8)
    img[10:40, 30:60] = 0      # 第一条：左边
    img[60:90, 900:950] = 0    # 第二条：右边
    frame = types.SimpleNamespace(
        window=types.SimpleNamespace(left=0, top=0),
        roi=types.SimpleNamespace(left=0, top=0, right=1000, bottom=200),
        image=img,
    )
    assert chat_read.label_sides(rows, frame=frame) == ["left", "right", "center"]


def test_parse_messages_keeps_side_aligned_when_rows_are_blank():
    """空行会被跳过，但 side 必须还是**按原名次序**对齐，不能错位一格。"""
    out = chat_read.parse_messages(["啥东西", "  ", "收到"], sides=["left", "left", "right"])
    assert [(m["text"], m["side"]) for m in out] == [("啥东西", "left"), ("收到", "right")]


# ---------------------------------------------------------------- 窗口失败要分因
# 2026-10-24 用户视角实测：微信收进托盘时，同一块面板上「发送预检」说
# 「它还在运行、多半被关进了托盘」，而「分析聊天」只说「没开」——
# 两个说法冲突，用户会认定分析按钮坏了。原因在 collect() 把
# wechat_proc.find_wechat_hwnd() 的分因报错吞掉了。


def test_collect_uses_tray_reason_when_window_missing(monkeypatch):
    import wechat_proc
    import wechat_uia

    monkeypatch.setattr(wechat_uia, "_wechat_hwnd_pid", lambda: (None, None))
    monkeypatch.setattr(wechat_uia, "_root", lambda: None)
    monkeypatch.setattr(wechat_uia, "UIA_ENABLED", True)

    tray_msg = (
        "微信没有任何可见窗口（pid: 9848）——它还在运行、多半被关进了托盘："
        "请点开微信主窗口、进入聊天界面后重试"
    )

    def _no_window():
        raise RuntimeError(tray_msg)

    monkeypatch.setattr(wechat_proc, "find_wechat_hwnd", _no_window)
    msgs, sess, chat, err = chat_read.collect()
    assert err == tray_msg          # 原文透传，不再改写成「没开」
    assert "托盘" in err
    assert msgs == [] and sess == [] and chat == ""


def test_collect_reports_disabled_uia_before_probing_window(monkeypatch):
    """UIA 被人为关掉时，别去怪微信没开 —— 那是配置问题。"""
    import wechat_uia

    monkeypatch.setattr(wechat_uia, "_wechat_hwnd_pid", lambda: (None, None))
    monkeypatch.setattr(wechat_uia, "_root", lambda: None)
    monkeypatch.setattr(wechat_uia, "UIA_ENABLED", False)
    err = chat_read.collect()[3]
    assert "VM_WECHAT_UIA" in err


def test_collect_says_uia_not_materialized_when_window_exists(monkeypatch):
    import wechat_proc
    import wechat_uia

    monkeypatch.setattr(wechat_uia, "_wechat_hwnd_pid", lambda: (123, 456))
    monkeypatch.setattr(wechat_uia, "ensure_active", lambda force=False: True)
    monkeypatch.setattr(wechat_uia, "_root", lambda: None)
    monkeypatch.setattr(wechat_uia, "UIA_ENABLED", True)
    monkeypatch.setattr(wechat_proc, "find_wechat_hwnd", lambda: 12345)
    err = chat_read.collect()[3]
    assert "控件树" in err


def test_collect_missing_message_list_points_at_chat_view(monkeypatch):
    """窗口在、树也在，只是没停在聊天界面 → 别再说「最小化了」（怎么试都没用）。"""

    class _List:
        def __init__(self, name):
            self.Name = name
            self.ControlTypeName = "ListControl"

        def GetChildren(self):
            return []

    class _Root:
        Name = "root"
        ControlTypeName = "WindowControl"
        ClassName = "mmui::MainWindow"

        def GetChildren(self):
            return [_List("会话")]      # 只有会话列表，没有「消息」

    monkeypatch.setattr(chat_read.wechat_uia, "_wechat_hwnd_pid", lambda: (123, 456))
    monkeypatch.setattr(chat_read.wechat_uia, "ensure_active", lambda force=False: True)
    monkeypatch.setattr(chat_read.wechat_uia, "_root", lambda: _Root())
    err = chat_read.collect()[3]
    assert "消息" in err
    assert "最小化" not in err


def test_collect_activates_uia_gate_before_reading(monkeypatch):
    """读之前必须先热激活 gate —— 微信重启后 gate 归零，不激活就永远读到空树。

    （2026-10-24 实测：发送链路会激活，而「分析聊天」不走那条路，
      于是重启微信后第一次点分析只看到「找不到消息列表」。）
    """
    import wechat_uia

    calls: list[bool] = []

    def _act(force=False):
        calls.append(force)
        return True

    monkeypatch.setattr(wechat_uia, "_wechat_hwnd_pid", lambda: (123, 456))
    monkeypatch.setattr(wechat_uia, "ensure_active", _act)
    monkeypatch.setattr(chat_read.wechat_uia, "_root", lambda: _RootWithChat())
    chat_read.collect()
    assert calls == [False]          # 只激活一次，不 force（force 是发送链路的重试用）


class _RootEmpty:
    """空壳树：只有窗口按钮，一个 ListControl 都没有（gate 关回去时的真实形态）。

    **根类名仍是 `mmui::MainWindow`** —— gate 关掉时 Qt 只是不物化子控件，
    根控件本身还在，所以这条路径不该被当成「挑错了窗口」。
    """

    Name = "root"
    ControlTypeName = "WindowControl"
    ClassName = "mmui::MainWindow"

    class _Btn:
        Name = "最小化"
        ControlTypeName = "ButtonControl"

        def GetChildren(self):
            return []

    def GetChildren(self):
        return [self._Btn()]


def test_collect_retries_with_forced_activation_when_tree_is_empty(monkeypatch):
    """空壳树要把 gate **强开一次再走**，别把「找不到消息列表」直接甩给用户。

    2026-10-29 复现：Qt 把 accessibility gate byte 关回 0 之后，进程内 `ready`
    仍是缓存的 True —— 同一个进程照样读成空树、报「找不到消息列表」；
    `ensure_active(force=True)` 把 1 写回去，同一份代码立刻恢复。用户看到的是
    「时灵时不灵，过一会儿自己又好了」（其实是别的进程顺手把它写回去的）。
    """
    import wechat_uia

    calls: list[bool] = []

    def _act(force=False):
        calls.append(force)
        return True

    roots = [_RootEmpty(), _RootWithChat()]
    monkeypatch.setattr(wechat_uia, "_wechat_hwnd_pid", lambda: (123, 456))
    monkeypatch.setattr(wechat_uia, "ensure_active", _act)
    monkeypatch.setattr(chat_read.wechat_uia, "_root", lambda: roots.pop(0))
    err = chat_read.collect()[3]
    assert err == ""
    assert calls == [False, True]      # 第一遍普通激活，第二遍强制重开


def test_collect_falls_back_to_real_main_window_when_area_pick_is_a_dialog(monkeypatch):
    """面积最大的窗是「新版本」弹窗时，要换真正的主窗读，别直接报找不到列表。

    2026-10-29 实测：主窗最小化后，微信更新提示窗（847×686，同进程同类名同标题）
    成了面积最大的那个 → 树上 19 个控件、0 个列表 → 用户看到「找不到消息列表」。
    """
    import wechat_uia

    class _Dialog:
        Name = "微信"
        ControlTypeName = "WindowControl"
        ClassName = "Qt51514QWindowIcon"      # 还没物化的弹窗就是这类名

        class _Btn:
            Name = "更新"
            ControlTypeName = "ButtonControl"

            def GetChildren(self):
                return []

        def GetChildren(self):
            return [self._Btn()]

    monkeypatch.setattr(wechat_uia, "_wechat_hwnd_pid", lambda: (123, 456))
    monkeypatch.setattr(wechat_uia, "ensure_active", lambda force=False: True)
    monkeypatch.setattr(chat_read.wechat_uia, "_root", lambda: _Dialog())
    monkeypatch.setattr(wechat_uia, "main_window", lambda: _RootWithChat())
    err = chat_read.collect()[3]
    assert err == ""


def test_collect_empty_tree_does_not_blame_the_chat_window(monkeypatch):
    """两遍都是空壳 → 说「控件树读不到」，别再说「请先打开一个聊天」（它明明开着）。"""
    import wechat_uia

    monkeypatch.setattr(wechat_uia, "_wechat_hwnd_pid", lambda: (123, 456))
    monkeypatch.setattr(wechat_uia, "ensure_active", lambda force=False: True)
    monkeypatch.setattr(chat_read.wechat_uia, "_root", lambda: _RootEmpty())
    err = chat_read.collect()[3]
    assert "控件树" in err
    assert "通讯录" not in err


def test_collect_surfaces_gate_failure_reason(monkeypatch):
    """gate 打不开时要说清「为什么」，别再落到「找不到消息列表」那句误导文案上。"""
    import wechat_uia

    monkeypatch.setattr(wechat_uia, "_wechat_hwnd_pid", lambda: (123, 456))
    monkeypatch.setattr(wechat_uia, "ensure_active", lambda force=False: False)
    monkeypatch.setattr(wechat_uia, "activation_reason", lambda: "写内存失败")
    err = chat_read.collect()[3]
    assert "写内存失败" in err
    assert "消息列表" not in err


class _RootWithChat:
    """最小可用的假 UIA 根：一份「消息」列表 + 一份「会话」列表。"""

    Name = "root"
    ControlTypeName = "WindowControl"
    ClassName = "mmui::MainWindow"

    class _List:
        def __init__(self, name):
            self.Name = name
            self.ControlTypeName = "ListControl"

        def GetChildren(self):
            return []

    def GetChildren(self):
        return [self._List("消息"), self._List("会话")]




# ---------------------------------------------------------------- HTTP 壳

def _ok_tick():
    return types.SimpleNamespace(
        ok=True,
        error="",
        verdict=types.SimpleNamespace(
            as_dict=lambda: {"action": "soon", "label": "可以回了", "score": 2, "reason": "对方等了 40s", "source": "rule"}
        ),
        signals=types.SimpleNamespace(
            as_dict=lambda: {"last_side": "left", "streak": 2, "gap_seconds": 40.0, "frames_seen": 9}
        ),
    )


class _StubRadar:
    def tick(self):
        return _ok_tick()

    def reset(self):
        pass


def _install_radar(stub=None):
    radar_api._RADAR = stub or _StubRadar()


def teardown_function():
    radar_api.reset_singleton()


def test_chat_endpoint_merges_snapshot_verdict_and_analysis(monkeypatch):
    _install_radar()
    monkeypatch.setattr(chat_read, "collect_labeled", lambda: _labeled(CHAT_SAMPLE))
    out = radar_api.chat(limit=20)
    assert out["ok"] is True
    assert out["chat"] == "117"
    assert out["verdict"]["action"] == "soon"
    assert out["analysis"]["suggest"]["action"] == "reply"
    assert out["signals"]["last_side"] == "left"
    assert "verdict_note" not in out


def test_chat_endpoint_failure_is_200_with_error(monkeypatch):
    _install_radar()
    monkeypatch.setattr(
        chat_read, "collect_labeled",
        lambda: ([], [], "", "读不到微信窗口（没开，或 UIA 未激活）"),
    )
    out = radar_api.chat()
    assert out["ok"] is False
    assert "读不到微信窗口" in out["error"]
    assert "analysis" not in out


def test_chat_endpoint_survives_radar_failure(monkeypatch):
    """雷达炸了不能把消息本身也带走：verdict_note 记一笔，分析照旧。"""

    class _Boom:
        def tick(self):
            raise RuntimeError("GDI 抓图失败")

    _install_radar(_Boom())
    monkeypatch.setattr(
        chat_read,
        "collect_labeled",
        lambda: _labeled(["23:21", "啥东西"], ["center", "left"]),
    )
    out = radar_api.chat()
    assert out["ok"] is True
    assert "雷达不可用" in out["verdict_note"]
    assert out["analysis"]["suggest"]["action"] == "reply"  # 文本规则仍可判断


# ---------------------------------------------------------------- 深读（滚动补齐）
#
# 2026-10-29 实测背景：消息列表是 `mmui::RecyclerListView`（虚拟列表），
# `GetPattern(ScrollPattern)` 返回 False —— 没有 UIA 滚动接口，只能发真实滚轮。
# 这些用例把「拼接 / 到顶 / 回滚 / 不滚」四件事钉死，不碰真微信。


def _m(text, side="left"):
    return {"text": text, "side": side, "speaker": 0, "name": ""}


def test_merge_history_aligns_on_overlap():
    """往上滚一屏后，旧批的尾巴会和新批的头重复 —— 不能拼出两遍同一句话。"""
    newest = [_m("D"), _m("E"), _m("F")]
    older = [_m("A"), _m("B"), _m("C"), _m("D"), _m("E")]
    merged = chat_read.merge_history([newest, older])
    assert [m["text"] for m in merged] == ["A", "B", "C", "D", "E", "F"]


def test_merge_history_keeps_batch_when_no_overlap():
    """对不上就别猜：宁可多一条重复，也不能吞掉用户的话。"""
    merged = chat_read.merge_history([[_m("X")], [_m("Y")]])
    assert [m["text"] for m in merged] == ["Y", "X"]


def test_merge_history_tolerates_empty_batches():
    assert chat_read.merge_history([]) == []
    assert [m["text"] for m in chat_read.merge_history([[_m("A")], []])] == ["A"]


def test_read_chat_deep_collects_scrolled_screens_and_restores(monkeypatch):
    """翻屏 → 拼接 → 判到顶 → 回到底部，四步都要发生（且只滚一次回去）。"""
    batches = [
        _labeled(["C", "D", "E"]),
        _labeled(["A", "B", "C"]),
        _labeled(["X", "Y", "A"]),
        _labeled(["X", "Y", "A"]),      # 滚不动了：Qt 原样再给一遍
    ]
    calls = []

    monkeypatch.setattr(chat_read, "collect_labeled", lambda: batches.pop(0))
    monkeypatch.setattr(chat_read, "_locate_lists", lambda: (object(), None, ""))
    monkeypatch.setattr(
        chat_read, "_scroll_chat",
        lambda ctrl, up, notches=chat_read.NOTCHES_PER_SCREEN: calls.append(up) or True,
    )
    monkeypatch.setattr(chat_read.time, "sleep", lambda _s: None)

    snap = chat_read.read_chat(limit=20, screens=3)
    assert [m["text"] for m in snap["messages"]] == ["X", "Y", "A", "B", "C", "D", "E"]
    assert snap["screens_read"] == 2
    assert calls == [True, True, True, False]   # 上滚三次命中到顶，最后一次回底


def test_read_chat_shallow_never_scrolls(monkeypatch):
    """screens=0（默认）不许动鼠标滚轮：这条是「只读、零副作用」的守门人。"""
    monkeypatch.setattr(chat_read, "collect_labeled", lambda: _labeled(["A"]))

    def _boom(*a, **k):
        raise AssertionError("screens=0 时不许滚动")

    monkeypatch.setattr(chat_read, "_scroll_chat", _boom)
    snap = chat_read.read_chat(limit=20)
    assert snap["screens_read"] == 0
    assert [m["text"] for m in snap["messages"]] == ["A"]


def test_scroll_chat_refuses_when_cursor_is_over_another_app(monkeypatch):
    """滚轮事件跟着**光标下的窗口**走：不是微信就一律不滚。

    没有这道护栏，微信被浏览器盖住时「自动看上下文」会去滚用户正在读的网页。
    """
    import wechat_uia

    monkeypatch.setattr(wechat_uia, "rect_of", lambda ctrl: (0, 0, 100, 100))
    monkeypatch.setattr(wechat_uia, "_wechat_hwnd_pid", lambda: (1, 456))
    monkeypatch.setattr(chat_read, "_window_pid", lambda hwnd: 999)
    assert chat_read._scroll_chat(object(), up=True) is False


def test_replies_scales_model_feed_with_screens(monkeypatch):
    """用户选了几屏，喂给模型的条数就得跟着涨（封顶 30），否则深读白翻。"""
    import reply_advisor

    seen = {}

    def _suggest(messages, chat="", last_side=None, *, avoid=None, feed=reply_advisor.FEED_MESSAGES):
        seen["feed"] = feed
        seen["n"] = len(messages)
        return {"engine": "local", "replies": []}

    monkeypatch.setattr(reply_advisor, "suggest", _suggest)
    batches = [
        _labeled(["C", "D", "E"]),
        _labeled(["A", "B", "C"]),
        _labeled(["X", "Y", "A"]),
    ]
    monkeypatch.setattr(chat_read, "collect_labeled", lambda: batches.pop(0))
    monkeypatch.setattr(chat_read, "_locate_lists", lambda: (object(), None, ""))
    monkeypatch.setattr(chat_read, "_scroll_chat", lambda *a, **k: True)
    monkeypatch.setattr(chat_read.time, "sleep", lambda _s: None)

    radar_api.replies(limit=14, screens=2)
    assert seen["feed"] == reply_advisor.FEED_MESSAGES + 8
    assert seen["n"] >= 5
