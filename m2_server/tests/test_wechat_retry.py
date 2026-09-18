"""F5 微信语音自动重试单测（mock 掉 ctypes / 音频 / 播放）。"""
import json
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
    # 禁止单测触碰真实微信：UIA 视为不可用（否则 _do_send 会真点微信）
    monkeypatch.setattr(wv, "_uia_ready", lambda: False)
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


def test_restore_async_writes_back_to_history(monkeypatch, tmp_path):
    """_restore_async 应调用 _safe_restore 并把结果写回发送历史最后一条。"""
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    hist_file = tmp_path / "wechat_send_history.json"
    hist_file.write_text(json.dumps([{"wav": "tts_x.wav", "duration_s": 1.0,
                                      "ts": 1, "outcome": "ok"}]), encoding="utf-8")
    monkeypatch.setattr(wv, "HISTORY_FILE", hist_file)
    wv._restore_async()          # 直接调用（即后台线程实体），同步跑完
    data = json.loads(hist_file.read_text("utf-8"))
    assert data[-1]["restored"] is True
    assert data[-1].get("restore_error", "MISSING") == ""


# ---------------- _do_send 成功 / 降级 / 失败 ----------------

class _FakeProc:
    """假播放子进程：绝不能让单测真的去启动 RVC venv 播音频。"""

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

def test_do_send_success_outcome_ok(tmp_path, monkeypatch):
    _make_wav(tmp_path)
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True})
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: None)
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_finish_record", lambda: True)   # 点击发送成功
    monkeypatch.setattr(wv, "_start_play", lambda w: _FakeProc())
    monkeypatch.setattr(wv, "_wait_play_start", lambda p, t=40.0: True)
    monkeypatch.setattr(wv, "_wait_play_done", lambda p, d: None)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 1.0)
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    from wechat_voice import SendVoiceReq
    res = wv._do_send(SendVoiceReq(wav="tts_x.wav"))
    assert res["ok"] is True
    assert res["outcome"] == "ok"
    # 声卡还原已交后台线程，立即返回时 restored=None（pending），最终结果写回历史
    assert res["restored"] is None
    assert any("后台线程" in s for s in res["steps"])


def test_do_send_failure_auto_fallback(tmp_path, monkeypatch):
    _make_wav(tmp_path)
    monkeypatch.setattr(wv, "AUTO_FALLBACK", True)
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True})
    # 新结构：前台化+定位+按下都并入 _trigger_record，失败注入点改为它
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("微信窗口找不到")))
    monkeypatch.setattr(wv, "_finish_record", lambda: None)
    monkeypatch.setattr(wv, "_start_play", lambda w: _FakeProc())
    monkeypatch.setattr(wv, "_wait_play_start", lambda p, t=40.0: True)
    monkeypatch.setattr(wv, "_wait_play_done", lambda p, d: None)
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
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
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


# ---------------- 录音环境告警落库（2026-09-18 静音事故） ----------------

def test_append_history_records_binding_warning(tmp_path):
    """告警必须随记录落库。

    此前它只活在 API 响应的 steps/summary 里，事后翻发送历史查不到任何线索 ——
    17:35 那条静音语音就是这么被漏掉的（前端把 summary 尾部的 ⚠ 当成了旧文案）。
    """
    wav = _make_wav(tmp_path)
    warn = "微信上次录音用的是「麦克风阵列 (Senary Audio)」而不是 VB-Audio Virtual Cable"
    wv._append_history(wav, 5.4, "ok", warning=warn)
    items = wv.send_history()["items"]
    assert items[-1]["warning"] == warn
    assert items[-1]["outcome"] == "ok"


def test_append_history_omits_warning_when_clean(tmp_path):
    """无告警时不写 warning 字段，别给旧前端塞空串。"""
    wav = _make_wav(tmp_path)
    wv._append_history(wav, 1.0, "ok")
    assert "warning" not in wv.send_history()["items"][-1]


def test_do_send_persists_env_warning(monkeypatch, tmp_path):
    """端到端复刻：_prepare_recording_env 报出绑定告警 → 发送历史里必须查得到。

    复刻 2026-09-18 17:35 现场：RESTART=0 + 微信绑在物理麦上 → 发出去是静音，
    当时发送历史里却什么都没有，只能靠用户耳朵发现。
    """
    wav = _make_wav(tmp_path, "tts_1789724088659_kangaroo_v2.wav")
    warn = "微信上次录音用的是「麦克风阵列 (Senary Audio)」而不是 VB-Audio Virtual Cable"
    monkeypatch.setattr(wv, "_prepare_recording_env", lambda: {
        "kind": "recording_env", "summary": f"麦克风已切到 CABLE Output；⚠ {warn}",
        "warning": warn})
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 5.4)
    monkeypatch.setattr(wv, "_start_play", lambda p: None)
    monkeypatch.setattr(wv, "_wait_play_start", lambda p: True)
    monkeypatch.setattr(wv, "_wait_play_done", lambda p, d: None)
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_finish_record", lambda: True)
    # ⚠️ 必须**带参**：真实签名是 `_restore_async(history_file=None)`，调用点
    # （wechat_voice.py 的 `Thread(target=_restore_async, args=(HISTORY_FILE,))`）
    # 会传一个位置参数。写成 `lambda: None` 会让线程里抛
    # `TypeError: <lambda>() takes 0 positional arguments but 1 was given` ——
    # 而异常发生在 daemon 线程里，**只产生 warning、测试照样绿**（假绿）。
    # 2026-09-18 用 `-W error::pytest.PytestUnhandledThreadExceptionWarning` 才把它逼出来。
    monkeypatch.setattr(wv, "_restore_async", lambda history_file=None: None)
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: 1)
    monkeypatch.setattr(wv, "_ensure_onscreen", lambda h: None)
    monkeypatch.setattr(wv, "_window_rect", lambda h: (0, 0, 100, 100))
    monkeypatch.setattr(wv, "_find_mic_icon", lambda r: (10, 10))
    monkeypatch.setattr(wv, "_mic_point", lambda r: (10, 10))

    from wechat_voice import SendVoiceReq
    res = wv._do_send(SendVoiceReq(wav=wav.name))

    assert res["outcome"] == "ok"
    assert "⚠" in " ".join(res["steps"])          # 响应里照旧带告警
    assert wv.send_history()["items"][-1]["warning"] == warn   # 历史里也必须留痕
