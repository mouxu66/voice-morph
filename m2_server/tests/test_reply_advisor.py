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
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64,QUJD"

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
