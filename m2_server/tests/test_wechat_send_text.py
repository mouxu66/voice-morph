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
    # 2026-09-18：send_text 现在会在锁内做只读预检（_send_preflight），
    # conftest 把 VM_WECHAT_RESTART 强制成 0 → 预检会真去枚举微信窗口。
    # 测试机微信开不开都会让结果抖动，这里统一打桩成「微信就绪」；
    # 预检本身的分因行为在 test_wechat_restart.py 里专门测。
    monkeypatch.setattr(wv.wproc, "list_wechat_processes",
                        lambda: [{"pid": 1, "name": "Weixin.exe", "exe": "C:/wx/Weixin.exe"}])
    monkeypatch.setattr(wv.wproc, "enum_wechat_windows",
                        lambda: [{"hwnd": 11, "pid": 1, "area": 2_000_000, "exe": "C:/wx/Weixin.exe"}])
    # 切卡预热（_PendingApply）在 _do_send 之前的后台线程就跑 _run_audio，
    # 而本机 audio_config.ps1 真实存在 → 不打桩就会真起 PowerShell 动声卡。
    # 这里默认打桩，需要记录调用的测试自行覆盖。
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True})
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

    def _do(req, pre_apply=None):
        calls["send"] = req.wav
        # 模拟真 _do_send 的契约：领了 pre_apply 就必须消费（消费后 send_text 不再 abandon）
        if pre_apply is not None:
            pre_apply.result()
            calls["pre_apply_consumed"] = True
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
    """文字 → 合成 → RVC 换声 → 发送，steps 里要说清楚换了哪个音色。

    顺带锁死切卡预热契约：_do_send 拿到的 pre_apply 必须被消费。
    """
    calls = _fake_send(tmp_path, monkeypatch)
    res = wv.send_text(wv.SendTextReq(text="你好", voice_id="kangaroo"))
    assert res["ok"] is True
    assert calls["synth"][0] == "你好"
    assert calls["rvc"][1] == "kangaroo_v2"           # 自动推出 RVC 实验名
    assert calls["send"] == "tts_fake_kangaroo_v2.wav"  # 发的是**换声后**的文件
    assert calls.get("pre_apply_consumed") is True     # 切卡任务被 _do_send 消费
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


# ---------------- 切卡与 TTS 并行（_PendingApply，2026-09-10 延迟优化） ----------------

class _FakeProc:
    """假播放子进程（同 test_wechat_retry）：绝不能让单测真的启动 RVC venv 播音频。"""

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def _make_wav_like_retry(tmp_path, name="tts_x.wav"):
    """真 wav 字节（_wav_duration 被打桩时不读内容，但 _do_send 仍要求文件存在）。"""
    import io
    import numpy as np
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, np.zeros(1600, dtype=np.float32), 16000, format="WAV")
    p = tmp_path / name
    p.write_bytes(buf.getvalue())
    return p


def test_pending_apply_result_returns_run_audio_result(monkeypatch):
    """result() 返回后台 _run_audio('apply') 的结果，且只调一次。"""
    calls = []
    monkeypatch.setattr(wv, "_run_audio", lambda a: calls.append(a) or {"ok": True, "action": a})
    t = wv._PendingApply()
    res = t.result()
    assert res == {"ok": True, "action": "apply"}
    assert calls == ["apply"]


def test_pending_apply_propagates_error(monkeypatch):
    """后台 apply 失败 → result() 原样抛出，交给 _do_send 的异常路径。"""
    monkeypatch.setattr(wv, "_run_audio", lambda a: (_ for _ in ()).throw(RuntimeError("apply boom")))
    t = wv._PendingApply()
    with pytest.raises(RuntimeError, match="apply boom"):
        t.result()


def test_pending_apply_abandon_restores_after_success(monkeypatch):
    """abandon()（未消费早退）：必须还原声卡，绝不把默认麦留在 CABLE 上。"""
    calls = []
    monkeypatch.setattr(wv, "_run_audio", lambda a: calls.append(a) or {"ok": True})
    t = wv._PendingApply()
    t.abandon()
    assert calls == ["apply", "restore"]


def test_pending_apply_abandon_restores_after_failure(monkeypatch):
    """apply 本身失败后 abandon：restore 可能同样失败，不能让异常冒出 abandon。"""
    calls = []
    def fake(a):
        calls.append(a)
        raise RuntimeError("no device")
    monkeypatch.setattr(wv, "_run_audio", fake)
    t = wv._PendingApply()
    t.abandon()          # 不应抛
    assert calls == ["apply", "restore", "reset"]   # restore 失败 → _safe_restore 用 reset 兑底


def test_pending_apply_abandon_noop_after_consume(monkeypatch):
    """result() 消费后 abandon 是空操作：还原已由 _do_send 自己的路径负责。"""
    calls = []
    monkeypatch.setattr(wv, "_run_audio", lambda a: calls.append(a) or {"ok": True})
    t = wv._PendingApply()
    t.result()
    t.abandon()
    assert calls == ["apply"]


def test_do_send_uses_pre_apply_instead_of_second_sync_apply(tmp_path, monkeypatch):
    """传了 pre_apply：_do_send 只等尾差，绝不能再同步跑第二次 apply（否则白省）。"""
    _make_wav_like_retry(tmp_path)
    calls = []
    def fake_run(a):
        calls.append(a)
        return {"ok": True}
    monkeypatch.setattr(wv, "_run_audio", fake_run)

    class FakeTask:
        def __init__(self):
            self.consumed = False
        def result(self):
            self.consumed = True
            return {"ok": True}
        def abandon(self):
            pass
    task = FakeTask()
    monkeypatch.setattr(wv, "_PendingApply", FakeTask)
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: None)
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_finish_record", lambda: True)
    monkeypatch.setattr(wv, "_start_play", lambda w: _FakeProc())
    monkeypatch.setattr(wv, "_wait_play_start", lambda p, t=40.0: True)
    monkeypatch.setattr(wv, "_wait_play_done", lambda p, d: None)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 1.0)
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    res = wv._do_send(wv.SendVoiceReq(wav="tts_x.wav"), pre_apply=task)
    assert res["outcome"] == "ok"
    assert task.consumed is True
    assert calls == []                   # 同步 apply 一次都没跑
    assert any("并行" in s for s in res["steps"])


def test_do_send_without_pre_apply_keeps_sync_apply(tmp_path, monkeypatch):
    """不传 pre_apply（/send_voice、tools 直连）：保持原地同步切卡，行为不变。"""
    _make_wav_like_retry(tmp_path)
    calls = []
    monkeypatch.setattr(wv, "_run_audio", lambda a: calls.append(a) or {"ok": True})
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: None)
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_finish_record", lambda: True)
    monkeypatch.setattr(wv, "_start_play", lambda w: _FakeProc())
    monkeypatch.setattr(wv, "_wait_play_start", lambda p, t=40.0: True)
    monkeypatch.setattr(wv, "_wait_play_done", lambda p, d: None)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 1.0)
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    res = wv._do_send(wv.SendVoiceReq(wav="tts_x.wav"))
    assert res["outcome"] == "ok"
    assert calls == ["apply"]            # 原地同步 apply，和优化前一致


def test_send_text_starts_pending_apply_and_frees_it_on_tts_failure(tmp_path, monkeypatch):
    """send_text 一拿锁就起预热任务；TTS 失败走 abandon 兜底还原声卡。"""
    calls = []
    def fake_run(a):
        calls.append(a)
        return {"ok": True}
    monkeypatch.setattr(wv, "_run_audio", fake_run)
    monkeypatch.setattr(tts_api, "synth_wav", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tts boom")))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        wv.send_text(wv.SendTextReq(text="你好"))
    assert e.value.status_code == 500
    assert "tts boom" in e.value.detail
    assert calls == ["apply", "restore"]   # 预热被 abandon()：切了卡又还原，无残留
