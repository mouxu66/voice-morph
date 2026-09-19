"""OpenAI 兼容语音 API 单测（m2_server/openai_compat.py）。

为什么值得单测：这层的价值全在**协议细节**上，而协议细节最容易在重构中被顺手改坏：
  1. 路由前缀必须是 /v1（不是 /api/v1）—— SDK 的 base_url 语义要求，改错用户按官方文档
     写反而打不通，且现场表现是 404，极难自查
  2. 错误体必须是 OpenAI 的 {"error": {...}} 形状 —— 用 FastAPI 默认 {"detail": ...}
     会让 SDK 抛解析异常，用户看不到我们写的中文提示
  3. voice 参数映射：内置名（alloy…）**不能**静默替换成本机音色，
     否则用户以为用的是 alloy，实际是别的声音，且毫无提示
  4. 不支持的 response_format 要 400 且列出可选项（而不是转码失败后 500）

真起 TTS 引擎代价太大（加载 1.7B 模型占显存，测试环境 VM_WARMUP=0 本就不预热），
所以这里**打桩 synth_wav**，只验协议层；音频内容正确性由 tts_api 自己的测试覆盖。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import openai_compat as mod  # noqa: E402

# ---------------- 路由前缀（回归锁）----------------


def test_router_prefix_is_v1_not_api_v1():
    """前缀必须是 /v1。

    锁死这个值：OpenAI SDK 的 base_url="http://127.0.0.1:8000/v1" 意味着
    「/v1 之前的部分是 base」，SDK 会自己拼 /audio/speech。挂到 /api/v1 后
    用户按官方文档写 base_url 会 404，而报错信息完全指不到原因。
    """
    assert mod.router.prefix == "/v1"


def test_openai_router_registered_on_app():
    """server.py 里确实挂上了这个 router（防止改装配层时漏掉）。"""
    server = pytest.importorskip("server")
    # 惰性挂载：不用枚举 app.routes 断言，走真实请求
    from fastapi.testclient import TestClient

    client = TestClient(server.app)
    assert client.get("/v1/models").status_code == 200


# ---------------- voice 映射 ----------------


def _stub_voices(monkeypatch, ids, selected=""):
    monkeypatch.setattr(mod, "_available_voice_ids", lambda: list(ids))
    monkeypatch.setattr("common.selected_voice", lambda: selected, raising=False)


def test_default_voice_falls_back_to_selected(monkeypatch):
    _stub_voices(monkeypatch, ["kangaroo", "nv-oi"], selected="kangaroo")
    assert mod._resolve_voice("default") == "kangaroo"


def test_empty_voice_falls_back_to_selected(monkeypatch):
    _stub_voices(monkeypatch, ["kangaroo"], selected="kangaroo")
    assert mod._resolve_voice("") == "kangaroo"


def test_exact_voice_id_wins(monkeypatch):
    _stub_voices(monkeypatch, ["kangaroo", "nv-oi"], selected="kangaroo")
    assert mod._resolve_voice("nv-oi") == "nv-oi"


def test_no_selected_voice_raises_actionable(monkeypatch):
    """ "default" 但本机没选音色 → 报错要告诉用户去哪选，而不是空指针。"""
    _stub_voices(monkeypatch, ["kangaroo"], selected="")
    with pytest.raises(ValueError) as e:
        mod._resolve_voice("default")
    assert "音色库" in str(e.value)


def test_openai_builtin_voice_not_silently_replaced(monkeypatch):
    """★ 关键行为：alloy 等内置名必须报错，不能悄悄换成本机音色。

    静默替换是最坏的一种"成功"——用户以为调的是 alloy，
    拿到的是完全别的声音，且任何日志里都看不出差异。
    """
    _stub_voices(monkeypatch, ["kangaroo"], selected="kangaroo")
    with pytest.raises(ValueError) as e:
        mod._resolve_voice("alloy")
    msg = str(e.value)
    assert "OpenAI" in msg and "本机没有" in msg


def test_unknown_voice_lists_available_ids(monkeypatch):
    """报错要指路：直接列出可用 id，用户不用去翻文档。"""
    _stub_voices(monkeypatch, ["kangaroo", "nv-oi"], selected="kangaroo")
    with pytest.raises(ValueError) as e:
        mod._resolve_voice("not-a-voice")
    msg = str(e.value)
    assert "kangaroo" in msg and "nv-oi" in msg


# ---------------- 格式转码 ----------------


def test_wav_passthrough():
    body, mime = mod._transcode(b"RIFFfake", "wav")
    assert body == b"RIFFfake" and mime == "audio/wav"


def test_pcm_strips_wav_header():
    """OpenAI 的 pcm 是裸流，带 44 字节 RIFF 头会让播放器出噪音。"""
    fake = b"H" * 44 + b"\x01\x02\x03\x04"
    body, mime = mod._transcode(fake, "pcm")
    assert body == b"\x01\x02\x03\x04"
    assert mime == "audio/L16"


def test_unsupported_format_lists_options():
    with pytest.raises(ValueError) as e:
        mod._transcode(b"x", "ogg-vorbis")
    msg = str(e.value)
    assert "wav" in msg and "mp3" in msg


# ---------------- 端点行为 ----------------


def _client():
    server = pytest.importorskip("server")
    from fastapi.testclient import TestClient

    return TestClient(server.app)


def test_models_endpoint_shape():
    """SDK 常先探测 /v1/models；返回空列表会被判定「服务不可用」。"""
    r = _client().get("/v1/models")
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "list"
    assert any(m["id"] == "tts-1" for m in data["data"])


def test_speech_empty_input_400_openai_shape(monkeypatch):
    _stub_voices(monkeypatch, ["kangaroo"], selected="kangaroo")
    r = _client().post(
        "/v1/audio/speech", json={"model": "tts-1", "input": "   ", "voice": "default"}
    )
    assert r.status_code == 400
    err = r.json()["error"]
    assert set(err) >= {"message", "type", "code"}
    assert err["code"] == "invalid_input"


def test_speech_bad_voice_400_lists_ids(monkeypatch):
    _stub_voices(monkeypatch, ["kangaroo"], selected="kangaroo")
    r = _client().post(
        "/v1/audio/speech", json={"input": "你好", "voice": "alloy", "response_format": "wav"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_voice"


def test_speech_bad_format_400_not_500(monkeypatch):
    """格式错是**请求**错，必须 400，且错误码要指向 response_format 本身。

    若让它在转码阶段才炸，用户看到的是 500「合成失败」，
    会以为是模型/显存问题，往完全错的方向排查。
    两个参数错还要给不同 code —— 合并成一个 invalid_voice 会让
    response_format 写错的人去查 voice（首版实测就是这个毛病）。
    """
    _stub_voices(monkeypatch, ["kangaroo"], selected="kangaroo")
    r = _client().post(
        "/v1/audio/speech", json={"input": "你好", "voice": "default", "response_format": "xyz"}
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_format"
    assert "response_format" in r.json()["error"]["message"]


def test_bad_format_checked_before_synthesis(monkeypatch, tmp_path):
    """格式错必须在**合成之前**返回 —— 不能先烧一次显存再报 400。"""
    _stub_voices(monkeypatch, ["kangaroo"], selected="kangaroo")

    def should_not_run(*a, **kw):
        raise AssertionError("格式非法时不应调用 synth_wav")

    monkeypatch.setattr("tts_api.synth_wav", should_not_run)
    r = _client().post(
        "/v1/audio/speech", json={"input": "你好", "voice": "default", "response_format": "xyz"}
    )
    assert r.status_code == 400


def test_speech_success_returns_audio_bytes(monkeypatch, tmp_path):
    """打桩 synth_wav，验证成功路径返回音频字节 + 正确 Content-Type。"""
    _stub_voices(monkeypatch, ["kangaroo"], selected="kangaroo")

    wav = tmp_path / "stub.wav"
    wav.write_bytes(b"\x00" * 44 + b"\x11\x22")

    def fake_synth(text, voice_id="", text_language="zh", **kw):
        fake_synth.calls.append((text, voice_id, text_language))
        return wav, 1.0, voice_id

    fake_synth.calls = []
    monkeypatch.setattr("tts_api.synth_wav", fake_synth)

    r = _client().post(
        "/v1/audio/speech",
        json={"input": "你好世界", "voice": "kangaroo", "response_format": "wav"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    assert r.content == b"\x00" * 44 + b"\x11\x22"
    assert fake_synth.calls == [("你好世界", "kangaroo", "zh")]


def test_synth_failure_becomes_openai_error(monkeypatch, tmp_path):
    """合成抛异常 → 500 + OpenAI 错误体（而不是 FastAPI 的 detail）。"""
    _stub_voices(monkeypatch, ["kangaroo"], selected="kangaroo")

    def boom(*a, **kw):
        raise RuntimeError("显存不足")

    monkeypatch.setattr("tts_api.synth_wav", boom)

    r = _client().post(
        "/v1/audio/speech", json={"input": "你好", "voice": "kangaroo", "response_format": "wav"}
    )
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "synthesis_failed"
    assert "显存不足" in err["message"]
