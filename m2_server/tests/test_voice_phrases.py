"""快捷短语板（voice_phrases）的 CRUD / 缓存 / 端点行为。

三条防线：
  1. 纯函数/存储层：播种、CRUD、缓存键、淘汰 —— 直接调模块函数（持锁路径真实跑）。
  2. 端点：TestClient 挂一个**只含本 router** 的裸 app（不起整个 server ——
     那会拉起全部插件，测试环境跑不动也不必要）。
  3. 合成链路：monkeypatch `tts_api.synth_wav`（模块属性 —— speak() 是函数内
     `from tts_api import synth_wav`，patch 模块属性才能命中）。
"""

from __future__ import annotations

from pathlib import Path

import soundfile as sf  # noqa: F401 —— 保证依赖在（speak 会用它读时长）
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config as cfg
import voice_phrases as vp


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(vp.router)
    return TestClient(app, raise_server_exceptions=False)


def _fake_synth(monkeypatch, calls: list[tuple[str, str]]):
    """替换 synth_wav：写一个 0.2s 的真 wav 到会话目录（speak 会 copy+读时长）。"""

    def _synth(text: str, voice_id: str = "", *a, **kw):
        calls.append((text, voice_id))
        out = cfg.OUTPUTS_DIR / ".session" / f"tts_{len(calls)}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out), [0.0] * 100, 500, subtype="PCM_16")
        return out, 0.2, voice_id

    monkeypatch.setattr("tts_api.synth_wav", _synth)


# ---------------- 存储 / 播种 ----------------


def test_first_load_seeds_defaults_and_persists():
    items = vp.load_items()
    assert len(items) == len(vp.DEFAULT_PHRASES)
    # 落盘了，且第二次读不再重播种（id 稳定）
    f = vp._state_file()
    assert f.is_file()
    again = vp.load_items()
    assert [i["id"] for i in again] == [i["id"] for i in items]


def test_corrupt_state_file_falls_back_to_defaults_not_crash():
    vp._state_file().write_text("{broken", "utf-8")
    items = vp._read_unlocked()
    assert len(items) == len(vp.DEFAULT_PHRASES)


# ---------------- CRUD 端点 ----------------


def test_crud_roundtrip():
    c = _client()
    items = c.get("/api/voice-phrases").json()["items"]
    assert len(items) == len(vp.DEFAULT_PHRASES)  # 首次 GET 播种

    r = c.post("/api/voice-phrases", json={"text": "  请翻开课本第 12 页。 "})
    assert r.status_code == 200
    item = r.json()["item"]
    assert item["text"] == "请翻开课本第 12 页。"  # 首尾空白剥掉

    r = c.put(f"/api/voice-phrases/{item['id']}", json={"text": "请翻开课本第 13 页。"})
    assert r.status_code == 200

    r = c.delete(f"/api/voice-phrases/{item['id']}")
    assert r.status_code == 200
    texts = [i["text"] for i in c.get("/api/voice-phrases").json()["items"]]
    assert "请翻开课本第 13 页。" not in texts


def test_crud_validation_and_missing_id():
    c = _client()
    assert c.post("/api/voice-phrases", json={"text": "   "}).status_code == 400
    assert c.post("/api/voice-phrases", json={"text": "x" * 201}).status_code == 400
    assert c.put("/api/voice-phrases/nope", json={"text": "改"}).status_code == 404
    assert c.delete("/api/voice-phrases/nope").status_code == 404
    assert c.post("/api/voice-phrases/nope/speak", json={}).status_code == 404


def test_add_is_capped():
    c = _client()
    # 首次 GET 播种 5 条，再加到上限
    for i in range(vp.MAX_PHRASES - len(vp.DEFAULT_PHRASES)):
        assert c.post("/api/voice-phrases", json={"text": f"第{i}句"}).status_code == 200
    r = c.post("/api/voice-phrases", json={"text": "装不下了"})
    assert r.status_code == 400
    assert "最多" in r.json()["detail"]


# ---------------- speak：缓存链路 ----------------


def test_speak_synth_then_cache_hit(monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_synth(monkeypatch, calls)
    c = _client()
    pid = c.post("/api/voice-phrases", json={"text": "大家先思考一分钟。"}).json()["item"]["id"]

    r1 = c.post(f"/api/voice-phrases/{pid}/speak", json={"voice_id": "wang"})
    assert r1.status_code == 200
    b1 = r1.json()
    assert b1["cached"] is False and b1["voice_id"] == "wang"
    assert b1["url"].startswith("/api/media/outputs/phrase_cache/")
    assert calls == [("大家先思考一分钟。", "wang")]

    # 第二次：同文同音色 → 缓存命中，不再合成
    r2 = c.post(f"/api/voice-phrases/{pid}/speak", json={"voice_id": "wang"}).json()
    assert r2["cached"] is True and r2["url"] == b1["url"]
    assert len(calls) == 1

    # 缓存文件真的在磁盘上（退出后依然零等待）
    name = b1["url"].rsplit("/", 1)[-1]
    assert (vp._cache_dir() / name).is_file()


def test_speak_cache_key_distinguishes_voice_and_text(monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_synth(monkeypatch, calls)
    c = _client()
    pid = c.post("/api/voice-phrases", json={"text": "同学们好。"}).json()["item"]["id"]

    c.post(f"/api/voice-phrases/{pid}/speak", json={"voice_id": "a"})
    c.post(f"/api/voice-phrases/{pid}/speak", json={"voice_id": "b"})  # 换音色 → miss
    assert len(calls) == 2

    c.put(f"/api/voice-phrases/{pid}", json={"text": "同学们再见。"})  # 改文字 → miss
    c.post(f"/api/voice-phrases/{pid}/speak", json={"voice_id": "a"})
    assert len(calls) == 3
    assert {t for t, _v in calls} == {"同学们好。", "同学们再见。"}


def test_speak_falls_back_to_selected_voice_and_400_without_one(monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_synth(monkeypatch, calls)
    c = _client()
    pid = c.post("/api/voice-phrases", json={"text": "上课。"}).json()["item"]["id"]

    # 没选音色 → 400（与 /tts 同一口径）
    r = c.post(f"/api/voice-phrases/{pid}/speak", json={})
    assert r.status_code == 400 and "音色" in r.json()["detail"]

    monkeypatch.setattr(vp, "_selected_voice", lambda: "preserve_wang")
    b = c.post(f"/api/voice-phrases/{pid}/speak", json={}).json()
    assert b["voice_id"] == "preserve_wang" and b["cached"] is False


def test_speak_uses_store_lock_not_deadlock(monkeypatch):
    """持 _STORE_LOCK 的端点读短语不许重入 load_items() —— 重入 = 死锁。

    真实持锁跑一遍就是防线：死锁时 TestClient 会挂到超时，而不是静默通过。
    """
    calls: list[tuple[str, str]] = []
    _fake_synth(monkeypatch, calls)
    c = _client()
    pid = c.post("/api/voice-phrases", json={"text": "锁不重入。"}).json()["item"]["id"]
    # update / delete / speak 三条都是「持锁读改写」路径
    assert c.put(f"/api/voice-phrases/{pid}", json={"text": "锁真的不重入。"}).status_code == 200
    assert c.post(f"/api/voice-phrases/{pid}/speak", json={"voice_id": "v"}).status_code == 200
    assert c.delete(f"/api/voice-phrases/{pid}").status_code == 200


# ---------------- play：热键链路（播进虚拟声卡） ----------------


def test_play_sends_cached_wav_to_soundboard_player(monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_synth(monkeypatch, calls)
    sent: list[dict] = []

    def _fake_command(payload: dict) -> dict:
        sent.append(payload)
        return {"type": "playing", "duration_s": 0.2}

    monkeypatch.setattr("soundboard._command", _fake_command)
    c = _client()
    pid = c.post("/api/voice-phrases", json={"text": "看黑板。"}).json()["item"]["id"]

    r = c.post(f"/api/voice-phrases/{pid}/play", json={"voice_id": "wang"})
    assert r.status_code == 200
    b = r.json()
    assert b["playing"] is True and b["cached"] is False and b["duration_s"] == 0.2
    # 下发给播放器的 wav 就是持久缓存产物本身（在 phrase_cache 下、真实存在）
    assert len(sent) == 1
    wav = Path(sent[0]["wav"])
    assert wav.is_file() and wav.parent == vp._cache_dir()
    assert sent[0]["id"].startswith("phrase_")
    assert sent[0]["gain"] == 1.0  # 整条语音是唯一内容，不叠人声 → 满增益

    # 第二次：不再合成（缓存命中），但播放命令照样下发（按一下响一次）
    r2 = c.post(f"/api/voice-phrases/{pid}/play", json={"voice_id": "wang"}).json()
    assert r2["cached"] is True
    assert len(sent) == 2
    assert len(calls) == 1


def test_play_maps_player_failure_to_503(monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_synth(monkeypatch, calls)
    monkeypatch.setattr(
        "soundboard._command", lambda payload: (_ for _ in ()).throw(RuntimeError("device_not_found:CABLE"))
    )
    c = _client()
    pid = c.post("/api/voice-phrases", json={"text": "下课。"}).json()["item"]["id"]
    r = c.post(f"/api/voice-phrases/{pid}/play", json={"voice_id": "v"})
    assert r.status_code == 503 and "device_not_found" in r.json()["detail"]


def test_play_requires_voice_and_existing_id(monkeypatch):
    monkeypatch.setattr("soundboard._command", lambda payload: {"type": "playing"})
    c = _client()
    assert c.post("/api/voice-phrases/nope/play", json={"voice_id": "v"}).status_code == 404
    pid = c.post("/api/voice-phrases", json={"text": "上课。"}).json()["item"]["id"]
    # 没选音色（默认 _selected_voice()==""）→ 400，播放器根本不该被碰
    r = c.post(f"/api/voice-phrases/{pid}/play", json={})
    assert r.status_code == 400 and "音色" in r.json()["detail"]


# ---------------- play：按「真声直讲」状态分流（2026-10-04） ----------------
#
# 这两条守的是**老师上课的主场景**：真声直讲开着时，热键必须把短语**叠加**到
# 正在直通的真声上，而不是走 `sd.play` 那条独占通路 —— 后者的全局 stop 会把
# 老师讲到一半的真声**整个掐断**（学生那头突然静音）。所以：
#   · 直讲开着 → 走 passthrough.play_overlay，**且不许碰 soundboard**；
#   · 直讲没开 → 走 soundboard（原行为），**且不许碰 passthrough**。
# 两条都断言"另一侧一次都没被调"，因为只断言"选对了"不足以发现"两边都调了"。


def test_play_routes_to_passthrough_when_live_is_on(monkeypatch):
    calls: list[tuple[str, str]] = []
    _fake_synth(monkeypatch, calls)
    mixed: list[tuple] = []
    stockboard_called: list[dict] = []

    import passthrough

    monkeypatch.setattr(passthrough, "is_running", lambda: True)
    monkeypatch.setattr(
        passthrough, "play_overlay",
        lambda wav, clip_id, gain=0.9: mixed.append((wav, clip_id, gain)) or {"type": "playing", "duration_s": 0.2},
    )
    monkeypatch.setattr("soundboard._command", lambda payload: stockboard_called.append(payload) or {"type": "playing"})

    c = _client()
    pid = c.post("/api/voice-phrases", json={"text": "看黑板。"}).json()["item"]["id"]
    b = c.post(f"/api/voice-phrases/{pid}/play", json={"voice_id": "wang"}).json()

    assert b["playing"] is True and b["mixed"] is True
    assert len(mixed) == 1, "直讲开着时没有走叠加通路"
    wav, clip_id, gain = mixed[0]
    assert Path(wav).is_file() and Path(wav).parent == vp._cache_dir()
    assert clip_id.startswith("phrase_")
    assert gain == 0.9, "叠加要给两路相加留余量（满增益会削波）"
    assert stockboard_called == [], "★ 直讲开着时绝不能走声板 —— 那会掐断老师的真声"


def test_play_falls_back_to_soundboard_when_live_is_off(monkeypatch):
    """直讲没开 → 行为**零变化**（不许因为新增了分流就把老路径换掉）。"""
    calls: list[tuple[str, str]] = []
    _fake_synth(monkeypatch, calls)
    sent: list[dict] = []
    mixed: list[tuple] = []

    import passthrough

    monkeypatch.setattr(passthrough, "is_running", lambda: False)
    monkeypatch.setattr(passthrough, "play_overlay", lambda *a, **k: mixed.append(a) or {"type": "playing"})
    monkeypatch.setattr("soundboard._command", lambda payload: sent.append(payload) or {"type": "playing", "duration_s": 0.2})

    c = _client()
    pid = c.post("/api/voice-phrases", json={"text": "下课。"}).json()["item"]["id"]
    b = c.post(f"/api/voice-phrases/{pid}/play", json={"voice_id": "wang"}).json()

    assert b["playing"] is True and b.get("mixed") is None
    assert len(sent) == 1 and sent[0]["gain"] == 1.0
    assert mixed == [], "直讲没开时不该走叠加通路"


def test_play_surfaces_passthrough_failure_as_503(monkeypatch):
    """叠加通路失败（worker 崩了/管道关了）→ 503，与声板那条同口径。"""
    calls: list[tuple[str, str]] = []
    _fake_synth(monkeypatch, calls)

    import passthrough

    monkeypatch.setattr(passthrough, "is_running", lambda: True)
    monkeypatch.setattr(
        passthrough, "play_overlay",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("真声直通 worker 已退出（管道关闭）")),
    )
    c = _client()
    pid = c.post("/api/voice-phrases", json={"text": "上课。"}).json()["item"]["id"]
    r = c.post(f"/api/voice-phrases/{pid}/play", json={"voice_id": "v"})
    assert r.status_code == 503 and "管道关闭" in r.json()["detail"]


def test_url_to_path_rejects_escape_and_keeps_session_fallback():
    assert vp._url_to_path("/api/media/outputs/../../secret.wav") is None
    assert vp._url_to_path("/api/media/outputs//x.wav") is None
    assert vp._url_to_path("/other/x.wav") is None
    assert vp._url_to_path("/api/media/outputs/phrase_cache/abc.wav") == (
        cfg.OUTPUTS_DIR / "phrase_cache" / "abc.wav"
    )
    # speak() 的 OSError 兜底产物在 .session 下 —— 点开头但不是 ".."，放行
    assert vp._url_to_path("/api/media/outputs/.session/tts_1.wav") == (
        cfg.OUTPUTS_DIR / ".session" / "tts_1.wav"
    )


# ---------------- 缓存淘汰 ----------------


def test_trim_cache_evicts_oldest(monkeypatch, tmp_path):
    monkeypatch.setattr(vp, "MAX_CACHE_FILES", 3)
    d = vp._cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        p = d / f"old{i}.wav"
        p.write_bytes(b"x")
        import os

        os.utime(p, (1000 + i, 1000 + i))
    vp._trim_cache()
    left = sorted(p.name for p in d.glob("*.wav"))
    assert left == ["old2.wav", "old3.wav", "old4.wav"]  # 最旧的两条被淘汰
