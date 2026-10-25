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
    assert "在的怎么了" in q
    # 最后一条是我发的：唯一正确的建议是「不用追」
    assert ra._rule_replies([{"kind": "text", "text": "吃了吗"}], "right") == ["等对方回就行"]


def test_rule_replies_uses_user_own_phrases_not_boilerplate():
    """兜底文案原来是「收到 / 哈哈哈 / 好嘞」—— 这三个词用户一次都没说过（第 209 条）。

    模型挂掉已经够糟了，不该再让他看到三句一眼不是自己说的话。
    """
    out = ra._rule_replies([{"kind": "text", "text": "不服来干我"}], None)
    assert out == ["666", "[奸笑]"]
    assert not any(w in x for x in out for w in ("收到", "哈哈", "好嘞"))


def test_suggest_falls_back_when_model_missing(monkeypatch):
    monkeypatch.setattr(ra, "available", lambda: (False, "没找到本地模型"))
    out = ra.suggest(MSGS, "117")
    assert out["engine"] == "rule"
    assert out["note"] == "没找到本地模型"
    assert out["replies"]


def test_suggest_parses_model_output(monkeypatch):
    monkeypatch.setattr(ra, "available", lambda: (True, ""))
    monkeypatch.setattr(ra, "_ensure_server", lambda: 8099)
    # 样例不能用「哈哈」「收到」：那两个词用户本人 0 次说过，会被 looks_like_offstyle
    # 过滤掉（2026-10-25），这个测试验的是**解析**而不是过滤，所以换成他真会说的词。
    monkeypatch.setattr(
        ra, "_chat", lambda port, prompt, timeout, **kw: '{"why":"他想聊","replies":["行","666"]}'
    )
    out = ra.suggest(MSGS, "117", "left")
    assert out["engine"] == "local"
    assert out["replies"] == ["行", "666"]
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
    monkeypatch.setattr(ra, "_chat", lambda port, prompt, timeout, **kw: "我觉得你应该先回一句")
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


# ---------------------------------------------------------------- 视觉（mmproj / 截图）
# 9B 是原生多模态，缺的只是 ~918MB 投影器；4B 的投影器是 2560 维，**接不到 9B 的 4096**。


def test_find_mmproj_matches_by_model_name(monkeypatch, tmp_path):
    fake = tmp_path / "mmproj-9b.gguf"
    fake.write_bytes(b"x")
    monkeypatch.delenv(ra.ENV_MMPROJ, raising=False)
    monkeypatch.setattr(ra, "MMPROJ_BY_MODEL", (("qwen3.5-9b", str(fake)),))
    assert ra._find_mmproj(r"D:\llama-b9660\Qwen3.5-9B-Q4_K_M.gguf") == str(fake)
    assert ra._find_mmproj(r"D:\Qwen3VL-4B-Instruct-Q4_K_M.gguf") is None


def test_find_mmproj_empty_env_forces_text_only(monkeypatch, tmp_path):
    fake = tmp_path / "mm.gguf"
    fake.write_bytes(b"x")
    monkeypatch.setattr(ra, "MMPROJ_BY_MODEL", (("qwen3.5-9b", str(fake)),))
    monkeypatch.setenv(ra.ENV_MMPROJ, "")
    assert ra._find_mmproj(r"D:\Qwen3.5-9B-Q4_K_M.gguf") is None


def test_needs_look_only_when_media_in_tail():
    text_only = [{"kind": "text", "text": "在吗"}] * 6
    assert ra._needs_look(text_only) is False
    with_media = [{"kind": "text", "text": "6666"}, {"kind": "media", "text": "图片"}]
    assert ra._needs_look(with_media) is True
    # 图片在很前面（窗口外）就不截图 —— 那多半已经聊过去了
    old_media = [{"kind": "media", "text": "图片"}] + [{"kind": "text", "text": "嗯"}] * 6
    assert ra._needs_look(old_media, window=6) is False


def test_chat_sends_image_parts_only_when_given(monkeypatch):
    import json as _json

    seen = {}

    def _spy(req, timeout=None):
        seen.update(_json.loads(req.data.decode()))
        return _fake_urlopen({"choices": [{"message": {"content": "好"}}]})(req)

    monkeypatch.setattr(ra.urllib.request, "urlopen", _spy)
    ra._chat(1234, "提示", 5.0, image_b64="QUJD")
    parts = seen["messages"][1]["content"]
    assert isinstance(parts, list) and parts[0]["type"] == "text"
    # mime 按 magic bytes 判：`iVBORw0KGgo` 才是 PNG，`/9j/` 才是 JPEG。
    # 以前这里写死 image/jpeg，靠 llama.cpp 自己嗅探才没炸 —— 换个解码器就 400。
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,QUJD"

    ra._chat(1234, "提示", 5.0, image_b64="/9j/4AAQSkZJRg")
    assert seen["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;")

    ra._chat(1234, "提示", 5.0)
    assert seen["messages"][1]["content"] == "提示"      # 没图时仍是纯字符串


def test_chat_snapshot_returns_none_when_window_missing(monkeypatch):
    import chat_radar.capture as cap

    monkeypatch.setattr(cap, "capture_frame", lambda *a, **k: None)
    assert ra.chat_snapshot_b64() is None


def test_system_forbids_unverified_assertions():
    """「确实没录」这类断言和「我帮你买」一样危险 —— 都是替用户说没核实过的事。"""
    assert "不要替用户断言" in ra._SYSTEM
    assert "不确定就写成问一句" in ra._SYSTEM


def test_mmproj_follows_free_vram_by_default(monkeypatch, tmp_path):
    """视觉塔默认 auto：够显存就上 GPU（看图 7.2s→1.1s），不够才留 CPU。

    永远留 CPU 是旧结论：那是 c=24576 时量的（KV 多吃 3~4G 才溢出），产品只跑 c=2048。
    """
    fake_mm = tmp_path / "mm.gguf"
    fake_mm.write_bytes(b"x")
    monkeypatch.setattr(ra, "_find_exe", lambda: "llama-server.exe")
    monkeypatch.setattr(ra, "_find_model", lambda: r"D:\Qwen3.5-9B-Q4_K_M.gguf")
    monkeypatch.setattr(ra, "_find_mmproj", lambda m=None: str(fake_mm))
    monkeypatch.setattr(ra, "_health", lambda port: True)
    monkeypatch.delenv(ra.ENV_MMPROJ_CPU, raising=False)

    captured = {}

    class _P:
        returncode = None

        def poll(self):
            return None

    def _popen(cmd, **kw):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr(ra.subprocess, "Popen", _popen)

    def _start() -> list[str]:
        captured.clear()
        try:
            ra._start_locked(9999)
        finally:
            ra._PROC = None
        return captured["cmd"]

    # auto + 显存够 → 视觉塔上 GPU，不加 flag
    monkeypatch.setattr(ra, "_vram_free_mib", lambda: ra.MMPROJ_NEED_MIB)
    assert "--no-mmproj-offload" not in _start()

    # auto + 显存紧（用户开着游戏）→ 退回 CPU，不赌
    monkeypatch.setattr(ra, "_vram_free_mib", lambda: ra.MMPROJ_NEED_MIB - 1)
    assert "--no-mmproj-offload" in _start()

    # 量不出显存（无独显/超时）→ 保守留 CPU
    monkeypatch.setattr(ra, "_vram_free_mib", lambda: None)
    assert "--no-mmproj-offload" in _start()

    # 显式开关优先于现场判断
    monkeypatch.setattr(ra, "_vram_free_mib", lambda: 99_999)
    monkeypatch.setenv(ra.ENV_MMPROJ_CPU, "1")
    assert "--no-mmproj-offload" in _start()

    monkeypatch.setattr(ra, "_vram_free_mib", lambda: 0)
    monkeypatch.setenv(ra.ENV_MMPROJ_CPU, "0")
    assert "--no-mmproj-offload" not in _start()


def _capture_start(monkeypatch, tmp_path):
    """跑一次 _start_locked，把 Popen 收到的 cmd 拿回来。"""
    fake_mm = tmp_path / "mm.gguf"
    fake_mm.write_bytes(b"x")
    monkeypatch.setattr(ra, "_find_exe", lambda: "llama-server.exe")
    monkeypatch.setattr(ra, "_find_model", lambda: r"D:\Ornith-1.5-9B-Q4_K_M.gguf")
    monkeypatch.setattr(ra, "_find_mmproj", lambda m=None: str(fake_mm))
    monkeypatch.setattr(ra, "_health", lambda port: True)
    monkeypatch.setattr(ra, "_vram_free_mib", lambda: 99_999)
    captured = {}

    class _P:
        returncode = None

        def poll(self):
            return None

    def _popen(cmd, **kw):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr(ra.subprocess, "Popen", _popen)

    def _start():
        captured.clear()
        try:
            ra._start_locked(9999)
        finally:
            ra._PROC = None
        return captured["cmd"]

    return _start


def test_start_uses_8192_with_kv_quant(monkeypatch, tmp_path):
    """context 上到 8192 **必须**配套 KV q8_0。

    2026-10-26 实测（同一张 2560×1600、同一套 prompt，只改这两个参数）：
        c=6144  f16 KV  → 14.8s
        c=8192  f16 KV  → 180s（两次复现，不报错、只是静默分页）
        c=8192  q8_0 KV → 13~20s，峰值显存还更低（KV 768→512 MiB）
    所以这两个参数是一对，拆开就等于把面板从 15 秒拖到 3 分钟。
    """
    monkeypatch.delenv(ra.ENV_CTX, raising=False)
    monkeypatch.delenv(ra.ENV_KV, raising=False)
    cmd = _capture_start(monkeypatch, tmp_path)()
    assert cmd[cmd.index("-c") + 1] == "8192"
    assert cmd[cmd.index("-ctk") + 1] == "q8_0"
    assert cmd[cmd.index("-ctv") + 1] == "q8_0"
    # 老版 llama-server 把 -fa 当开关，多给一个值会启动失败 —— 不要自己加
    assert "-fa" not in cmd


def test_kv_quant_can_be_turned_off(monkeypatch, tmp_path):
    """VM_REPLY_KV=f16 时退回不量化（老显卡/老 llama-server 的退路）。"""
    monkeypatch.setenv(ra.ENV_KV, "f16")
    cmd = _capture_start(monkeypatch, tmp_path)()
    assert "-ctk" not in cmd and "-ctv" not in cmd


def test_ctx_override_still_wins(monkeypatch, tmp_path):
    """VM_REPLY_CTX 仍然优先于默认的 8192（用户自己要降回去时）。"""
    monkeypatch.setenv(ra.ENV_CTX, "4096")
    cmd = _capture_start(monkeypatch, tmp_path)()
    assert cmd[cmd.index("-c") + 1] == "4096"


def test_system_says_praise_is_about_someone_else():
    """群里刷「高手」「佩服」是在夸**别的群友**，不是在夸用户。

    2026-10-25 实测：只说「不要道谢道贺」时，纯夸赞刷屏 3 次里有 2 次回出
    「哈哈过奖啦」「谢谢，一起冲呀」—— 用户点一下就在群里莫名道谢。
    改写成针对性规则后 3/3 正确（不回或跟着夸）。
    """
    assert "不是在夸你" in ra._SYSTEM
    assert "过奖" in ra._SYSTEM
    # 但不能写成「遇到夸就别回」：实测那样会把「高手/佩服」后面的吐槽、问事一起压掉
    assert "跟着夸一句" in ra._SYSTEM
    assert "优先接那个" in ra._SYSTEM


# ---- 2026-10-25：3864 群 4363 条真实语料上的「替你答应/复述别人」回归 ----
# 40 个真实回话时机、每类重跑 3 次的实测结论：旧规则只列了「买/送/取」这类跑腿动词，
# 群聊里真正高频的是**接活和排计划**，全部 3/3 稳定漏过。下面这些句子都来自真实输出。

import pytest  # noqa: E402


@pytest.mark.parametrize("text", [
    "那我先刷王城，你打熊我记下了",      # 替你分工
    "周五熊我打，你安心搬炉子",          # 替你排日程
    "行，明早我准时上线挖",              # 替你承诺上线
    "收到，我报名",                      # 替你报名
    "五毛就五毛，周二那车我帮你打",      # 接活（旧规则漏）
    "这攻略不错，我明天就试试",          # 时间词在「我」后面（旧规则漏）
    "行啊，那你先忙，王城我慢慢刷",      # 「慢慢」也是排计划
    "好嘞，马上安排",                    # 没有「我」也是点头
    "[得意] 那熊归你，峡谷我扛着",        # 揽活
])
def test_promise_catches_task_commitment(text):
    assert ra.looks_like_promise(text), text


@pytest.mark.parametrize("text", [
    "继续啊，点左下那个",
    "全栈那不得秃头",
    "这算盘打得我在隔壁都听见了",
    "驻防呢？谁在防？",
    "燃霜应该还有，具体看公告",
    "手册换火晶看你要哪个，缺哪个就换哪个",
    "哈哈你这号谁登的",
])
def test_promise_does_not_kill_normal_replies(text):
    assert not ra.looks_like_promise(text), text


def test_echo_detects_repeating_others_first_person():
    """对方说「我控号都给驻防」，模型给用户的候选是同一句 —— 点发就成了用户认领别人的话。"""
    ctx = [{"side": "left", "text": "我控号都给驻防"}]
    assert ra.echoes_context("我控号只给驻防，没动你号", ctx)
    assert ra.echoes_context("我控号都给驻防，没动你号哈", ctx)


def test_echo_ignores_at_mention_of_others():
    """「巴黎才下课吗」被回复成「@巴黎才下课吗 我没叫你打乔伊呀」是正常接话，不能判成抄。

    这条是实测误杀过的：按子串判把一句跟用户真人原话几乎一样的候选拦掉了。
    现在只检对方句子里带「我」的（那才是「我指向谁」的坑）。
    """
    ctx = [{"side": "left", "text": "巴黎才下课吗"}]
    assert not ra.echoes_context("@巴黎才下课吗 我没叫你打乔伊呀，我也没开电脑", ctx)


def test_system_has_no_copyable_examples():
    """提示词里不许举具体句子当例子：写过「这运气绝了」之后，40 条采样里 4 条原样抄。"""
    assert "这运气绝了" not in ra._SYSTEM
    assert "太离谱了吧" not in ra._SYSTEM


def test_system_says_model_does_not_know_user_situation():
    """最致命的一类不是答得差，是编用户的处境（在不在家/号里有什么/打到多少分）。"""
    assert "不知道用户的情况" in ra._SYSTEM
    assert "不许写用户的具体安排" in ra._SYSTEM


# ---- 2026-10-25：按用户本人 474 条发言量出来的「风格指纹」----
# 需求原话：「让他知道我的语言风格，但不需要完全照抄」。所以不做微调，
# 而是把人肉量出来的统计写进提示词 + 输出侧过滤。下列断言锁的就是这份量出来的事实。

import pytest  # noqa: E402


def test_system_demands_short_length_and_no_punctuation():
    """中位 8 字、88% 完全无标点 —— 模型原来写带逗号的完整句，一眼就不是本人。"""
    assert "多数 3~10 个字" in ra._SYSTEM
    assert "不要句号、逗号、叹号" in ra._SYSTEM
    assert "几乎不打标点" in ra._SYSTEM


def test_system_bans_words_user_never_says():
    """474 条发言里「哈哈」「收到」「好嘞」「嗯」「哦」「牛」「厉害」「确实」全部 0 次。

    模型却满屏都是，这是「看着不像本人」的最大来源，比长度问题更致命。
    """
    for w in ("「哈哈」", "「收到」", "「好嘞」", "「嗯」", "「哦」"):
        assert w in ra._SYSTEM, w
    assert "用户从来不这么说" in ra._SYSTEM


def test_system_keeps_666_because_user_actually_says_it():
    """「666」是他开头词第一名（20 次）—— 允许举例，因为抄了也是他的真话。

    反过来说：提示词里能写具体词的唯一标准就是「用户真的这么说」，
    模型自己编的通顺句（第 205 条的「这运气绝了」）一律不许写。
    """
    assert "666" in ra._SYSTEM


def test_system_requires_staggered_lengths():
    """三条候选长度错开，否则用户看到的是三句一样长的话。"""
    assert "长度要错开" in ra._SYSTEM
    assert "五个字以内" in ra._SYSTEM


@pytest.mark.parametrize("text", [
    "动画表情", "图片", "视频", "动画表情 3张", "语音",
])
def test_placeholder_output_is_dropped(text):
    """模型会把微信占位符当正文给出来 —— 2026-10-25 实测 #27 直接给「动画表情」，
    用户点「发送」就真的在群里发出这四个字。"""
    assert ra.is_placeholder(text), text


@pytest.mark.parametrize("text", ["[奸笑]", "[旺柴]", "[得意]", "行", "666"])
def test_real_emoji_names_survive_placeholder_filter(text):
    assert not ra.is_placeholder(text), text


@pytest.mark.parametrize("text", [
    "收到", "好嘞", "嗯", "哦", "哈哈", "哈哈哈", "我看看", "我试试",
    "确实", "在呢", "等下", "明白了", "收到！",
])
def test_offstyle_boilerplate_is_dropped(text):
    """用户本人 0 次说过的套话，整条只剩它时丢弃。"""
    assert ra.looks_like_offstyle(text), text


@pytest.mark.parametrize("text", [
    "好的", "可以", "行", "666", "[奸笑]", "我靠", "笑死",
    "哈哈哈这波血赚",         # 套话后面有实质内容，不能砍
    "收到晚上挖矿",
    "我看看规则吧",
])
def test_offstyle_does_not_kill_meaningful_replies(text):
    assert not ra.looks_like_offstyle(text), text


@pytest.mark.parametrize("text", [
    "我这就来", "在呢马上", "行 我摆着", "那我先囤着",
    "收到 你打熊我盯会战", "驻防我盯着", "@某人 我这就来",
])
def test_short_sentence_promises_are_caught(text):
    """把回复改短（中位 5 字）后承诺变得**更难**抓：短句里「我+动作」几乎只有一种含义。

    这 6 条全部通过了旧规则（旧动词表里没有 来/摆/囤/盯）。
    """
    assert ra.looks_like_promise(text), text


@pytest.mark.parametrize("text", [
    "真的假的，我也以为不放了",   # 「我…放…了」曾因跨过否定词被误杀
    "这奖励真香，那我就不摆啦",
    "我明天不上线了",
    "我没摆啊",
    "明天不打了",
])
def test_promise_rule_does_not_cross_negation(text):
    """规则里 `[^，。！？]{0,4}` 会把中间的「不/没/别」一并吃掉，导致拒绝被当成承诺。

    2026-10-25 实测误杀过「真的假的，我也以为不放了」和「那我就不摆啦」，
    改成 `[^，。！？不没别]{0,2}` 之后两者都放行、真承诺仍照拦。
    """
    assert not ra.looks_like_promise(text), text


# ---- 2026-10-25：为什么记录**不能**标出「谁说的」（两次尝试都回退了）----

def test_prompt_keeps_single_timeline_without_side_prefix():
    """不许写行内「我：/对方：」（2026-10-24 回退）：前缀会被抄进正文。

    也不许拆成「【对方说的】/【用户自己已经发过的】」两块（2026-10-25 回退）：
    40 个真实时机 A/B 结果是净亏 —— 禁词 1/36 → 3/39（冒出「哈哈谢谢啊」
    「确实是我大号」「确实红姜惨」），该「不回」的从 4 条掉到 1 条，其余持平。
    """
    msgs = [
        {"side": "right", "text": "码发给我"},
        {"side": "left", "text": "2561"},
        {"side": "left", "text": "2562"},
    ]
    p = ra.build_prompt(msgs, chat="117", last_side="left")
    assert "我：码发给我" not in p
    assert "对方：2561" not in p
    assert "【对方说的】" not in p
    assert "【用户自己已经发过的】" not in p
    # 单一时间线：三句都在，顺序不变
    assert p.index("码发给我") < p.index("2561") < p.index("2562")


def test_prompt_only_hints_who_spoke_last():
    """方向提示只给一句（最后一条是谁说的）——这是分不清发言人时唯一可靠的信号。"""
    theirs = ra.build_prompt([{"side": "left", "text": "2561"}], chat="117", last_side="left")
    mine = ra.build_prompt([{"side": "right", "text": "码发给我"}], chat="117", last_side="right")
    assert "最后一条是对方发的" in theirs
    assert "最后一条是用户自己发的" in mine


def test_prompt_drops_time_rows():
    p = ra.build_prompt(
        [{"side": "left", "kind": "time", "text": "18:48"}, {"side": "left", "text": "2561"}],
        chat="117",
    )
    assert "18:48" not in p
    assert "2561" in p


@pytest.mark.parametrize("text,expect", [
    ("【对方说的】行", "行"),
    ("对方说的：行", "行"),
    ("用户：行", "行"),
    ("我：行", "行"),
    ("1. 行", "行"),
    ("1. 【对方说的】行", "行"),
    ("建议你回一句", "建议你回一句"),   # 没有标签，不要乱剥
    ("我靠", "我靠"),                   # 「我」后面不是冒号，别动
    ("对方在吗", "对方在吗"),
])
def test_strip_section_label(text, expect):
    """模型会把提示词里的标题当成正文抄出来（和第 203 条的「我：」是同一个坑）。

    清在输出侧：提示词拦不住，但剥标题是确定性操作。
    """
    assert ra.strip_section_label(text) == expect


def test_parsed_replies_are_label_free():
    raw = '{"why":"问码","replies":["【对方说的】2561","我：发你","行"]}'
    out = ra.parse_reply_json(raw)
    assert out["replies"] == ["2561", "发你", "行"]


# ---- 2026-10-25：117 群真实模拟暴露的三件事 ----

def test_suggest_does_not_call_model_when_user_spoke_last(monkeypatch):
    """最后一条是用户自己发的 → 直接说「不用接」，**不调模型**。

    117 群实测：用户刚发完「码发给我 @瑜清」，点「怎么回」得到
    「在的，地址发你了」「地址给你」—— 整段记录里根本没有「地址」这两个字。
    模型被问了就一定要编一句，所以这道题不该问它。
    """
    monkeypatch.setattr(ra, "available", lambda: (True, ""))

    def boom(*a, **kw):
        raise AssertionError("最后一条是自己发的，不该拉起模型")

    monkeypatch.setattr(ra, "_ensure_server", boom)
    out = ra.suggest([{"side": "left", "text": "带一份"}, {"side": "right", "text": "码发给我"}],
                     chat="117", last_side="right")
    assert out["replies"] == []
    assert "不用接" in out["note"]
    assert out["elapsed_ms"] >= 0


def test_suggest_infers_last_side_when_caller_forgets(monkeypatch):
    """前端可能不传 last_side（/replies 注释写着「不给也行」）—— 后端自己推。"""
    monkeypatch.setattr(ra, "available", lambda: (True, ""))
    monkeypatch.setattr(ra, "_ensure_server", lambda: (_ for _ in ()).throw(AssertionError("不该调模型")))
    out = ra.suggest([{"side": "left", "text": "带一份"}, {"side": "right", "text": "码发给我"}], chat="117")
    assert out["replies"] == []


@pytest.mark.parametrize("text", ["发你了", "发你", "转你了", "给你发了", "@瑜清 发你了"])
def test_claims_done_is_dropped(text):
    """声称「我已经发给你了」是**完成态的假话**，和承诺（将来时）不是一回事。

    117 群实测三次里两次给「发你了」，而用户一个字都没发出去。
    """
    assert ra.claims_done(text), text


@pytest.mark.parametrize("text", [
    "给我看看", "发我", "你发码", "码发我", "你发给我", "这个给你", "辛苦你了", "给你点赞",
])
def test_claims_done_does_not_kill_requests(text):
    """「发我」「码发我」是**要东西**，不是声称已给 —— 别拦。"""
    assert not ra.claims_done(text), text


def test_drop_promises_removes_claims_done():
    out = ra.drop_promises(["发你了", "码发我", "666"])
    assert out == ["码发我", "666"]


# ---- 2026-10-25：意图分级 —— 到底该不该由 AI 给正文 ----

#: 顶替模型的最小夹具：让 suggest() 走到「拿到 JSON」那一步，但一个 token 都不真跑。
def _fake_model(monkeypatch, payload: str, replies=None):
    monkeypatch.setattr(ra, "_realtime_busy", lambda: "")
    monkeypatch.setattr(ra, "available", lambda: (True, ""))
    monkeypatch.setattr(ra, "_ensure_server", lambda: 1)
    monkeypatch.setattr(ra, "_needs_look", lambda *a, **kw: False)
    monkeypatch.setattr(ra, "_chat", lambda *a, **kw: payload)


def _ctx(*texts, side="left"):
    return [{"side": side, "text": t, "kind": "text"} for t in texts]


@pytest.mark.parametrize("text", [
    "码发给我", "你地址发我", "链接给我", "截图发一下", "账号给我", "收款码发我",
])
def test_intent_need_you_when_asking_for_things(text):
    """对方在要东西 → 不给正文。

    117 群实测：对方要收款码，模型三条「发你了 / 码给你 / @你 我发你了」，
    用户一个字节都没发出去。这类错不是措辞不好，是替用户声称做过一件事。
    """
    out = ra.classify_intent(_ctx(text))
    assert out["intent"] == "need_you", text
    assert out["block"] is True, text
    assert "只有你能给" in out["gap"]


@pytest.mark.parametrize("text", [
    "你今晚打不打", "你几点上线", "你几级了", "你号还在不", "你有空吗", "你明天来不来",
])
def test_intent_need_you_when_asking_user_state(text):
    """问用户自己的处境 → 同样不给正文（答案只在他脑子里）。"""
    out = ra.classify_intent(_ctx(text))
    assert out["intent"] == "need_you", text
    # 这条不 block：候选里要是有句纯表态（「行」），还能放行
    assert out["block"] is False, text


def test_intent_lets_pure_stance_through():
    """嘴里已经有一句「行」就够答了 —— 表态不需要知道细节。"""
    out = ra.classify_intent(_ctx("你今晚打不打"), ["行", "在的怎么了"])
    assert out["intent"] == "direct"


@pytest.mark.parametrize("text", [
    "无尽冬日阵容怎么配", "茉莉值得练吗", "盾矛弓配比多少", "打熊怎么打", "专武升到几",
])
def test_intent_need_info_when_public_knowledge(text):
    """公共知识（攻略/数值）→ 不编，给「查攻略」入口。

    与 need_you 的分界：攻略搜得到，用户的处境搜不到。
    """
    out = ra.classify_intent(_ctx(text))
    assert out["intent"] == "need_info", text
    assert out["can_search"] is True


@pytest.mark.parametrize("text", ["哈哈哈哈哈", "6666", "动画表情", "[奸笑]"])
def test_intent_ack_for_noise(text):
    out = ra.classify_intent(_ctx("行", text))
    assert out["intent"] == "ack", text


def test_intent_direct_for_ordinary_question():
    out = ra.classify_intent(_ctx("点哪个", "3000还是600"))
    assert out["intent"] == "direct"


def test_intent_reads_two_messages_not_just_the_last():
    """群聊一句话常被拆成几条，最后一条往往是收尾词。

    2026-10-25 拿 3864 群 40 个真实时机只看最后一条 → 37/40 判成 ack，
    而真正的问题（「点哪个」「3000还是600」）就在前一条。
    """
    out = ra.classify_intent(_ctx("点哪个", "左下"))
    assert out["intent"] == "direct"
    assert out["asked"] == "左下"          # 提示用的是最后一条，判据用两条


def test_suggest_does_not_run_model_when_asking_for_things(monkeypatch):
    """要东西这类连模型都不该拉起来 —— 拉起来也只有「发你了」。"""
    def boom(*a, **kw):
        raise AssertionError("对方在要东西，不该调模型")

    monkeypatch.setattr(ra, "_realtime_busy", lambda: "")
    monkeypatch.setattr(ra, "available", lambda: (True, ""))
    monkeypatch.setattr(ra, "_ensure_server", boom)
    out = ra.suggest(_ctx("带一份", "码发给我"), chat="117")
    assert out["replies"] == []
    assert out["intent"] == "need_you"
    assert "码" in out["gap"]


def test_suggest_clears_replies_when_public_knowledge(monkeypatch):
    """攻略类问题：模型给了候选也要清掉 —— 那几条里的数字都是编的。"""
    _fake_model(monkeypatch,
                '{"should_reply":true,"why":"对方在问阵容",'
                '"replies":["茉莉先练","赫克托毕业盾","我看看"]}')
    out = ra.suggest(_ctx("无尽冬日阵容怎么配"), chat="3864")
    assert out["replies"] == []
    assert out["intent"] == "need_info"
    assert out["can_search"] is True
    assert "查攻略" in out["note"]


def test_suggest_keeps_replies_for_direct(monkeypatch):
    _fake_model(monkeypatch,
                '{"should_reply":true,"why":"在问选哪个","replies":["3000","最低那个","两个都行吧"]}')
    out = ra.suggest(_ctx("点哪个", "3000还是600"), chat="3864")
    assert out["replies"] == ["3000", "最低那个", "两个都行吧"]
    assert out["intent"] == "direct"


# ---- 攻略检索：拿到资料之后重写一句 ----

def test_suggest_from_digest_drops_copied_titles(monkeypatch):
    """搜索结果的标题被整句搬进回复是最常见的失败形态 —— 长度语体都不对。"""
    _fake_model(monkeypatch,
                '{"should_reply":true,"why":"照资料说",'
                '"replies":["无尽冬日英雄搭配攻略","茉莉先拉到4星","盾矛弓4比1比5"]}')
    digest = {"items": [{"title": "无尽冬日英雄搭配攻略", "snippet": "茉莉 4 星够用"}]}
    out = ra.suggest_from_digest(_ctx("阵容怎么配"), digest, chat="3864")
    assert "无尽冬日英雄搭配攻略" not in out["replies"]
    assert "茉莉先拉到4星" in out["replies"]
    assert out["intent"] == "need_info"


def test_suggest_from_digest_without_items_does_not_call_model(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("没搜到东西不该调模型")

    monkeypatch.setattr(ra, "_ensure_server", boom)
    out = ra.suggest_from_digest(_ctx("阵容怎么配"), {"items": []}, chat="3864")
    assert out["replies"] == []
    assert "没搜到" in out["note"]


def test_digest_prompt_never_leaks_urls():
    """URL 会被模型抄进回复发出去 —— 给模型的资料里不能有。"""
    from web_digest import digest_prompt

    text = digest_prompt(
        {"items": [{"title": "攻略", "snippet": "茉莉4星", "url": "https://x.com/a"}]}, []
    )
    assert "http" not in text
    assert "茉莉4星" in text


def test_repeats_self_catches_own_last_line():
    """117 群真实翻车：用户刚说「码发给我」，候选里又出现「码发我」。

    点下去就是把刚打的字再打一遍。这条纯字符串比对，不需要语义。
    """
    msgs = [
        {"side": "left", "text": "谁有码"},
        {"side": "right", "text": "码发给我"},
    ]
    assert ra.repeats_self("码发我", msgs) is False   # 不完全同句，放行
    assert ra.repeats_self("码发给我", msgs) is True


def test_repeats_self_ignores_punctuation_and_at():
    msgs = [{"side": "right", "text": "@瑜清 码发给我！"}]
    assert ra.repeats_self("码发给我", msgs) is True


def test_repeats_self_ignores_other_side():
    """别人说的话不算「自己说过」—— 那是 echoes_context 的活。"""
    msgs = [{"side": "left", "text": "码发给我"}]
    assert ra.repeats_self("码发给我", msgs) is False


def test_repeats_self_skips_tiny_lines():
    """两个字以下不拦：「嗯」「好」本来就该能重复。"""
    msgs = [{"side": "right", "text": "嗯"}]
    assert ra.repeats_self("嗯", msgs) is False


def test_drop_promises_removes_self_echo():
    msgs = [{"side": "right", "text": "码发给我"}]
    assert ra.drop_promises(["码发给我", "666"], msgs) == ["666"]


# ---- 看图链路：advice 是「给本人看的」，与 replies 规则相反 -------------------

def test_clean_advice_keeps_full_sentences():
    """advice 允许长、允许标点、允许数字 —— 它就是给用户看懂用的。"""
    out = ra.clean_advice([
        "南瓜大侵袭要13000积分才打，先攒够再上",
        "最后1天别打，材料攒着别浪费",
    ])
    assert out == ["南瓜大侵袭要13000积分才打，先攒够再上", "最后1天别打，材料攒着别浪费"]


def test_clean_advice_strips_model_numbering():
    """模型爱自己加「1. 」「- 」，那是排版不是内容。"""
    assert ra.clean_advice(["1. 先攒积分", "- 再打包子", "2、看背包"]) == ["先攒积分", "再打包子", "看背包"]


def test_clean_advice_drops_dupes_and_walls():
    """重复的去掉；超过 120 字的是「把整篇攻略抄下来」，不是一条要点。"""
    long = "打" * 130
    assert ra.clean_advice(["先攒积分", "先攒积分", long, ""]) == ["先攒积分"]


def test_clean_advice_caps_at_five():
    assert len(ra.clean_advice([f"要点{i}" for i in range(9)])) == 5


def test_clean_advice_rejects_non_list():
    assert ra.clean_advice("先攒积分") == []
    assert ra.clean_advice(None) == []


def test_parse_reply_json_reads_advice():
    got = ra.parse_reply_json(
        '{"advice":["a1","a2"],"replies":["x","y","z"],"why":"说的这个"}'
    )
    assert got["advice"] == ["a1", "a2"]
    assert got["replies"] == ["x", "y", "z"]


def test_parse_reply_json_accepts_advice_without_replies():
    """只有 advice 也算成功。

    用户点「看图」最想要的往往就是那几条要点（图讲的是排期，不是非接不可的话头），
    这时候一条回复都没写出来是正常的 —— 以前这里会判成「输出非法」退回模板。
    """
    got = ra.parse_reply_json('{"advice":["先攒13000积分","最后一天别打"]}')
    assert got is not None
    assert got["advice"] == ["先攒13000积分", "最后一天别打"]
    assert got["replies"] == []


def test_parse_reply_json_still_fails_when_both_empty():
    assert ra.parse_reply_json('{"replies":[]}') is None


# ---- 视觉入口的守卫 ---------------------------------------------------------

def test_sniff_mime_reads_magic_bytes():
    """mime 按内容判，不按扩展名 —— 硬写 image/jpeg 传 PNG 只是靠解码器嗅探。"""
    assert ra._sniff_mime("iVBORw0KGgoAAAANSUhEUg") == "image/png"
    assert ra._sniff_mime("/9j/4AAQSkZJRg") == "image/jpeg"
    assert ra._sniff_mime("UklGRiQAAABXRUJQ") == "image/webp"
    assert ra._sniff_mime("QUJD") == "image/png"      # 判不出来当 PNG（微信截图都是 PNG）


def test_vision_ready_reports_missing_mmproj(monkeypatch):
    monkeypatch.setattr(ra, "_find_mmproj", lambda *a, **k: None)
    ok, why = ra.vision_ready()
    assert ok is False
    assert "mmproj" in why


def test_vision_ready_true_when_mmproj_present(monkeypatch):
    monkeypatch.setattr(ra, "_find_mmproj", lambda *a, **k: r"D:\mmproj-Ornith.gguf")
    assert ra.vision_ready() == (True, "")


def test_image_payload_shrinks_only_when_too_big(tmp_path):
    """不超限就别缩 —— 这张安排表缩到 1200 宽后小榜活动全读错（2026-10-25 实测）。"""
    from PIL import Image

    small = tmp_path / "s.png"
    Image.new("RGB", (800, 600), "white").save(small)
    b64 = ra.image_payload(str(small), max_side=2400)
    assert len(b64) > 100

    big = tmp_path / "b.png"
    Image.new("RGB", (4000, 3000), "white").save(big)
    b64b = ra.image_payload(str(big), max_side=1200)
    # 缩过之后再解回来量长边：应该压到 1200 以内
    import base64
    import io

    im = Image.open(io.BytesIO(base64.b64decode(b64b)))
    assert max(im.size) <= 1200


def test_clean_advice_keeps_leading_dates():
    """日期开头的要点不许被当成序号吃掉。

    2026-10-25 真机：模型给的五条要点里三条以「10月27日先升巴尔德…」开头，
    旧正则把开头那段数字连同点号一起剥掉，于是输出成「月27日先升…」。
    序号只认 `1.` `1、` `1)` 和项目符号，不认裸数字。
    """
    got = ra.clean_advice([
        "10月27日先升巴尔德1技能再总动员",
        "28号士官计划刷宝石",
        "11月3日最后一天，要拿榜的注意",
    ])
    assert got[0].startswith("10月27日")
    assert got[1].startswith("28号")
    assert got[2].startswith("11月3日")


def test_clean_advice_still_strips_real_numbering():
    assert ra.clean_advice(["1.先攒", "2、再打", "3) 最后看背包"]) == ["先攒", "再打", "最后看背包"]
