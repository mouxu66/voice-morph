"""F5 微信语音自动重试单测（mock 掉 ctypes / 音频 / 播放）。"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import config as cfg  # noqa: E402
import wechat_voice as wv  # noqa: E402


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(wv, "HISTORY_FILE", tmp_path / "wechat_send_history.json")
    yield tmp_path


def _make_wav(tmp_path, name="tts_x.wav"):
    import io
    import numpy as np
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, np.zeros(1600, dtype=np.float32), 16000, format="WAV")
    p = tmp_path / name
    p.write_bytes(buf.getvalue())
    return p


# ---------------- _safe_restore ----------------

def test_safe_restore_ok(monkeypatch):
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True})
    ok, err = wv._safe_restore()
    assert ok is True and err == ""


def test_safe_restore_fallback_to_reset(monkeypatch):
    calls = []

    def fake(a):
        calls.append(a)
        if a == "restore":
            raise RuntimeError("restore boom")
        return {"ok": True}
    monkeypatch.setattr(wv, "_run_audio", fake)
    ok, err = wv._safe_restore()
    assert ok is True
    assert "reset" in err
    assert calls == ["restore", "reset"]


def test_safe_restore_both_fail(monkeypatch):
    def fake(a):
        raise RuntimeError("boom")
    monkeypatch.setattr(wv, "_run_audio", fake)
    ok, err = wv._safe_restore()
    assert ok is False
    assert "reset" in err


# ---------------- _do_send 成功 / 降级 / 失败 ----------------

def test_do_send_success_outcome_ok(tmp_path, monkeypatch):
    _make_wav(tmp_path)
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True})
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: None)
    monkeypatch.setattr(wv, "_trigger_record", lambda: None)
    monkeypatch.setattr(wv, "_finish_record", lambda: True)   # 点击发送成功
    monkeypatch.setattr(wv, "_play_to_cable", lambda w, d: None)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 1.0)
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    from wechat_voice import SendVoiceReq
    res = wv._do_send(SendVoiceReq(wav="tts_x.wav"))
    assert res["ok"] is True
    assert res["outcome"] == "ok"
    assert res["restored"] is True


def test_do_send_failure_auto_fallback(tmp_path, monkeypatch):
    _make_wav(tmp_path)
    monkeypatch.setattr(wv, "AUTO_FALLBACK", True)
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True})
    # 新结构：前台化+定位+按下都并入 _trigger_record，失败注入点改为它
    monkeypatch.setattr(wv, "_trigger_record", lambda: (_ for _ in ()).throw(RuntimeError("微信窗口找不到")))
    monkeypatch.setattr(wv, "_finish_record", lambda: None)
    monkeypatch.setattr(wv, "_play_to_cable", lambda w, d: None)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 1.0)
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    monkeypatch.setattr(wv, "_key", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_guided_fallback", lambda w, d, s: {"steps": ["已降级"], "hint": "h"})
    from wechat_voice import SendVoiceReq
    res = wv._do_send(SendVoiceReq(wav="tts_x.wav"))
    assert res["outcome"] == "manual_fallback"
    assert res["ok"] is True
    assert res["fallback"]["hint"]


def test_do_send_failure_no_fallback(tmp_path, monkeypatch):
    _make_wav(tmp_path)
    monkeypatch.setattr(wv, "AUTO_FALLBACK", False)
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True})
    monkeypatch.setattr(wv, "_trigger_record", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(wv, "_finish_record", lambda: None)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 1.0)
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    from wechat_voice import SendVoiceReq
    res = wv._do_send(SendVoiceReq(wav="tts_x.wav"))
    assert res.status_code == 500
    body = res.body  # JSONResponse.body
    import json
    data = json.loads(body)
    assert data["outcome"] == "failed"


# ---------------- history outcome ----------------

def test_append_history_with_outcome(tmp_path):
    wav = _make_wav(tmp_path)
    wv._append_history(wav, 1.0, "ok")
    wv._append_history(wav, 1.0, "manual_fallback")
    items = wv.send_history()["items"]
    assert len(items) == 2
    assert items[0]["outcome"] == "ok"
    assert items[1]["outcome"] == "manual_fallback"


def test_send_history_backfills_old_outcome(tmp_path):
    """旧记录无 outcome 字段 → 补默认 ok。"""
    import json
    (tmp_path / "wechat_send_history.json").write_text(
        json.dumps([{"wav": "a.wav", "duration_s": 1.0, "ts": 1}]), encoding="utf-8")
    items = wv.send_history()["items"]
    assert items[0]["outcome"] == "ok"
