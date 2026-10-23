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

def test_read_chat_shapes_collect_result(monkeypatch):
    monkeypatch.setattr(
        chat_read,
        "collect",
        lambda: (CHAT_SAMPLE, SESSIONS_SAMPLE, "117", ""),
    )
    snap = chat_read.read_chat(limit=6)
    assert snap["ok"] is True
    assert snap["chat"] == "117"
    assert snap["raw_n"] == len(CHAT_SAMPLE)
    assert [m["text"] for m in snap["messages"]] == CHAT_SAMPLE[-6:]
    assert snap["sessions"][1]["chat"] == "117"


def test_read_chat_degrades_to_error_not_exception(monkeypatch):
    monkeypatch.setattr(chat_read, "collect", lambda: ([], [], "", "读不到微信窗口"))
    snap = chat_read.read_chat()
    assert snap == {"ok": False, "error": "读不到微信窗口"}


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
    monkeypatch.setattr(chat_read, "collect", lambda: (CHAT_SAMPLE, SESSIONS_SAMPLE, "117", ""))
    out = radar_api.chat(limit=20)
    assert out["ok"] is True
    assert out["chat"] == "117"
    assert out["verdict"]["action"] == "soon"
    assert out["analysis"]["suggest"]["action"] == "reply"
    assert out["signals"]["last_side"] == "left"
    assert "verdict_note" not in out


def test_chat_endpoint_failure_is_200_with_error(monkeypatch):
    _install_radar()
    monkeypatch.setattr(chat_read, "collect", lambda: ([], [], "", "读不到微信窗口（没开，或 UIA 未激活）"))
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
        "collect",
        lambda: (["23:21", "啥东西"], [], "117", ""),
    )
    out = radar_api.chat()
    assert out["ok"] is True
    assert "雷达不可用" in out["verdict_note"]
    assert out["analysis"]["suggest"]["action"] == "reply"  # 文本规则仍可判断
