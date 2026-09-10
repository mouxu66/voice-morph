"""一键发送 `/api/wechat/send_text`（文字 → TTS → RVC → 自动发微信）单测。

背景：桌宠「合成并发送」原本走 `/api/tts` + `play_to_cable`（半自动，还得用户自己按住 Alt），
且**整条链路没有 RVC** —— 袋鼠音色的唯一来源被漏掉了。这里锁定新链路的组装行为。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import config as cfg  # noqa: E402
import rvc_convert  # noqa: E402
import tts_api  # noqa: E402
import wechat_voice as wv  # noqa: E402


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(wv, "HISTORY_FILE", tmp_path / "wechat_send_history.json")
    # 绝不能让单测真去点微信 / 真跑 RVC / 真加载 TTS
    monkeypatch.setattr(wv, "_uia_ready", lambda: False)
    yield tmp_path


def _fake_send(tmp_path, monkeypatch, ret=None):
    """打桩 TTS / RVC / _do_send，返回记录用的 dict。"""
    calls = {}

    def _synth(text, voice_id="", *a, **kw):
        calls["synth"] = (text, voice_id)
        f = tmp_path / "tts_fake.wav"
        f.write_bytes(b"RIFF")
        return f, 3.0, voice_id or "kangaroo"

    def _rvc(wav, voice, pitch=0, index_rate=0.5):
        calls["rvc"] = (Path(wav).name, voice, pitch, index_rate)
        out = tmp_path / f"{Path(wav).stem}_{voice}.wav"
        out.write_bytes(b"RIFF")
        return out

    def _do(req):
        calls["send"] = req.wav
        return {"ok": True, "outcome": "ok", "steps": ["已发送"], "restored": True}

    monkeypatch.setattr(tts_api, "synth_wav", _synth)
    monkeypatch.setattr(rvc_convert, "rvc_convert", _rvc)
    monkeypatch.setattr(rvc_convert, "resolve_model", lambda v: (Path("x.pth"), None))
    monkeypatch.setattr(wv, "_do_send", _do)
    return calls


# ---------------- resolve_rvc_voice：voicebank id → RVC 实验名 ----------------

def test_resolve_rvc_voice_prefers_v2(monkeypatch):
    """kangaroo → kangaroo_v2（现役主力），不是 kangaroo（voicebank 目录名不同）。"""
    seen = []

    def _resolve_model(voice):
        seen.append(voice)
        if voice != "kangaroo_v2":
            raise rvc_convert.RvcError("nope")
        return (Path("x.pth"), None)

    monkeypatch.setattr(rvc_convert, "resolve_model", _resolve_model)
    assert rvc_convert.resolve_rvc_voice("kangaroo") == "kangaroo_v2"
    assert seen[0] == "kangaroo_v2"      # 主力优先，先试它


def test_resolve_rvc_voice_falls_back_to_40k(monkeypatch):
    """v2 没有时退 40k（实际目录名是 kangaroo_v2_40k，不是 kangaroo_40k）。"""
    monkeypatch.setattr(rvc_convert, "resolve_model",
                        lambda v: (Path("x.pth"), None) if v == "kangaroo_v2_40k"
                        else (_ for _ in ()).throw(rvc_convert.RvcError("nope")))
    assert rvc_convert.resolve_rvc_voice("kangaroo") == "kangaroo_v2_40k"


def test_resolve_rvc_voice_none_when_missing(monkeypatch):
    monkeypatch.setattr(rvc_convert, "resolve_model",
                        lambda v: (_ for _ in ()).throw(rvc_convert.RvcError("nope")))
    assert rvc_convert.resolve_rvc_voice("nosuch") is None
    assert rvc_convert.resolve_rvc_voice("") is None


# ---------------- /api/wechat/send_text ----------------

def test_send_text_full_chain(tmp_path, monkeypatch):
    """文字 → 合成 → RVC 换声 → 发送，steps 里要说清楚换了哪个音色。"""
    calls = _fake_send(tmp_path, monkeypatch)
    res = wv.send_text(wv.SendTextReq(text="你好", voice_id="kangaroo"))
    assert res["ok"] is True
    assert calls["synth"][0] == "你好"
    assert calls["rvc"][1] == "kangaroo_v2"           # 自动推出 RVC 实验名
    assert calls["send"] == "tts_fake_kangaroo_v2.wav"  # 发的是**换声后**的文件
    assert any("RVC 换声" in s for s in res["steps"])


def test_send_text_honors_explicit_rvc_voice(tmp_path, monkeypatch):
    calls = _fake_send(tmp_path, monkeypatch)
    wv.send_text(wv.SendTextReq(text="你好", voice_id="kangaroo", rvc_voice="katoong_manbo"))
    assert calls["rvc"][1] == "katoong_manbo"


def test_send_text_warns_when_no_rvc(tmp_path, monkeypatch):
    """找不到 RVC 音色时照发，但必须显式警告——否则又是一条"不像袋鼠"的语音。"""
    calls = _fake_send(tmp_path, monkeypatch)
    monkeypatch.setattr(rvc_convert, "resolve_rvc_voice", lambda v: None)
    res = wv.send_text(wv.SendTextReq(text="你好", voice_id="nosuch"))
    assert "rvc" not in calls                 # 没换声
    assert any("未换声" in s for s in res["steps"])
    assert calls["send"] == "tts_fake.wav"    # 发的是原始 TTS 产物


def test_send_text_rejects_empty_text():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        wv.send_text(wv.SendTextReq(text="   "))
    assert e.value.status_code == 400


def test_send_text_route_registered():
    """桌宠调的是这个路由，别哪天改名了没人发现。

    include_router 是惰性挂载，枚举 app.routes 看不到子路由（见 test_server.py），
    故用真实请求验证：空 text 应返回 400 而不是 404。
    """
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    resp = client.post("/api/wechat/send_text", json={"text": "  "})
    assert resp.status_code == 400      # 路由在，被参数校验拦下（404 才是没注册）
