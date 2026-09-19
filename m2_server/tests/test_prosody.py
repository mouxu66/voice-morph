"""语气中转（prosody relay）与 RVC 权重查找的单元测试。

背景：RVC 只换音色、保留源音频韵律；想换语气必须先把韵律重铸
（ASR 转文字 → TTS 用目标音色的参考音重新合成），即本模块职责。
"""

import io
import wave

import config as cfg
import prosody_relay
import pytest
from rvc_common import find_index


def _wav_bytes(frames: int = 2400, sr: int = 24000) -> bytes:
    """造一段静音 wav 的字节（模拟 TTS 返回）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


@pytest.fixture()
def relay_env(tmp_path, monkeypatch):
    """把音色库指向临时目录，与真实环境隔离。"""
    vb = tmp_path / "voicebank"
    vb.mkdir()
    monkeypatch.setattr(prosody_relay, "VOICEBANK", vb)
    monkeypatch.setattr(prosody_relay, "OUT", tmp_path / "outputs")
    return vb, tmp_path


def test_resolve_ref_audio_prefers_voicebank(relay_env):
    """有参考音频就用它（语气与目标音色同源）。"""
    vb, _ = relay_env
    ref = vb / "merg_004" / "reference.wav"
    ref.parent.mkdir(parents=True)
    ref.write_bytes(b"RIFF")
    assert prosody_relay.resolve_ref_audio("merg_004") == str(ref)


def test_resolve_ref_audio_falls_back_to_default(relay_env):
    """没有参考音频（市场下载的多数如此）回退内置默认，保证链路出声。"""
    assert prosody_relay.resolve_ref_audio("katoong_lanyangyang") == str(prosody_relay.DEFAULT_REF)
    assert prosody_relay.resolve_ref_audio("") == str(prosody_relay.DEFAULT_REF)


def test_resolve_ref_text_reads_meta(relay_env):
    """meta.json 有 ref_text 则用，没有或损坏则返回空（走 x-vector 模式）。"""
    vb, _ = relay_env
    d = vb / "merg_004"
    d.mkdir(parents=True)
    (d / "meta.json").write_text('{"ref_text": "你好"}', encoding="utf-8")
    assert prosody_relay.resolve_ref_text("merg_004") == "你好"
    assert prosody_relay.resolve_ref_text("nonexistent") == ""


def test_relay_rewrites_audio(relay_env, monkeypatch):
    """中转 = ASR 拿文本 → TTS 合成 → 落盘中继 wav（音色仍是 TTS 的，待 RVC）。"""
    _, tmp_path = relay_env
    src = tmp_path / "src.wav"
    src.write_bytes(_wav_bytes())

    captured = {}
    monkeypatch.setattr(
        prosody_relay.qwen3_tts, "transcribe", lambda path: {"text": "今天天气不错"}
    )
    monkeypatch.setattr(
        prosody_relay.qwen3_tts,
        "tts",
        lambda text, ref_audio, ref_text, **kw: captured.update(text=text, ref_audio=ref_audio)
        or _wav_bytes(),
    )

    out = prosody_relay.relay(src, "merg_004")
    assert out.is_file()
    assert captured["text"] == "今天天气不错"
    assert captured["ref_audio"] == str(prosody_relay.DEFAULT_REF)  # 该音色无参考音


def test_relay_rejects_short_transcript(relay_env, monkeypatch):
    """转写文本过短（噪声/空音频）直接报错，不把一句噪声放大成一段胡话。"""
    _, tmp_path = relay_env
    src = tmp_path / "src.wav"
    src.write_bytes(_wav_bytes())
    monkeypatch.setattr(prosody_relay.qwen3_tts, "transcribe", lambda path: {"text": "嗯"})
    with pytest.raises(RuntimeError, match="转写文本过短"):
        prosody_relay.relay(src, "merg_004")


def test_relay_surfaces_asr_error(relay_env, monkeypatch):
    """ASR 失败要抛错，由调用方决定降级还是报错，不能静默返回空文本。"""
    _, tmp_path = relay_env
    src = tmp_path / "src.wav"
    src.write_bytes(_wav_bytes())
    monkeypatch.setattr(
        prosody_relay.qwen3_tts, "transcribe", lambda path: {"error": "whisper 未加载"}
    )
    with pytest.raises(RuntimeError, match="whisper 未加载"):
        prosody_relay.relay(src, "merg_004")


def test_find_index(tmp_path, monkeypatch):
    """特征检索库 added_*.index：有则返回，没有返回 None（仍能推理，只是不检索）。"""
    monkeypatch.setattr(cfg, "RVC_ROOT", tmp_path)
    exp_dir = tmp_path / "logs" / "kangaroo_v2"
    exp_dir.mkdir(parents=True)
    assert find_index("kangaroo_v2") is None
    idx = exp_dir / "added_IVF512_Flat_nprobe_1_kangaroo_v2_v2.index"
    idx.write_bytes(b"")
    assert find_index("kangaroo_v2") == idx


def test_offlinevc_run_rejects_bad_prosody():
    """prosody 只接受 keep/relay；非法值在动工前就 400，不浪费后面几十秒推理。"""
    import server
    from fastapi.testclient import TestClient

    client = TestClient(server.app)
    resp = client.post(
        "/api/offlinevc/run",
        data={"voice_id": "x", "prosody": "bogus"},
        files={"file": ("a.wav", b"RIFF", "audio/wav")},
    )
    assert resp.status_code == 400
    assert "prosody" in resp.json()["detail"]
