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
