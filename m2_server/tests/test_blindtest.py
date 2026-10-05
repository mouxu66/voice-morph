# -*- coding: utf-8 -*-
"""`sound.blindtest`（盲听评测）的测试 —— 2026-10-05 新增。

分两层：
  · **核心层**：建会话（上传/引用）、洗牌、打分校验、汇总、收卷/删除、
    以及**保密面**（手机端形状里永远没有 label）—— 全部纯标准库，无模型。
  · **API 层**：TestClient 打 `/api/blindtest/*`，验证 HTTP 形状与错误码。

★ 本插件最重要的不变量：**手机端点（/t/{token}）的任何返回都不含 label / file /
  原始 responses** —— 盲听的意义全在这。有专门用例钉死。
"""

from __future__ import annotations

import json

import pytest

import blindtest as bt


@pytest.fixture(autouse=True)
def _isolated_bt(tmp_path, monkeypatch):
    """每个用例：存储目录指向临时目录；outputs 引用根也指过去。"""
    monkeypatch.setattr(bt.cfg, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(bt, "OUT", tmp_path / "outputs")
    yield


def _make_out_file(name: str, content: bytes = b"RIFFfake") -> str:
    """在（被指向临时目录的）outputs/ 下造一个可引用的产物，返回相对路径。"""
    p = bt.OUT / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return name.replace("\\", "/")


def _mk_session(**kw) -> dict:
    """两个样本的默认会话（用 refs，省 multipart）。"""
    refs = kw.pop("refs", None) or [_make_out_file("audition/a.wav"), _make_out_file("audition/b.wav")]
    return bt.create(kw.pop("title", "甲乙对比"), kw.pop("labels", ["甲音色", "乙音色"]), refs=refs)


# ---------------------------------------------------------------- 创建


def test_create_basic_shape():
    sess = _mk_session()
    assert sess["id"].startswith("bt_")
    assert len(sess["token"]) >= 20, "token 是链接凭证，必须够长"
    assert len(sess["samples"]) == 2
    assert {s["sid"] for s in sess["samples"]} == {"A", "B"}
    # order 是 0..n-1 的排列（洗过牌）
    assert sorted(s["order"] for s in sess["samples"]) == [0, 1]
    for s in sess["samples"]:
        assert (bt._audio_dir(sess["id"]) / s["file"]).is_file(), "样本副本必须真的落位"


def test_create_persists_and_reloads():
    sess = _mk_session()
    again = bt.detail(sess["id"])
    assert again["title"] == "甲乙对比"
    assert [s["label"] for s in again["samples"]] == ["甲音色", "乙音色"]


def test_create_too_few_or_many():
    one = _make_out_file("only/one.wav")
    with pytest.raises(bt.BlindtestError) as ei:
        bt.create("t", [], refs=[one])
    assert "至少" in str(ei.value)
    refs = [_make_out_file(f"x/{i}.wav") for i in range(bt.MAX_SAMPLES + 1)]
    with pytest.raises(bt.BlindtestError) as ei:
        bt.create("t", [], refs=refs)
    assert ei.value.status == 400


@pytest.mark.parametrize(
    "bad_ref",
    ["../escape.wav", "/abs/path.wav", "a/../../x.wav", "", "no/such/file.wav"],
)
def test_create_bad_refs(bad_ref):
    good = _make_out_file("audition/good.wav")
    with pytest.raises(bt.BlindtestError):
        bt.create("t", [], refs=[good, bad_ref])


def test_create_bad_ext_rejected(tmp_path):
    good = _make_out_file("audition/good.wav")
    evil = tmp_path / "evil.exe"
    evil.write_bytes(b"MZ")
    with pytest.raises(bt.BlindtestError) as ei:
        bt.create("t", [], files=[evil], refs=[good])
    assert "格式" in str(ei.value)


def test_create_title_and_label_limits():
    refs = [_make_out_file("a.wav"), _make_out_file("b.wav")]
    with pytest.raises(bt.BlindtestError):
        bt.create("x" * (bt.TITLE_MAX + 1), [], refs=refs)
    # 标签超长被截断而不是报错
    sess = bt.create("t", ["L" * 999, ""], refs=refs)
    d = bt.detail(sess["id"])
    assert len(d["samples"][0]["label"]) == bt.LABEL_MAX
    assert d["samples"][1]["label"], "空标签给默认值"


def test_failed_create_leaves_no_garbage(tmp_path):
    good = _make_out_file("audition/good.wav")
    bad = _make_out_file("audition/bad.txt")
    with pytest.raises(bt.BlindtestError):
        bt.create("t", [], refs=[good, bad])
    assert not (bt.base_dir() / "audio").exists() or not any(
        (bt.base_dir() / "audio").iterdir()
    ), "建会话失败不许留样本副本"


# ---------------------------------------------------------------- 查询 / token


def test_list_sessions_summary():
    s1 = _mk_session(title="第一场")
    s2 = _mk_session(title="第二场")
    items = bt.list_sessions()
    assert [i["id"] for i in items] == [s2["id"], s1["id"]], "新→旧"
    for i in items:
        assert "token" not in i, "列表摘要不需要 token"
        assert i["n_samples"] == 2 and i["n_responses"] == 0


def test_token_lookup():
    sess = _mk_session()
    found = bt.find_by_token(sess["token"])
    assert found["id"] == sess["id"]
    with pytest.raises(bt.BlindtestError) as ei:
        bt.find_by_token("wrong-token")
    assert ei.value.status == 404
    with pytest.raises(bt.BlindtestError) as ei:
        bt.find_by_token("")
    assert ei.value.status == 404


def test_detail_unknown_404():
    with pytest.raises(bt.BlindtestError) as ei:
        bt.detail("bt_nope")
    assert ei.value.status == 404


# ---------------------------------------------------------------- 打分


def test_submit_and_aggregate_math():
    sess = _mk_session()
    # A 打 5/5，B 打 3/4 → A 均分 5.0 排第一
    sids = {s["sid"] for s in sess["samples"]}
    a = "A" if "A" in sids else "B"
    b = "B" if a == "A" else "A"
    bt.submit(sess["token"], {a: 5, b: 3}, ua="r1")
    bt.submit(sess["token"], {a: 5, b: 4}, ua="r2")
    rows = {r["sid"]: r for r in bt.aggregate(bt.find_by_token(sess["token"]))}
    assert rows[a]["n"] == 2 and rows[a]["mean"] == 5.0
    assert rows[b]["mean"] == 3.5
    assert rows[a]["rank"] == 1 and rows[b]["rank"] == 2
    assert rows[b]["std"] == 0.5, "总体标准差 pstdev([3,4]) = 0.5"


@pytest.mark.parametrize(
    "scores",
    [
        {"A": 0, "B": 3},  # 越界下
        {"A": 6, "B": 3},  # 越界上
        {"A": 4.5, "B": 3},  # 非整数
        {"A": True, "B": 3},  # bool 冒充整数
        {"A": 3},  # 漏打
        {"A": 3, "B": 3, "C": 3},  # 多打（本场没有 C）
        {},
    ],
)
def test_submit_validation(scores):
    sess = _mk_session()
    with pytest.raises(bt.BlindtestError) as ei:
        bt.submit(sess["token"], scores)
    assert ei.value.status == 400


def test_submit_note_truncated():
    sess = _mk_session()
    r = bt.submit(sess["token"], {"A": 4, "B": 4}, note="n" * 999)
    assert r["ok"] is True
    resp = bt.detail(sess["id"])["responses"][0]
    assert len(resp["note"]) == bt.NOTE_MAX


def test_submit_closed_409():
    sess = _mk_session()
    bt.set_closed(sess["id"], True)
    with pytest.raises(bt.BlindtestError) as ei:
        bt.submit(sess["token"], {"A": 4, "B": 4})
    assert ei.value.status == 409
    bt.set_closed(sess["id"], False)  # 重开后又能交
    assert bt.submit(sess["token"], {"A": 4, "B": 4})["ok"] is True


def test_response_cap(monkeypatch):
    monkeypatch.setattr(bt, "MAX_RESPONSES", 2)
    sess = _mk_session()
    assert bt.submit(sess["token"], {"A": 4, "B": 4})["ok"] is True
    assert bt.submit(sess["token"], {"A": 4, "B": 4})["ok"] is True
    with pytest.raises(bt.BlindtestError) as ei:
        bt.submit(sess["token"], {"A": 4, "B": 4})
    assert ei.value.status == 409


# ---------------------------------------------------------------- ★ 保密面


def test_phone_shape_never_leaks_labels():
    sess = _mk_session()
    bt.submit(sess["token"], {"A": 4, "B": 5}, note="有的")
    phone = bt.phone_session(sess["token"])
    text = json.dumps(phone, ensure_ascii=False)
    for banned in ("甲音色", "乙音色", "label", "file", "responses"):
        assert banned not in text, f"手机端形状漏了 {banned}"
    assert {s["sid"] for s in phone["samples"]} == {"A", "B"}
    assert all(s["url"].startswith("/api/blindtest/t/") for s in phone["samples"])


def test_blind_overview_has_rank_but_no_identity():
    sess = _mk_session()
    bt.submit(sess["token"], {"A": 4, "B": 5})
    ov = bt.blind_overview(sess["token"])
    text = json.dumps(ov, ensure_ascii=False)
    assert "甲音色" not in text and "label" not in text
    rows = {r["sid"]: r for r in ov["samples"]}
    assert rows["B"]["rank"] == 1 and rows["A"]["rank"] == 2
    assert rows["B"]["mean"] == 5.0


def test_phone_forbidden_keys_documented():
    """口径有处可查：这份清单就是上面两个用例的判据来源。"""
    assert set(bt._PHONE_FORBIDDEN_KEYS) == {"labels", "label", "file", "responses"}


# ---------------------------------------------------------------- 音频伺服 / 生命周期


def test_audio_path_and_mime():
    sess = _mk_session()
    for s in sess["samples"]:
        p = bt.audio_path(sess["token"], s["sid"])
        assert p.is_file()
        assert bt.audio_mime(p) == "audio/wav"
        p2 = bt.audio_path(sess["token"], s["sid"].lower())
        assert p2 == p, "sid 大小写不敏感"
    with pytest.raises(bt.BlindtestError) as ei:
        bt.audio_path(sess["token"], "Z")
    assert ei.value.status == 404


def test_delete_removes_everything():
    sess = _mk_session()
    sid = sess["id"]
    assert bt.delete(sid)["ok"] is True
    assert not (bt.base_dir() / "audio" / sid).exists()
    with pytest.raises(bt.BlindtestError):
        bt.detail(sid)


def test_delete_unknown_404():
    with pytest.raises(bt.BlindtestError) as ei:
        bt.delete("bt_nope")
    assert ei.value.status == 404


# ---------------------------------------------------------------- API 层


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import blindtest_api

    app = FastAPI()
    app.include_router(blindtest_api.router)
    return TestClient(app)


def _wav_bytes() -> bytes:
    import io

    import numpy as np
    import soundfile as sf

    buf = io.BytesIO()
    t = np.linspace(0, 0.3, 7200, endpoint=False)
    sf.write(buf, (0.3 * np.sin(2 * np.pi * 440 * t)).astype("float32"), 24000, format="WAV")
    return buf.getvalue()


def test_api_full_flow(client):
    w = _wav_bytes()
    r = client.post(
        "/api/blindtest/sessions",
        data={"title": "接口流", "labels": json.dumps(["一个", "两个"]), "refs": "[]"},
        files=[
            ("files", ("x.wav", io_bytes(w), "audio/wav")),
            ("files", ("y.mp3", io_bytes(w), "audio/mpeg")),
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["token"]
    sid, token = body["id"], body["token"]

    # 手机端：形状 + 无 label
    ph = client.get(f"/api/blindtest/t/{token}")
    assert ph.status_code == 200
    assert "一个" not in json.dumps(ph.json(), ensure_ascii=False)
    sids = [s["sid"] for s in ph.json()["samples"]]
    assert len(sids) == 2

    # 音频伺服：随便取手机形状里的一个样本，必须是可播的 audio/*
    target = next(s for s in ph.json()["samples"])
    audio = client.get(f"/api/blindtest/t/{token}/audio/{target['sid']}")
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/")

    # 交卷 → 详情可见映射与汇总
    assert (
        client.post(f"/api/blindtest/t/{token}/submit", json={"scores": {sids[0]: 5, sids[1]: 2}, "note": "行"}).json()[
            "ok"
        ]
        is True
    )
    detail = client.get(f"/api/blindtest/sessions/{sid}").json()
    assert detail["aggregate"][0]["rank"] == 1
    assert len(detail["responses"]) == 1

    # 收卷后手机不能再交
    assert client.post(f"/api/blindtest/sessions/{sid}/close", json={"closed": True}).json()["closed"] is True
    assert client.post(f"/api/blindtest/t/{token}/submit", json={"scores": {sids[0]: 5, sids[1]: 2}}).status_code == 409

    # 删除 → 手机链接失效
    assert client.delete(f"/api/blindtest/sessions/{sid}").json()["ok"] is True
    assert client.get(f"/api/blindtest/t/{token}").status_code == 404


def io_bytes(b: bytes):
    from io import BytesIO

    return BytesIO(b)


def test_api_list_shape(client):
    r = client.get("/api/blindtest/sessions")
    assert r.status_code == 200
    assert "sessions" in r.json()


def test_api_create_bad_refs_json_400(client):
    r = client.post(
        "/api/blindtest/sessions",
        data={"title": "t", "refs": "{not json", "labels": "[]"},
    )
    assert r.status_code == 400


def test_api_create_ref_not_found_404(client):
    good = _make_out_file("audition/ok.wav")
    r = client.post(
        "/api/blindtest/sessions",
        data={"title": "t", "labels": "[]", "refs": json.dumps([good, "audition/missing.wav"])},
    )
    assert r.status_code == 404


def test_api_results_endpoint_blind(client):
    w = _wav_bytes()
    body = client.post(
        "/api/blindtest/sessions",
        data={"title": "r", "labels": json.dumps(["密", "码"]), "refs": "[]"},
        files=[
            ("files", ("a.wav", io_bytes(w), "audio/wav")),
            ("files", ("b.wav", io_bytes(w), "audio/wav")),
        ],
    ).json()
    token = body["token"]
    sess = bt.find_by_token(token)
    sids = [s["sid"] for s in sess["samples"]]
    client.post(f"/api/blindtest/t/{token}/submit", json={"scores": {sids[0]: 5, sids[1]: 1}})
    res = client.get(f"/api/blindtest/t/{token}/results")
    assert res.status_code == 200
    text = json.dumps(res.json(), ensure_ascii=False)
    assert "密" not in text and "label" not in text
