"""全局托管的规则与引擎（不碰微信、不碰模型）。

这个文件钉的是**「什么情况下它会替你说话」** —— 托管是全项目唯一会替用户
给别人发消息的功能，规则错一条就是真实社交事故。所以每条规则都单独有用例，
且引擎用假 ops 跑完整单轮（读列表 → 切会话 → 判 → 生成 → 发 → 恢复）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chat_autopilot as ap  # noqa: E402


# ---------------------------------------------------------------- 配置


def _cfg(**kw) -> dict:
    base = ap.normalize({})
    base.update(kw)
    return base


def test_defaults_are_safe():
    """默认必须「什么都不做」：白名单空、只回提问、跳过免打扰。"""
    d = ap.normalize({})
    assert d["whitelist"] == []
    assert d["question_only"] is True
    assert d["skip_muted"] is True
    assert d["history_pages"] == 0


@pytest.mark.parametrize(
    ("patch", "needle"),
    [
        ({"whitelist": "117"}, "whitelist"),
        ({"poll_s": 1}, "poll_s"),
        ({"poll_s": 601}, "poll_s"),
        ({"poll_s": True}, "poll_s"),
        ({"question_only": "yes"}, "question_only"),
        ({"quiet_hours": [24, 7]}, "quiet_hours"),
        ({"quiet_hours": [23, 7, 1]}, "quiet_hours"),
        ({"reply_max_chars": 999}, "reply_max_chars"),
    ],
)
def test_normalize_rejects_bad_values(patch, needle):
    """坏配置必须**当场报错**，而不是悄悄修正成别的意思。"""
    with pytest.raises(ValueError) as ei:
        ap.normalize({**ap.normalize({}), **patch})
    assert needle in str(ei.value)


def test_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ap, "CONFIG_FILE", tmp_path / "autopilot.json")
    monkeypatch.setattr(ap, "LOG_FILE", tmp_path / "log.jsonl")
    saved = ap.save_config({"whitelist": ["117", "117", " 妈妈 "], "groups": ["117"]})
    assert saved["whitelist"] == ["117", "妈妈"]
    assert ap.load_config()["groups"] == ["117"]


def test_broken_config_falls_back_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "autopilot.json"
    path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(ap, "CONFIG_FILE", path)
    assert ap.load_config()["whitelist"] == []


# ---------------------------------------------------------------- 规则


@pytest.mark.parametrize("hour, expect", [(22, False), (23, True), (0, True), (6, True), (7, False), (12, False)])
def test_quiet_hours_cross_midnight(hour, expect):
    assert ap.is_quiet(_cfg(quiet_hours=[23, 7]), hour) is expect


def test_quiet_hours_equal_means_off():
    assert ap.is_quiet(_cfg(quiet_hours=[9, 9]), 9) is False


def test_at_me_handles_wechat_space():
    assert ap._at_me("@小明 你看下", ["小明"]) is True
    assert ap._at_me("@\u2005小明 你看下", ["小明"]) is True
    assert ap._at_me("小明你看下", ["小明"]) is False
    assert ap._at_me("@所有人 开会", ["小明"]) is False


def _decide(msgs, **kw):
    base = dict(
        chat="117",
        unread=2,
        muted=False,
        messages=msgs,
        side="left",
        cfg_in=_cfg(whitelist=["117"], me_names=["小明"], quiet_hours=[9, 9]),
        now_ts=1000.0,
        hour=12,
        sent_at={},
        seen={},
    )
    base.update(kw)
    return ap.decide(**base)


TEXT_ASK = [{"kind": "text", "text": "在吗？"}]


def test_decide_replies_to_question_from_other_side():
    ok, reason, fp = _decide(TEXT_ASK)
    assert ok and reason == "该回" and fp.startswith("117|2|")


def test_decide_skips_when_not_whitelisted():
    ok, reason, _ = _decide(TEXT_ASK, chat="别的群")
    assert not ok and reason == "不在白名单"


def test_decide_skips_muted():
    ok, reason, _ = _decide(TEXT_ASK, muted=True)
    assert not ok and reason == "会话免打扰"


def test_decide_skips_no_unread():
    ok, reason, _ = _decide(TEXT_ASK, unread=0)
    assert not ok and reason == "没有新消息"


def test_decide_skips_last_from_me():
    """最后一条是我发的 → 不能自己接自己的话。"""
    ok, reason, _ = _decide(TEXT_ASK, side="right")
    assert not ok and "不是对方发的" in reason


def test_decide_skips_unknown_side():
    ok, reason, _ = _decide(TEXT_ASK, side="")
    assert not ok and "不是对方发的" in reason


def test_decide_skips_voice():
    ok, reason, _ = _decide([{"kind": "voice", "text": '语音 4"秒'}])
    assert not ok and "只回文字" in reason


def test_decide_skips_statement_when_question_only():
    ok, reason, _ = _decide([{"kind": "text", "text": "今天天气不错"}])
    assert not ok and reason == "既不是提问也没 @你"


def test_decide_allows_statement_when_question_only_off():
    cfg_in = _cfg(whitelist=["117"], question_only=False, quiet_hours=[9, 9])
    ok, _, _ = _decide([{"kind": "text", "text": "今天天气不错"}], cfg_in=cfg_in)
    assert ok


def test_decide_group_requires_at_me():
    cfg_in = _cfg(whitelist=["117"], groups=["117"], me_names=["小明"], quiet_hours=[9, 9])
    ok, reason, _ = _decide(TEXT_ASK, cfg_in=cfg_in)
    assert not ok and reason == "群聊里没 @你"
    ok2, _, _ = _decide([{"kind": "text", "text": "@小明 这周有空吗"}], cfg_in=cfg_in)
    assert ok2


def test_decide_respects_seen_fingerprint():
    ok, reason, fp = _decide(TEXT_ASK)
    assert ok
    ok2, reason2, _ = _decide(TEXT_ASK, seen={fp: 900.0})
    assert not ok2 and reason2 == "这条刚处理过"


def test_decide_respects_min_gap():
    ok, reason, _ = _decide(TEXT_ASK, sent_at={"117": [980.0]})
    assert not ok and "不足 45s" in reason


def test_decide_respects_hourly_cap():
    sent_at = {"117": [100.0, 300.0, 500.0]}
    ok, reason, _ = _decide(TEXT_ASK, sent_at=sent_at)
    assert not ok and reason == "本会话一小时内回得太多"


def test_decide_quiet_hours_block():
    ok, reason, _ = _decide(TEXT_ASK, cfg_in=_cfg(whitelist=["117"], quiet_hours=[23, 7]), hour=2)
    assert not ok and reason == "静默时段"


# ---------------------------------------------------------------- 引擎单轮


def _ops(
    *,
    sessions=None,
    messages=None,
    side="left",
    reply=None,
    send=None,
    alive=(True, ""),
    detail_error="",
    read_error="",
):
    calls = {"select": [], "send": [], "scroll": [], "suggest": 0}
    selected = {"chat": "orig"}
    sessions = sessions if sessions is not None else [{"chat": "117", "unread": 2, "muted": False}]
    messages = messages if messages is not None else TEXT_ASK
    reply = reply if reply is not None else {"reply": "在的，你说", "why": "对方在等回应"}
    send = send if send is not None else {"ok": True, "chat": "117", "method": "value", "error": ""}

    def read_chat(limit: int = 20):
        if read_error:
            return {"ok": False, "error": read_error, "sessions": [], "chat": "", "messages": []}
        if limit <= 8:
            return {"ok": True, "chat": "orig", "sessions": sessions, "messages": []}
        if detail_error:
            return {"ok": False, "error": detail_error, "sessions": [], "chat": "", "messages": []}
        return {"ok": True, "chat": sessions[0]["chat"], "sessions": sessions, "messages": messages}

    def select_session(title):
        calls["select"].append(title)
        selected["chat"] = title
        return {"ok": True, "chat": title, "error": ""}

    def send_text(text, chat):
        calls["send"].append((text, chat))
        return send

    return calls, {
        "wechat_alive": lambda: alive,
        "read_chat": read_chat,
        "select_session": select_session,
        # ★ 必须跟着 select_session 变，否则 `_restore` 会以为「本来就在原会话」
        "selected_chat": lambda: selected["chat"],
        "peek_side": lambda: {"ok": True, "side": side, "kind": "text", "n_blocks": 3, "error": ""},
        "suggest_one": lambda m, c, s: (calls.__setitem__("suggest", calls["suggest"] + 1) or reply),
        "send_text": send_text,
        "scroll": lambda pages: calls["scroll"].append(pages),
        "now": lambda: 1000.0,
    }


def _engine(monkeypatch, tmp_path, ops, **cfgkw):
    monkeypatch.setattr(ap, "CONFIG_FILE", tmp_path / "autopilot.json")
    monkeypatch.setattr(ap, "LOG_FILE", tmp_path / "log.jsonl")
    path = tmp_path / "autopilot.json"
    path.write_text(__import__("json").dumps(_cfg(whitelist=["117"], quiet_hours=[9, 9], **cfgkw)), encoding="utf-8")
    eng = ap.Engine(ops)
    eng._generation_ready = lambda: (True, "")  # type: ignore[method-assign]
    return eng


def test_tick_sends_and_restores_original(monkeypatch, tmp_path):
    calls, ops = _ops()
    eng = _engine(monkeypatch, tmp_path, ops)
    out = eng.tick()
    assert out["ok"] and out["sent"] == 1
    assert calls["send"] == [("在的，你说", "117")]
    # 切过去 → 收尾切回来；且只切了这两次
    assert calls["select"] == ["117", "orig"]
    assert eng.status()["stats"]["sent"] == 1


def test_tick_skips_when_last_from_me(monkeypatch, tmp_path):
    calls, ops = _ops(side="right")
    eng = _engine(monkeypatch, tmp_path, ops)
    out = eng.tick()
    assert out["sent"] == 0
    assert calls["send"] == []
    assert calls["suggest"] == 0


def test_tick_skips_muted_before_switching(monkeypatch, tmp_path):
    calls, ops = _ops(sessions=[{"chat": "117", "unread": 3, "muted": True}])
    eng = _engine(monkeypatch, tmp_path, ops)
    eng.tick()
    assert calls["send"] == []
    assert calls["select"] == ["orig"] or calls["select"] == []


def test_tick_no_send_when_model_returns_none(monkeypatch, tmp_path):
    calls, ops = _ops(reply={})
    eng = _engine(monkeypatch, tmp_path, ops)
    out = eng.tick()
    assert out["sent"] == 0 and calls["send"] == []


def test_tick_drops_overlong_reply(monkeypatch, tmp_path):
    calls, ops = _ops(reply={"reply": "这" * 41, "why": ""})
    eng = _engine(monkeypatch, tmp_path, ops, reply_max_chars=30)
    out = eng.tick()
    assert out["sent"] == 0 and calls["send"] == []


def test_tick_send_failure_counts_as_error(monkeypatch, tmp_path):
    calls, ops = _ops(send={"ok": False, "chat": "117", "method": "value", "error": "输入框没清空"})
    eng = _engine(monkeypatch, tmp_path, ops)
    out = eng.tick()
    assert out["sent"] == 0
    assert eng.status()["stats"]["errors"] >= 1
    assert "发送失败" in eng.status()["last_error"]


def test_tick_read_failure_records_error(monkeypatch, tmp_path):
    _c, ops = _ops(read_error="微信没开")
    eng = _engine(monkeypatch, tmp_path, ops)
    out = eng.tick()
    assert out["ok"] is False and "微信没开" in out["error"]


def test_tick_respects_max_per_run(monkeypatch, tmp_path):
    sessions = [{"chat": "117", "unread": 5, "muted": False}, {"chat": "妈妈", "unread": 1, "muted": False}]
    calls, ops = _ops(sessions=sessions)
    eng = _engine(monkeypatch, tmp_path, ops, max_per_run=1)
    out = eng.tick()
    assert out["sent"] == 1
    assert len(calls["send"]) == 1


def test_tick_scrolls_when_history_pages_set(monkeypatch, tmp_path):
    calls, ops = _ops()
    eng = _engine(monkeypatch, tmp_path, ops, history_pages=2)
    eng.tick()
    assert calls["scroll"] == [2, -2], "翻上去读完必须翻回底部"


def test_three_failures_trip_stop(monkeypatch, tmp_path):
    _c, ops = _ops(alive=(False, "微信没开"))
    eng = _engine(monkeypatch, tmp_path, ops)
    for _ in range(ap.MAX_FAIL_STREAK):
        eng.tick()
    assert eng._stop.is_set(), "连续失败必须自动停机"


def test_log_is_written(monkeypatch, tmp_path):
    _c, ops = _ops()
    eng = _engine(monkeypatch, tmp_path, ops)
    eng.tick()
    recs = ap.read_log(10)
    assert any(r["action"] == "sent" and r["chat"] == "117" for r in recs)
    assert recs[-1]["reply"] == "在的，你说"


def test_start_requires_consent(monkeypatch, tmp_path):
    _c, ops = _ops()
    eng = _engine(monkeypatch, tmp_path, ops)
    with pytest.raises(PermissionError):
        eng.start(False)


def test_start_requires_whitelist(monkeypatch, tmp_path):
    monkeypatch.setattr(ap, "CONFIG_FILE", tmp_path / "autopilot.json")
    monkeypatch.setattr(ap, "LOG_FILE", tmp_path / "log.jsonl")
    (tmp_path / "autopilot.json").write_text("{}", encoding="utf-8")
    eng = ap.Engine(_ops()[1])
    with pytest.raises(ValueError):
        eng.start(True)


def test_start_then_stop(monkeypatch, tmp_path):
    _c, ops = _ops(alive=(False, "微信没开"))
    eng = _engine(monkeypatch, tmp_path, ops)
    eng.ops()["wechat_alive"] = lambda: (True, "")
    eng.tick = lambda: {"ok": True, "sent": 0}  # type: ignore[method-assign]
    eng.start(True)
    assert eng.status()["status"] == "running"
    eng.pause()
    assert eng.status()["status"] == "paused"
    eng.resume()
    assert eng.status()["status"] == "running"
    eng.stop()
    assert eng.status()["status"] == "off"


def test_ops_are_lazy():
    """引擎模块顶层不能 import wechat/模型 —— 否则测试收集期就被拖进 UIA。"""
    eng = ap.Engine()
    assert eng._ops == {}
    src = Path(ap.__file__).read_text(encoding="utf-8")
    head = src.split("def _default_ops")[0]
    assert "import wechat_text" not in head
    assert "import reply_advisor" not in head
