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
        # 2026-10-24 实测漏过的两条（都在 9B/Ornith 的真输出里）
        "洗衣机在哪？我帮你拿",
        "我去买一瓶",
        "我给他带过去",
    ]
    for s in bad:
        assert ra.looks_like_promise(s), s


def test_promise_guard_does_not_eat_normal_replies():
    for s in ("已经很多人被点到了", "我来了", "我去洗澡了", "取餐码 33", "外面等你们", "哈哈哈"):
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


# ---------------------------------------------------------------- 括号补齐（Ornith）
# Ornith-1.5-9B 实测 27 次里 11 次**少写结尾的 `}`**（finish=stop、token 没用完），
# 全被判成「输出非法」白降级。这里只补括号，不猜内容。


def test_close_unbalanced_json_fixes_missing_brace():
    raw = '{"should_reply":true,"why":"顺势接梗","replies":["哈哈确实","神秘嘉宾是谁","藏得挺深"]'
    fixed = ra.close_unbalanced_json(raw)
    assert fixed.endswith("]}")
    assert json.loads(fixed)["replies"] == ["哈哈确实", "神秘嘉宾是谁", "藏得挺深"]


def test_parse_reply_json_repairs_ornith_truncated_tail():
    """就是 2026-10-24 抓到的真实输出：末尾缺一个 `}`。"""
    raw = '{"should_reply":true,"why":"对方要买荔枝水，先确认具体需求","replies":["哪买？","要几瓶？","发个链接我看看"]'
    out = ra.parse_reply_json(raw)
    assert out["replies"] == ["哪买？", "要几瓶？", "发个链接我看看"]


def test_parse_one_json_also_repairs_brace():
    out = ra.parse_one_json('{"reply":"行，买什么牌子？","why":"先问清楚"')
    assert out["reply"] == "行，买什么牌子？"


def test_close_unbalanced_json_refuses_broken_structure():
    assert ra.close_unbalanced_json("没有花括号") is None
    assert ra.close_unbalanced_json('{"a":"没闭合的字符串') is None
    assert ra.close_unbalanced_json('{"a":1}}') is None      # 多一个收尾，结构本来就乱


def test_should_reply_false_is_a_verdict_not_a_failure():
    """模型说「这条不用回」时别退回模板 —— 用户点了按钮收到「哈哈哈」更糟。"""
    out = ra.parse_reply_json('{"should_reply":false,"why":"对方只是知会，无新问题","replies":[]}')
    assert out["should_reply"] is False
    assert out["replies"] == []
    assert out["why"] == "对方只是知会，无新问题"


def test_suggest_returns_empty_when_model_says_no_reply(monkeypatch):
    monkeypatch.setattr(ra, "available", lambda: (True, ""))
    monkeypatch.setattr(ra, "_ensure_server", lambda: 1234)
    monkeypatch.setattr(
        ra, "_chat",
        lambda *a, **k: '{"should_reply":false,"why":"对方只是知会","replies":[]}',
    )
    out = ra.suggest([{"kind": "text", "text": "我回去了"}], "117", "left")
    assert out["replies"] == []          # 不再拿「收到/哈哈哈」凑数
    assert out["note"] == "对方只是知会"


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


# ---------------------------------------------------------------- 与实时变声互斥（2026-10-25）
# 2026-10-25 本机实测：9B 起来后整卡只剩 ~780MB，而 RVC 峰值 1372MB、预检阈值 2048MB。
# 换 4B 时代「4B+RVC 还能剩 1040MB」的数据不可外推（4B 全量才 4.1G）。
# 这一节锁住两件事：RVC 在跑时**不许**拉起模型；RVC 要起时**必须**先让模型让路。


def test_realtime_busy_detects_running_rvc(monkeypatch):
    import rvc_live

    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: True)
    note = ra._realtime_busy()
    assert "实时变声" in note          # 给用户看的原因，不能是空串


def test_realtime_busy_is_empty_when_rvc_idle(monkeypatch):
    import rvc_live

    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: False)
    assert ra._realtime_busy() == ""


def test_realtime_busy_swallows_probe_errors(monkeypatch):
    """探测失败 ≠ 忙：一次 import/查询异常不该把「怎么回」按钮变成死按钮。"""
    import rvc_live

    def boom():
        raise RuntimeError("UIA 挂了")

    monkeypatch.setattr(rvc_live, "_live_proc_alive", boom)
    assert ra._realtime_busy() == ""


def test_suggest_degrades_to_template_when_realtime_running(monkeypatch):
    monkeypatch.setattr(ra, "_realtime_busy", lambda: "实时变声正在运行（显存让它先占着）")
    monkeypatch.setattr(
        ra, "_ensure_server",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("RVC 在跑时不许拉起模型")),
    )
    out = ra.suggest(MSGS, "117", "left")
    assert out["engine"] == "rule"
    assert "实时变声" in out["note"]
    assert out["replies"]              # 仍要给出能点的东西


def test_suggest_one_skips_instead_of_sending_template(monkeypatch):
    """托管是真发：没有模型就不发，不能拿「收到/哈哈哈」顶出去。"""
    monkeypatch.setattr(ra, "_realtime_busy", lambda: "实时变声正在运行")
    monkeypatch.setattr(
        ra, "_ensure_server",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该拉起模型")),
    )
    assert ra.suggest_one(MSGS, "117", "left") is None


def test_release_reply_model_unloads_when_running(monkeypatch):
    import rvc_live

    calls = {"shutdown": 0}
    monkeypatch.setattr(ra, "is_running", lambda: True)
    monkeypatch.setattr(ra, "shutdown", lambda: calls.__setitem__("shutdown", calls["shutdown"] + 1))
    assert rvc_live._release_reply_model() is True
    assert calls["shutdown"] == 1


def test_release_reply_model_noop_when_idle(monkeypatch):
    import rvc_live

    calls = {"shutdown": 0}
    monkeypatch.setattr(ra, "is_running", lambda: False)
    monkeypatch.setattr(ra, "shutdown", lambda: calls.__setitem__("shutdown", calls["shutdown"] + 1))
    assert rvc_live._release_reply_model() is False
    assert calls["shutdown"] == 0


def test_release_reply_model_never_raises(monkeypatch):
    """释放失败只记日志 —— 不能因为它把变声拦住。"""
    import rvc_live

    def boom():
        raise RuntimeError("进程表读不到")

    monkeypatch.setattr(ra, "is_running", boom)
    assert rvc_live._release_reply_model() is False


def test_rvc_start_releases_reply_model_before_vram_precheck(monkeypatch):
    """起实时变声时必须**先**让回复模型让路，再读显存余量。

    顺序不是细节：预检读的是「让路之后」的余量。先检查再卸载，等于拿旧数字做决定 ——
    这正是 2026-10-25 之前漏掉的那一步（9B 驻留时余量 ~2.7G > 2048 阈值 → 放行 →
    RVC 起来只剩 ~1.4G，预检想拦的那种 OOM 照样发生）。
    """
    import cascade
    import pytest
    import rvc_live
    from fastapi import HTTPException

    order = []
    monkeypatch.setattr(rvc_live.live_settings, "get", lambda: {"perf_profile": "balanced"})
    monkeypatch.setattr(rvc_live, "_sync_worker_for_profile", lambda profile: False)
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: False)
    monkeypatch.setattr(rvc_live, "_model_status", lambda *a, **k: True)
    monkeypatch.setattr(rvc_live, "RUNTIME_PY", Path(__file__))
    monkeypatch.setattr(rvc_live, "_release_reply_model", lambda: order.append("release") or True)
    monkeypatch.setattr(rvc_live, "_vram_precheck", lambda: order.append("precheck") or "余量不够")
    monkeypatch.setattr(cascade, "_cascade_alive", lambda: False)

    with pytest.raises(HTTPException) as ei:
        rvc_live.rvc_live_start()
    assert ei.value.status_code == 409
    assert order == ["release", "precheck"]
    assert "已卸载本地回复模型" in ei.value.detail   # 让路成功但还不够，要如实说
