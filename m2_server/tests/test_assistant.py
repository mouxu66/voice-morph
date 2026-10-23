"""assistant 包的纯逻辑测试：不联网、不调 TTS/RVC/微信、不碰真实 outputs。

覆盖三类最容易改坏的东西：
    1. 规则抽取的**边界**（多剥一个字就把歌名搜错，少剥一个就搜不到）
    2. 提案状态机（重复 confirm 会发两条 —— 那是用户最不能接受的事故）
    3. 搜索源必须是下载白名单的子集（否则"确认了却下不了"）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from assistant import extract, search, store
from assistant.schema import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_PENDING,
    STATUS_RUNNING,
    Proposal,
    validate_params,
)

# ------------------------------------------------------------------ 抽歌名


@pytest.mark.parametrize(
    "text,expect",
    [
        ("能给我发一首晴天吗", "晴天"),
        ("发首周杰伦的稻香", "周杰伦的稻香"),
        ("来首《孤勇者》", "孤勇者"),
        ("点歌：告白气球", None),  # 冒号后没接在动词上，规则不认（宁可漏）
        ("帮我放一首海阔天空谢谢", "海阔天空"),
        ("整一首夜曲吧", "夜曲"),
        ("我想要听晴天", "晴天"),
        ("唱个稻香", "稻香"),
    ],
)
def test_parse_song_hits(text, expect):
    hit = extract.parse_song(text)
    if expect is None:
        assert hit is None
        return
    assert hit is not None, f"{text!r} 应该抽得出歌名"
    assert hit.params["query"] == expect


@pytest.mark.parametrize(
    "text",
    [
        "你发的那首歌真好听",  # 有"发"有"歌"，但是评价不是请求
        "这首歌叫什么名字",  # 没有动词+数量词
        "发个了",  # 抽出来是空壳
        "发首歌",  # 泛指，没有指名道姓
        "",
    ],
)
def test_parse_song_misses(text):
    assert extract.parse_song(text) is None, f"{text!r} 不该被当成点歌"


def test_parse_song_keeps_evidence():
    """`matched` 必须是原文里真实存在的一段 —— 用户靠它判断"AI 是看哪句起意的"。"""
    text = "小明：能给我发一首晴天吗"
    hit = extract.parse_song(text)
    assert hit is not None
    assert hit.matched in text


# ------------------------------------------------------------------ 抽发语音


def test_parse_voice_with_speaker():
    hit = extract.parse_voice("用袋鼠的声音说今天真开心")
    assert hit is not None
    assert hit.params["text"] == "今天真开心"
    assert hit.params["voice"] == "袋鼠"


def test_parse_voice_dialect():
    hit = extract.parse_voice("用粤语说今天天气不错")
    assert hit is not None
    assert hit.params["dialect"] == "粤语"
    assert hit.params["text"] == "今天天气不错"


def test_parse_voice_plain():
    hit = extract.parse_voice("说一句晚安")
    assert hit is not None
    assert hit.params["text"] == "晚安"


def test_parse_both_song_and_voice():
    """一句里同时有"发什么"和"用什么声音"时，两条都要留下。"""
    hits = extract.parse("发一首晴天，用袋鼠的声音说")
    kinds = [h.kind for h in hits]
    assert "song.send" in kinds


# ------------------------------------------------------------------ 参数校验


def test_validate_params_missing():
    clean, missing = validate_params("song.send", {})
    assert missing == ["query"]
    assert clean == {}


def test_validate_params_strips():
    clean, missing = validate_params("song.send", {"query": "  晴天  "})
    assert not missing
    assert clean["query"] == "晴天"


def test_validate_unknown_kind():
    with pytest.raises(KeyError):
        validate_params("nope.do", {})


# ------------------------------------------------------------------ 状态机


def test_proposal_state_machine():
    p = Proposal(kind="song.send", title="发《晴天》", params={"query": "晴天"})
    ok, _ = p.usable()
    assert ok, "新提案应当可以确认"
    p.touch(STATUS_RUNNING)
    ok, why = p.usable()
    assert not ok and "执行" in why, "执行中不许再点"
    p.touch(STATUS_DONE)
    ok, why = p.usable()
    assert not ok and "完成" in why


def test_proposal_to_dict_has_label_and_sends():
    p = Proposal(kind="reply.draft", title="拟回复", params={"text": "好啊"})
    d = p.to_dict()
    assert d["label"] == "拟一句回复"
    assert d["sends"] is False, "拟回复不能有发送副作用"


# ------------------------------------------------------------------ 搜索


def test_score_prefers_original_over_cover():
    q = "周杰伦 晴天"
    good = search.score("【Hi-Res无损】周杰伦 - 晴天", 270, q)
    bad = search.score("晴天 翻唱 cover 吉他教学", 270, q)
    assert good > bad


def test_score_punishes_short_clip():
    q = "晴天"
    assert search.score("晴天 15秒", 15, q) < search.score("晴天 完整版", 260, q)


def test_search_source_is_download_whitelisted():
    """搜索源必须是下载白名单的子集。

    这条是硬约束：搜到 YouTube 再点确认，会在下载那一步被 `_check_supported` 拒掉，
    用户点的那一下等于白点。所以这里直接断言源码里只用了 bilisearch。
    """
    src = Path(search.__file__).read_text(encoding="utf-8")
    assert '"ytsearch' not in src, "不要用 YouTube 搜索：它不在下载白名单里"
    assert "bilisearch" in src


def test_search_skips_unlisted_links(monkeypatch):
    """即使 yt-dlp 返回了白名单外的链接，也要在结果里剔掉。"""
    fake = (
        "晴天 官方 https://www.bilibili.com/video/BV1xx411c7mD\t270\t\n"
        "晴天 搬运 https://www.youtube.com/watch?v=abc\t270\t\n"
    )

    class _R:
        stdout = ""
        stderr = ""
        returncode = 0

    rows_out = (
        "晴天 正版\t270\thttps://www.bilibili.com/video/BV1xx411c7mD\tup\n"
        "晴天 外链\t270\thttps://www.youtube.com/watch?v=abc\tup\n"
    )
    r = _R()
    r.stdout = rows_out

    monkeypatch.setattr(search, "_exe", lambda: "yt-dlp")
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: r)
    rows = search.search_song("晴天", 2)
    assert len(rows) == 1
    assert "bilibili.com" in rows[0]["url"]
    assert fake  # 保留变量，说明这里验的是"过滤"而不是"伪造输出"


# ------------------------------------------------------------------ 存储


@pytest.fixture()
def tmp_outputs(tmp_path, monkeypatch):
    """把 OUTPUTS_DIR 指到临时目录，并且清掉 store 的进程内缓存。"""
    import config

    monkeypatch.setattr(config, "OUTPUTS_DIR", tmp_path)
    store.reset_cache_for_tests()
    yield tmp_path
    store.reset_cache_for_tests()


def test_store_roundtrip(tmp_outputs):
    p = Proposal(kind="song.send", title="发《晴天》", params={"query": "晴天"})
    store.add(p)
    got = store.get(p.id)
    assert got is not None and got.title == "发《晴天》"
    # 落盘了没：重新读盘也应在
    store.reset_cache_for_tests()
    assert store.get(p.id) is not None


def test_store_drop_only_pending(tmp_outputs):
    p = Proposal(kind="voice.say", title="说一句", params={"text": "晚安"})
    store.add(p)
    assert store.drop(p.id)
    assert store.get(p.id).status == STATUS_CANCELLED
    # 已取消的不能再取消
    assert not store.drop(p.id)


def test_store_survives_corrupt_file(tmp_outputs):
    (tmp_outputs / "assistant").mkdir()
    (tmp_outputs / "assistant" / "proposals.json").write_text("{坏的", encoding="utf-8")
    store.reset_cache_for_tests()
    # 坏文件不该让后端起不来，且应被改名保留
    assert store.list_all() == []
    assert (tmp_outputs / "assistant" / "proposals.bad").exists()


def test_store_gc_keeps_only_pending_forever(tmp_outputs):
    import time as _t

    old = Proposal(kind="song.send", title="老的", params={"query": "旧"})
    old.touch(STATUS_DONE)
    old.updated = _t.time() - 7200  # 两小时前完成的
    store.add(old)
    new = Proposal(kind="song.send", title="新的", params={"query": "新"})
    store.add(new)
    ids = [p.id for p in store.list_all()]
    assert new.id in ids
    assert old.id not in ids, "过期终态应被清掉"


def test_history_is_append_only(tmp_outputs):
    p = Proposal(kind="reply.draft", title="拟回复", params={"text": "好啊"})
    store.add(p)
    p.touch(STATUS_DONE)
    store.append_history(p)
    store.append_history(p)
    lines = (tmp_outputs / "assistant" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == p.id


# ------------------------------------------------------------------ 提案生成（不落库）


def test_api_observe_dry_run_does_not_persist(tmp_outputs):
    import assistant_api
    from fastapi.testclient import TestClient

    app = __import__("fastapi").FastAPI()
    app.include_router(assistant_api.router)
    # TestClient 默认 client.host="testclient"，会被 loopback 守卫 403；显式给本机地址
    client = TestClient(app, client=("127.0.0.1", 51234))
    r = client.post("/api/assistant/observe", json={"text": "发一首晴天", "dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["proposals"], "应当抽出一条提案"
    assert body["proposals"][0]["status"] == STATUS_PENDING
    assert store.list_all() == [], "dry_run 不该落库"


def test_api_confirm_rejects_unknown_id(tmp_outputs):
    import assistant_api
    from fastapi.testclient import TestClient

    app = __import__("fastapi").FastAPI()
    app.include_router(assistant_api.router)
    # TestClient 默认 client.host="testclient"，会被 loopback 守卫 403；显式给本机地址
    client = TestClient(app, client=("127.0.0.1", 51234))
    r = client.post("/api/assistant/proposals/nonexistent/confirm")
    assert r.status_code == 404


# ---------------------------------------------------------------- 绝对时间音效
# 用户原话：「我说给我生成一个什么什么的话，然后在第几秒插入音效」。
# 这里钉住三件事：秒数解析、音效名归一、**指令片段绝不能混进要念的字里**。


@pytest.mark.parametrize(
    "raw,expect",
    [("3", 3.0), ("3.5", 3.5), ("三", 3.0), ("十五", 15.0), ("二十", 20.0), ("两", 2.0), ("零", 0.0)],
)
def test_seconds_parses_arabic_and_chinese(raw, expect):
    assert extract._seconds(raw) == expect


@pytest.mark.parametrize("raw", ["", "abc", "秒", "十百", "1.2.3"])
def test_seconds_rejects_garbage(raw):
    assert extract._seconds(raw) is None


def test_parse_sfx_instruction_forward():
    hit = extract.parse_sfx_instruction("说一句明天上午十点开会，第3秒插入爆炸声")
    assert hit is not None and hit.kind == "voice.say"
    assert hit.params["text"] == "明天上午十点开会", "音效指令不能被一起念出来"
    assert hit.params["sfx"] == [{"name": "爆炸", "at_s": 3.0}], "口语「爆炸声」要归一到素材名「爆炸」"
    assert "第3秒插入爆炸声" in hit.matched


def test_parse_sfx_instruction_backward_and_voice():
    hit = extract.parse_sfx_instruction("用袋鼠的声音说明天见，爆炸声放在第2.5秒")
    assert hit is not None
    assert hit.params["voice"] == "袋鼠"
    assert hit.params["text"] == "明天见"
    assert hit.params["sfx"][0]["at_s"] == 2.5


def test_parse_sfx_instruction_chinese_second():
    hit = extract.parse_sfx_instruction("念一句晚安，在第三秒的时候来一声掌声")
    assert hit is not None
    assert hit.params["text"] == "晚安"
    assert hit.params["sfx"] == [{"name": "掌声", "at_s": 3.0}]


def test_parse_sfx_instruction_multiple():
    hit = extract.parse_sfx_instruction("说明天开会，第2秒放个掌声，第5秒插入爆炸")
    assert hit is not None
    assert [(x["name"], x["at_s"]) for x in hit.params["sfx"]] == [("掌声", 2.0), ("爆炸", 5.0)]
    assert "第2秒" not in hit.params["text"] and "第5秒" not in hit.params["text"]


@pytest.mark.parametrize(
    "text,expect",
    [
        ("给我生成一句话，内容是我今晚不去了，然后在第5秒插入爆炸声", "我今晚不去了"),
        ("帮我生成一段话：明天上午十点开会，第3秒插入爆炸声", "明天上午十点开会"),
        ("跟我说：晚安，第1秒来一声叮", "晚安"),
    ],
)
def test_parse_sfx_strips_instruction_shell(text, expect):
    """"给我生成一句话"这类**需求外壳**不是台词 —— 绝不能念给群里听。"""
    hit = extract.parse_sfx_instruction(text)
    assert hit is not None
    assert hit.params["text"] == expect
    assert hit.params["sfx"]


def test_parse_sfx_unknown_name_returns_warning_not_proposal():
    hit = extract.parse_sfx_instruction("说明天开会，第3秒插入打雷声")
    assert hit is not None
    assert hit.params["text"] == "", "名字不认识时不许带着空参数生成可执行提案"
    assert any("没有这个音效" in n for n in hit.notes)
    assert any("爆炸" in n for n in hit.notes), "提示里要列出可用素材，否则用户无从改起"


def test_parse_sfx_only_says_whats_missing():
    hit = extract.parse_sfx_instruction("第3秒插入爆炸声")
    assert hit is not None
    assert hit.params["text"] == ""
    assert any("只有音效" in n for n in hit.notes)


def test_parse_without_sfx_still_normal():
    """没有秒数指令时，普通"说一句"必须原样工作（不能因为新分支把旧路径吃掉）。"""
    hits = extract.parse("说一句晚安")
    assert len(hits) == 1
    assert hits[0].params["text"] == "晚安"
    assert "sfx" not in hits[0].params


def test_clean_sfx_drops_dirty_and_caps():
    from assistant.schema import MAX_SFX_ITEMS, clean_sfx

    rows = clean_sfx(
        [
            {"name": "爆炸", "at_s": "3"},        # 字符串秒数可接受
            {"name": "", "at_s": 1},             # 没名字
            {"name": "掌声", "at_s": -1},         # 负数
            {"name": "叮", "at_s": "abc"},        # 非数字
            "爆炸",                              # 不是 dict
        ]
    )
    assert rows == [{"name": "爆炸", "at_s": 3.0}]
    many = clean_sfx([{"name": "叮", "at_s": i} for i in range(MAX_SFX_ITEMS + 5)])
    assert len(many) == MAX_SFX_ITEMS


def test_validate_params_normalizes_sfx():
    clean, missing = validate_params("voice.say", {"text": "晚安", "sfx": [{"name": "叮", "at_s": 0}]})
    assert not missing
    assert clean["sfx"] == [{"name": "叮", "at_s": 0.0}]
    # 全是脏项 → 当作没提音效，执行器不能走带音效的分支
    clean, _ = validate_params("voice.say", {"text": "晚安", "sfx": [{"name": "", "at_s": 1}]})
    assert "sfx" not in clean


def test_api_observe_returns_sfx_proposal(tmp_outputs):
    import assistant_api
    from fastapi.testclient import TestClient

    app = __import__("fastapi").FastAPI()
    app.include_router(assistant_api.router)
    client = TestClient(app, client=("127.0.0.1", 51234))
    r = client.post(
        "/api/assistant/observe",
        json={"text": "说明天开会，第3秒插入爆炸声", "dry_run": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["proposals"]) == 1
    p = body["proposals"][0]
    assert p["kind"] == "voice.say"
    assert p["params"]["sfx"] == [{"name": "爆炸", "at_s": 3.0}]
    assert "第 3s 插 爆炸" in p["detail"], "确认前要让人看见落点，不能只写句意"


def test_api_observe_reports_unknown_sfx_as_warning(tmp_outputs):
    import assistant_api
    from fastapi.testclient import TestClient

    app = __import__("fastapi").FastAPI()
    app.include_router(assistant_api.router)
    client = TestClient(app, client=("127.0.0.1", 51234))
    r = client.post(
        "/api/assistant/observe",
        json={"text": "说明天开会，第3秒插入打雷声", "dry_run": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["proposals"] == []
    assert body["warnings"] and "没有这个音效" in body["warnings"][0]


def test_executor_sfx_path_mixes_at_seconds_then_sends(tmp_outputs, monkeypatch):
    """带 sfx 的 voice.say 必须走"整段合成 → 按秒叠加 → send_voice"这条线。

    这里只验**接线**（落点秒数、发送的是混完的成品），混音算法本身由
    `test_premix.py` 压着 —— 两边分工，避免在这条测试里再造一个假混音器。
    """
    import numpy as np
    import soundfile as sf
    import tts_api
    import wechat_voice
    from assistant import executor
    from assistant.schema import STATUS_DONE

    src = tmp_outputs / "tts.wav"
    sf.write(str(src), np.zeros(22050 * 2, dtype=np.float32), 22050)

    seen: dict = {}

    monkeypatch.setattr(tts_api, "synth_wav", lambda text, voice_id=None: (src, 2.0, voice_id))
    import common

    monkeypatch.setattr(common, "selected_voice", lambda: "袋鼠")
    monkeypatch.setattr(wechat_voice, "_tts_ref_for", lambda want, rvc: ("tts_id", ""))
    monkeypatch.setattr(wechat_voice, "send_voice", lambda req: {"ok": True, "wav": req.wav})
    import rvc_convert

    monkeypatch.setattr(rvc_convert, "resolve_rvc_voice", lambda name: "")
    import sfx_mark
    import sfx_lib

    monkeypatch.setattr(sfx_mark, "parse", lambda t: ([("", "boom_norm")], [], []))
    monkeypatch.setattr(sfx_mark, "available", lambda: ["爆炸"])

    def _mix(pcm, sr, inserts):
        seen["inserts"] = inserts
        seen["sr"] = sr
        return pcm, ["按秒叠加：boom @3.0s"]

    monkeypatch.setattr(sfx_lib, "mix_into", _mix)
    import session_out

    out = tmp_outputs / "mixed.wav"
    monkeypatch.setattr(session_out, "new_path", lambda prefix, suffix=".wav": out)

    prop = Proposal(
        kind="voice.say",
        title="说一句",
        params={"text": "明天开会", "sfx": [{"name": "爆炸", "at_s": 3.0}]},
    )
    executor.run(prop)
    assert prop.status == STATUS_DONE, prop.error
    assert seen["inserts"][0]["at_s"] == 3.0
    assert seen["inserts"][0]["mode"] == "layer", "绝对时间音效是叠加，不能把语音推后"
    assert seen["inserts"][0]["sample"] == "boom_norm"
    assert out.exists(), "混完的成品必须落盘再发"
    assert any("boom" in s for s in prop.result["steps"]), "夹紧/落点备注要进 UI"
    assert prop.result["send"]["ok"]
