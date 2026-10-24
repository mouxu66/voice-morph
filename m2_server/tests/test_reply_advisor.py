"""reply_advisor 单测：提示词 / 严格 JSON 校验 / 降级，以及 /api/radar/replies 的合并。

全部不碰真模型、不碰真微信：模型用 monkeypatch 顶掉，样本取自 117 群的真实 UIA 名称。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chat_read  # noqa: E402
import radar_api  # noqa: E402
import reply_advisor as ra  # noqa: E402

MSGS = [
    {"kind": "time", "text": "23:03"},
    {"kind": "text", "text": "@瑜清 红包领一"},
    {"kind": "text", "text": "收到"},
    {"kind": "time", "text": "23:15"},
    {"kind": "voice", "text": '语音4"秒', "seconds": 4},
    {"kind": "text", "text": "啥东西"},
    {"kind": "media", "text": "图片"},
]


# ---------------------------------------------------------------- 提示词


def test_build_prompt_drops_time_and_labels_media():
    prompt = ra.build_prompt(MSGS, "117", "left")
    assert "当前会话：117" in prompt
    assert "23:03" not in prompt          # 时间行只白占 token
    assert "[语音 4 秒]" in prompt
    assert "[图片]" in prompt
    assert "最后一条是对方发的" in prompt


def test_build_prompt_marks_my_own_last_message():
    prompt = ra.build_prompt(MSGS, "117", "right")
    assert "用户自己发的" in prompt


def test_build_prompt_limits_to_tail():
    many = [{"kind": "text", "text": f"消息{i}"} for i in range(40)]
    prompt = ra.build_prompt(many, "117")
    assert "消息39" in prompt
    assert "消息0" not in prompt          # 只喂最后 FEED_MESSAGES 条


# ---------------------------------------------------------------- 发言人标记（2026-10-24）
# 试过把「我：/对方：」写进提示词：9 组真实语料没有一组变好，还反复把「我：」
# 原样写进回复正文（点一下就真发出去）。所以提示词里**不该**出现发送方标记 ——
# 这条测试就是防止以后「看着挺合理」又加回去。


def test_build_prompt_does_not_leak_sender_labels():
    msgs = [
        {"kind": "text", "text": "要不要出去玩", "side": "left"},
        {"kind": "text", "text": "🉑", "side": "left"},
        {"kind": "text", "text": "我下午没课", "side": "right"},
    ]
    prompt = ra.build_prompt(msgs, "117", "right")
    assert "要不要出去玩" in prompt
    for bad in ("我：", "对方：", "（我说的）"):
        assert bad not in prompt


def test_system_prompt_forbids_promises_but_allows_jokes():
    """用户 2026-10-24 拍板：梗要留，但「先答上再谈梗」，且不许替我答应任何事。"""
    s = ra._SYSTEM
    assert "至少一条" in s            # 三条里必须先有一条落到实质诉求上
    assert "梗不能替代答案" in s
    assert "不要承诺" in s
    # 自动托管是**真的发出去**，分寸更硬，也必须带上这条
    assert "绝不替用户答应" in ra._SYSTEM_ONE


# ---------------------------------------------------------------- 承诺拦截（确定性）
# 提示词说了「不许承诺」，4B 照写（A/B 实测）。所以输出侧必须有一道确定性过滤。


def test_promise_guard_catches_real_failures():
    """样本就是 117 群里真实翻车的那些句子。"""
    bad = [
        "我帮你买一瓶荔枝水",
        "好的，我马上帮你买！",
        "荔枝水我马上去给你买",
        "我帮你下单",
        "我请你吃饭",
        "收到红包，谢谢！",
        "包在我身上",
    ]
    for s in bad:
        assert ra.looks_like_promise(s), s


def test_promise_guard_does_not_eat_normal_replies():
    for s in ("已经很多人被点到了", "我来了", "取餐码 33", "外面等你们", "哈哈哈"):
        assert not ra.looks_like_promise(s), s


def test_suggest_drops_promise_candidates(monkeypatch):
    monkeypatch.setattr(ra, "available", lambda: (True, ""))
    monkeypatch.setattr(ra, "_ensure_server", lambda: 1234)

    def _fake_chat(port, prompt, timeout, **kw):
        return '{"should_reply":true,"why":"他让带水","replies":["我帮你买一瓶","上一次买过的"]}'

    monkeypatch.setattr(ra, "_chat", _fake_chat)
    out = ra.suggest([{"kind": "text", "text": "帮我买瓶水"}], "117", "left")
    assert out["replies"] == ["上一次买过的"]
    assert "替你答应" in out["note"]


def test_suggest_falls_back_when_every_candidate_promises(monkeypatch):
    monkeypatch.setattr(ra, "available", lambda: (True, ""))
    monkeypatch.setattr(ra, "_ensure_server", lambda: 1234)
    monkeypatch.setattr(
        ra, "_chat",
        lambda *a, **k: '{"replies":["我帮你买","我请你吃饭","收到红包，谢谢"]}',
    )
    out = ra.suggest([{"kind": "text", "text": "帮我买瓶水"}], "117", "left")
    assert out["replies"] == ra._rule_replies([{"kind": "text", "text": "帮我买瓶水"}], "left")
    assert "安全的模板" in out["note"]


def test_suggest_one_refuses_to_send_a_promise(monkeypatch):
    monkeypatch.setattr(ra, "available", lambda: (True, ""))
    monkeypatch.setattr(ra, "_ensure_server", lambda: 1234)
    monkeypatch.setattr(ra, "_chat", lambda *a, **k: '{"reply":"我马上帮你下单","why":"带水"}')
    assert ra.suggest_one([{"kind": "text", "text": "帮我买瓶水"}], "117", "left") is None


# ---------------------------------------------------------------- 思考型模型（2026-10-24）
# Qwen3.5 不关思考时：content 是空串、答案全在 reasoning_content。实测 9 条全空，
# 看着像模型挂了；拿 reasoning 兜底更糟 —— 里面的格式样例会被当成回复。


def _fake_urlopen(payload_obj):
    import io
    import json as _json

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return lambda req, timeout=None: _R(_json.dumps(payload_obj).encode())


def test_chat_disables_thinking_by_default(monkeypatch):
    import json as _json

    captured = {}

    def _spy(req, timeout=None):
        captured.update(_json.loads(req.data.decode()))
        return _fake_urlopen({"choices": [{"message": {"content": "好的"}}]})(req)

    monkeypatch.setattr(ra.urllib.request, "urlopen", _spy)
    assert ra._chat(1234, "hi", 5.0) == "好的"
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}


def test_chat_never_returns_reasoning_as_answer(monkeypatch):
    monkeypatch.setattr(
        ra.urllib.request, "urlopen",
        _fake_urlopen({"choices": [{"message": {
            "content": "",
            "reasoning_content": '思考…{"replies":["reply1","reply2"]}',
        }}]}),
    )
    assert ra._chat(1234, "hi", 5.0) == ""    # 空就是空，让调用方降级成模板


# ---------------------------------------------------------------- JSON 校验


def test_parse_reply_json_plain_and_fenced():
    out = ra.parse_reply_json('{"why":"他想聊","replies":["哈哈","收到"]}')
    assert out["replies"] == ["哈哈", "收到"]
    assert out["why"] == "他想聊"
    fenced = '```json\n{"replies":["哈哈"]}\n```'
    assert ra.parse_reply_json(fenced)["replies"] == ["哈哈"]


def test_parse_reply_json_rejects_bad_shapes():
    assert ra.parse_reply_json("") is None
    assert ra.parse_reply_json("我不知道怎么回") is None
    assert ra.parse_reply_json('{"replies":[]}') is None
    assert ra.parse_reply_json('{"replies":"哈哈"}') is None
    assert ra.parse_reply_json('{"replies":[1,2]}') is None


def test_parse_reply_json_clamps_and_dedupes():
    long = "很长" * 30
    out = ra.parse_reply_json(json.dumps({"replies": ["好", "好", long, "收到"]}))
    assert out["replies"] == ["好", "收到"]


# ---------------------------------------------------------------- 降级模板


def test_rule_replies_question_vs_right_side():
    q = ra._rule_replies([{"kind": "text", "text": "啥东西"}], None)
    assert "在的，怎么了？" in q
    # 最后一条是我发的：唯一正确的建议是「不用追」
    assert ra._rule_replies([{"kind": "text", "text": "吃了吗"}], "right") == ["等对方回就行"]


def test_suggest_falls_back_when_model_missing(monkeypatch):
    monkeypatch.setattr(ra, "available", lambda: (False, "没找到本地模型"))
    out = ra.suggest(MSGS, "117")
    assert out["engine"] == "rule"
    assert out["note"] == "没找到本地模型"
    assert out["replies"]


def test_suggest_parses_model_output(monkeypatch):
    monkeypatch.setattr(ra, "available", lambda: (True, ""))
    monkeypatch.setattr(ra, "_ensure_server", lambda: 8099)
    monkeypatch.setattr(
        ra, "_chat", lambda port, prompt, timeout: '{"why":"他想聊","replies":["哈哈","收到"]}'
    )
    out = ra.suggest(MSGS, "117", "left")
    assert out["engine"] == "local"
    assert out["replies"] == ["哈哈", "收到"]
    assert out["why"] == "他想聊"
    assert isinstance(out["elapsed_ms"], float)


def test_suggest_falls_back_when_inference_fails(monkeypatch):
    monkeypatch.setattr(ra, "available", lambda: (True, ""))

    def boom():
        raise RuntimeError("llama-server 在 90s 内没 ready")

    monkeypatch.setattr(ra, "_ensure_server", boom)
    out = ra.suggest(MSGS, "117")
    assert out["engine"] == "rule"
    assert "没跑起来" in out["note"]


def test_suggest_falls_back_when_output_invalid(monkeypatch):
    monkeypatch.setattr(ra, "available", lambda: (True, ""))
    monkeypatch.setattr(ra, "_ensure_server", lambda: 8099)
    monkeypatch.setattr(ra, "_chat", lambda port, prompt, timeout: "我觉得你应该先回一句")
    out = ra.suggest(MSGS, "117")
    assert out["engine"] == "rule"
    assert "校验" in out["note"]


# ---------------------------------------------------------------- HTTP 端点


def test_radar_replies_passes_side_and_reads_chat(monkeypatch):
    monkeypatch.setattr(
        chat_read, "read_chat",
        lambda limit=20: {"ok": True, "chat": "117", "messages": list(MSGS)},
    )
    seen = {}

    def fake_suggest(msgs, chat, side):
        seen.update(msgs=msgs, chat=chat, side=side)
        return {"engine": "local", "replies": ["哈哈"], "why": "他想聊"}

    monkeypatch.setattr(ra, "suggest", fake_suggest)
    out = radar_api.replies(limit=14, last_side="left")
    assert out["ok"] is True and out["chat"] == "117"
    assert out["replies"] == ["哈哈"]
    assert seen["side"] == "left"
    assert seen["chat"] == "117"


def test_radar_replies_ignores_unknown_side(monkeypatch):
    monkeypatch.setattr(
        chat_read, "read_chat",
        lambda limit=20: {"ok": True, "chat": "117", "messages": []},
    )
    seen = {}
    monkeypatch.setattr(
        ra, "suggest",
        lambda msgs, chat, side: seen.update(side=side) or {"engine": "rule", "replies": ["收到"]},
    )
    radar_api.replies(limit=14, last_side="up")
    assert seen["side"] is None


def test_radar_replies_passthrough_when_wechat_closed(monkeypatch):
    monkeypatch.setattr(
        chat_read, "read_chat",
        lambda limit=20: {"ok": False, "error": "读不到微信窗口（没开，或 UIA 未激活）"},
    )
    out = radar_api.replies(limit=14)
    assert out["ok"] is False
    assert "读不到微信窗口" in out["error"]
