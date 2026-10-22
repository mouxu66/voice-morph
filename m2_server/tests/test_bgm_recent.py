"""`GET /api/soundboard/bgm/recent` —— 桌宠面板「放歌」页的换歌列表。

为什么这批用例值得单独存在
--------------------------
这个端点是**面板上唯一的选歌入口**（204px 里放不下导入按钮，选歌靠它）。
它列错东西的代价不是"少一首歌"，而是两种更难发现的失败：

1. **把不是歌的列成歌**：会话目录里同时躺着 `tts_*.wav`（合成语音）、`sfxmix_*`
   （预混产物）、`cover_in_*` / `cover_voice_*`（翻唱链路的中间件）。列进来用户
   点了才发现是 5 秒人声或一段纯伴奏 —— 而列表上看不出任何异常。
2. **列得出、点不动**：`/bgm/start` 只查会话目录时，常驻歌单（`media/songs`）里的歌
   会出现在列表里却 404。所以「列表里的名字必须真能被解析」是一条独立的不变量，
   不是同一件事的两种说法。

还有一个跨会话细节：`last` 指针指向的歌可能在会话清空后消失（歌是会话产物，
退出即删），那时端点必须**当作没有**，而不是把面板指向一个点了 404 的名字。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

_M2 = Path(__file__).resolve().parents[1]
if str(_M2) not in sys.path:
    sys.path.insert(0, str(_M2))

import config as cfg  # noqa: E402
import soundboard  # noqa: E402

SR = 44100


# --------------------------------------------------------------- 夹具


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把 outputs/ 与 media/ 都按用例隔离（会话目录 = outputs/.session）。

    `_songs_dir()` / `_bgm_last_file()` 都是**调用期**现读 `cfg.*` 的，所以这里
    monkeypatch 模块属性就能生效；这也正是它们不写成模块常量的理由
    （早绑定会让隔离静默失效，见 `docs/犯错档案-工程.md` §8.36/§8.37）。
    """
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path / "outputs")
    monkeypatch.setattr(cfg, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(soundboard, "_proc", None)
    session = cfg.OUTPUTS_DIR / ".session"
    session.mkdir(parents=True)
    (cfg.MEDIA_DIR / "songs").mkdir(parents=True)
    return {"session": session, "songs": cfg.MEDIA_DIR / "songs"}


@pytest.fixture()
def client(env):  # noqa: ARG001 —— 只需要目录就位
    app = FastAPI()
    app.include_router(soundboard.router)
    return TestClient(app, raise_server_exceptions=False)


def _song(path: Path, seconds: float = 1.0) -> Path:
    """造一条能读的短音频（真 wav —— 端点会读时长，空文件会被静默跳过）。"""
    n = int(SR * seconds)
    sf.write(str(path), np.zeros(n, dtype=np.float32), SR, subtype="PCM_16")
    return path


def _names(payload) -> list[str]:
    return [it["name"] for it in payload["items"]]


# --------------------------------------------------------------- 会话目录白名单


def test_recent_lists_uploaded_songs_and_skips_tts_products(client, env):
    _song(env["session"] / "bgm_1792678124662.wav")
    _song(env["session"] / "tts_1792678124000.wav")       # 合成语音：不是歌
    _song(env["session"] / "rvc_tts_1792678124000.wav")   # 换声产物：不是歌
    _song(env["session"] / "sfxmix_tts_x.wav")            # 预混产物：不是歌

    r = client.get("/api/soundboard/bgm/recent")
    assert r.status_code == 200, r.text
    assert _names(r.json()) == ["bgm_1792678124662.wav"]
    assert r.json()["items"][0]["source"] == "session"


def test_recent_includes_cover_final_and_skips_its_intermediates(client, env):
    """翻唱成品能放，但同一条链路的中间件不能列（voice 只有人声、in 是原始输入）。"""
    _song(env["session"] / "cover_1792678124662_1792678125000.wav")
    _song(env["session"] / "cover_in_1792678124662.wav")
    _song(env["session"] / "cover_voice_1792678124662.wav")
    _song(env["session"] / "cover_src_1792678124662.wav")

    assert _names(client.get("/api/soundboard/bgm/recent").json()) == [
        "cover_1792678124662_1792678125000.wav"
    ]


# --------------------------------------------------------------- 常驻歌单


def test_recent_lists_library_songs_and_start_can_resolve_them(client, env, monkeypatch):
    """★ 不变量：**列表里点得中的名字，`/bgm/start` 必须解析得到**。

    常驻歌单（`media/songs`）不归会话目录管，所以 `_bgm_path` 要单独放行它 ——
    否则面板会列出一首点了就 404 的歌，而界面上看不出任何异常。
    """
    lib = _song(env["songs"] / "my_song.wav")
    sent: list[dict] = []

    def fake_command(payload):
        sent.append(payload)
        return {"type": "bgm_playing", "id": payload.get("id"), "duration_s": 1.0}

    monkeypatch.setattr(soundboard, "_command", fake_command)

    j = client.get("/api/soundboard/bgm/recent").json()
    assert [(it["name"], it["source"]) for it in j["items"]] == [("my_song.wav", "library")]

    r = client.post("/api/soundboard/bgm/start", json={"bgm": "my_song.wav"})
    assert r.status_code == 200, r.text
    assert sent and Path(sent[0]["bgm"]) == lib
    assert r.json()["id"] == "my_song"     # = 文件名去扩展名，面板拿它镜像按钮状态


def test_recent_prefers_the_session_copy_when_names_collide(client, env):
    """重名时列表顺序要与 `_bgm_path` 的查找顺序一致（会话在前）——
    否则列表首项和真正被播放的那份可能不是同一个文件。"""
    _song(env["session"] / "bgm_same.wav")
    _song(env["songs"] / "bgm_same.wav")

    items = client.get("/api/soundboard/bgm/recent").json()["items"]
    assert [it["name"] for it in items] == ["bgm_same.wav"]
    assert items[0]["source"] == "session"


# --------------------------------------------------------------- last 指针


def test_recent_returns_last_played_and_drops_it_once_the_file_is_gone(client, env, monkeypatch):
    """歌是会话产物：退出应用就没了。指针必须跟着失效，否则面板指着一个 404 的名字。"""
    song = _song(env["session"] / "bgm_1792678124662.wav")
    monkeypatch.setattr(soundboard, "_command", lambda payload: {"duration_s": 1.0})

    assert client.get("/api/soundboard/bgm/recent").json()["last"] is None

    assert client.post("/api/soundboard/bgm/start", json={"bgm": song.name}).status_code == 200
    assert client.get("/api/soundboard/bgm/recent").json()["last"] == song.name

    song.unlink()
    j = client.get("/api/soundboard/bgm/recent").json()
    assert j["items"] == [] and j["last"] is None


def test_recent_survives_a_broken_last_pointer(client, env):
    """指针文件坏了就当没有 —— 它只是"上次放了哪首"，不该拖着整个列表 500。"""
    _song(env["session"] / "bgm_a.wav")
    bad = cfg.OUTPUTS_DIR / "soundboard_bgm_last.json"
    bad.write_text("{not json", encoding="utf-8")

    j = client.get("/api/soundboard/bgm/recent").json()
    assert _names(j) == ["bgm_a.wav"] and j["last"] is None


# --------------------------------------------------------------- 边界


def test_recent_caps_the_list_at_the_panel_limit(client, env):
    """204px 的下拉装不下长列表：上限是端点契约的一部分（面板不做二次截断）。"""
    for i in range(soundboard.BGM_RECENT_LIMIT + 4):
        _song(env["session"] / f"bgm_{i:03d}.wav")

    assert len(client.get("/api/soundboard/bgm/recent").json()["items"]) == (
        soundboard.BGM_RECENT_LIMIT
    )


def test_bgm_start_rejects_path_components(client, env, monkeypatch):
    """请求体里的路径不该能指向歌单目录之外（预混复用同一个 `_bgm_path`）。"""
    monkeypatch.setattr(soundboard, "_command", lambda payload: {"duration_s": 1.0})
    for bad in ("../secret.wav", "C:/Windows/win.ini", ""):
        r = client.post("/api/soundboard/bgm/start", json={"bgm": bad})
        assert r.status_code in (400, 404), (bad, r.status_code, r.text)
