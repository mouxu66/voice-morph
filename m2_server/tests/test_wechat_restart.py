"""「录音前重启微信」链路单测（纯逻辑，绝不真杀/真拉起微信）。

背景：微信在**进程启动时**绑定采集设备，之后再改 Windows 默认麦克风对它不热生效
（2026-09-11 实测，证据链见 docs/犯错指南.md §2.15）。症状是「CABLE 里信号满幅、
微信却录到安静房间声、发出去听着是静音」。
本文件锁住三件事：
    1. wechat_proc 的纯函数（设备名解析、判定、等待逻辑）
    2. _need_wechat_restart 的 auto 判据（漏重启=静音语音发出去，代价不对称）
    3. _prepare_recording_env 的执行顺序：**杀微信 → 切卡 → 拉起微信**（反了就白切）
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import wechat_proc as wp  # noqa: E402
import wechat_voice as wv  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_side_effects(monkeypatch):
    """这条链路的真机副作用是「杀掉并重新拉起用户的微信」，单测里一律禁掉。

    conftest.py 已把 VM_WECHAT_RESTART 强制成 0；这里再把 wechat_proc 的
    进程枚举/强杀全部打桩，任何漏网的调用都会立刻炸成 AttributeError 而不是动真机。
    """
    monkeypatch.setattr(wp, "list_wechat_processes", lambda: [])
    monkeypatch.setattr(wp, "enum_wechat_windows", lambda: [])
    monkeypatch.setattr(wp, "_force_kill", lambda pids: None)
    monkeypatch.setattr(wp, "_registry_exe", lambda: None)
    monkeypatch.setattr(wp, "DEFAULT_EXE_CANDIDATES", ())
    monkeypatch.setattr(wv, "_run_audio", lambda a: {"ok": True, "action": a})
    monkeypatch.delenv("VM_WECHAT_EXE", raising=False)


# ---------------- wechat_proc：进程名与设备名解析 ----------------

def test_proc_name_match_is_case_insensitive():
    assert wp._name_ok("Weixin.exe") is True
    assert wp._name_ok("wechat.EXE") is True
    assert wp._name_ok("WeChatApp.exe") is True
    assert wp._name_ok("WeChatAppEx.exe") is False     # 小程序宿主不是主进程
    assert wp._name_ok("explorer.exe") is False
    assert wp._name_ok("") is False


def _blob(device: str) -> bytes:
    """造一份形如微信真实遥测的字节流（结构照抄本机实测样本）。"""
    head = b"\x00\x01kv-misc:"
    payload = (b"start_record,x,1789136410307,,{...}"
               b"\x12\x0096,1,165,184,205,0,0,0,0," + device.encode("utf-8") + b",,,"
               b"\x12\x00end_record,x,1789136416911,,{...}")
    return head + payload


def test_device_from_blob_reads_device_name():
    assert wp._device_from_blob(_blob("麦克风阵列 (Senary Audio)")) == "麦克风阵列 (Senary Audio)"
    assert wp._device_from_blob(
        _blob("CABLE Output (VB-Audio Virtual Cable)")) == "CABLE Output (VB-Audio Virtual Cable)"


def test_device_from_blob_requires_record_context():
    """没有 start_record 的孤立 CSV 行不算证据（否则会拿别的统计行当设备名）。"""
    stray = (b"\x12\x0096,1,165,184,205,0,0,0,0,"
             + "麦克风阵列 (Senary Audio)".encode("utf-8") + b",,,")
    assert wp._device_from_blob(stray) is None


def test_device_from_blob_ignores_nameless_csv():
    """9 个数字后面跟的不是带括号的设备名 → 丢弃（实测裸数字前缀会命中 37 条假阳性）。"""
    assert wp._device_from_blob(_blob("113.105.153.166")) is None


def test_input_device_probe_picks_newest_file(tmp_path, monkeypatch):
    monkeypatch.setattr(wp, "_KVCOMM", tmp_path)
    (tmp_path / "old_input.statistic").write_bytes(_blob("麦克风阵列 (Senary Audio)"))
    new = tmp_path / "new_input.statistic"
    new.write_bytes(_blob("CABLE Output (VB-Audio Virtual Cable)"))
    os.utime(tmp_path / "old_input.statistic", (1, 1))
    probe = wp.input_device_probe()
    assert probe["device"] == "CABLE Output (VB-Audio Virtual Cable)"
    assert probe["file"] == new.name


def test_input_device_probe_missing_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(wp, "_KVCOMM", tmp_path / "nope")
    assert wp.input_device_probe() == {"device": None, "file": None, "hits": 0}
    assert wp.last_input_device() is None


# ---------------- wechat_proc：杀 / 拉起 / 等就绪 ----------------

def test_kill_wechat_noop_when_not_running(monkeypatch):
    killed = []
    monkeypatch.setattr(wp, "_force_kill", lambda pids: killed.append(pids))
    r = wp.kill_wechat(grace_s=0)
    assert r == {"pids": [], "graceful": False, "forced": False, "remaining": []}
    assert killed == []          # 没进程绝不能乱杀


def test_kill_wechat_forces_after_grace(monkeypatch):
    """微信「关闭=收进托盘」，WM_CLOSE 基本不真退出 → 宽限期过后必须强杀。"""
    killed = []

    def fake_list():
        return [] if killed else [{"pid": 111, "name": "Weixin.exe", "exe": "x"}]

    monkeypatch.setattr(wp, "list_wechat_processes", fake_list)
    monkeypatch.setattr(wp, "_force_kill", lambda pids: killed.extend(pids))
    r = wp.kill_wechat(grace_s=0)
    assert r["pids"] == [111]
    assert r["forced"] is True
    assert r["remaining"] == []
    assert killed == [111]


def test_start_wechat_rejects_missing_exe(tmp_path):
    with pytest.raises(RuntimeError, match="不存在"):
        wp.start_wechat(tmp_path / "nope.exe")


def test_wait_wechat_ready_times_out_when_no_process(monkeypatch):
    with pytest.raises(RuntimeError, match="微信进程没起来"):
        wp.wait_wechat_ready(timeout_s=0.05, poll=0.01)


def test_wait_wechat_ready_reports_login_page(monkeypatch):
    """只有小窗（登录二维码）时不能硬着头皮录——坐标全错，必须报错让人先登录。"""
    monkeypatch.setattr(wp, "list_wechat_processes", lambda: [{"pid": 9}])
    monkeypatch.setattr(wp, "enum_wechat_windows",
                        lambda: [{"hwnd": 5, "pid": 9, "area": 300 * 420, "exe": "x"}])
    with pytest.raises(RuntimeError, match="登录页"):
        wp.wait_wechat_ready(timeout_s=0.05, poll=0.01, min_area=200000)


def test_wait_wechat_ready_returns_after_window_grows(monkeypatch):
    calls = {"n": 0}

    def fake_wins():
        calls["n"] += 1
        small = {"hwnd": 5, "pid": 9, "area": 100, "exe": "x"}
        big = {"hwnd": 6, "pid": 9, "area": 1299 * 1609, "exe": "x"}
        return [small] if calls["n"] < 3 else [big]

    monkeypatch.setattr(wp, "enum_wechat_windows", fake_wins)
    r = wp.wait_wechat_ready(timeout_s=2, poll=0.01, min_area=200000)
    assert r["hwnd"] == 6
    assert r["area"] == 1299 * 1609


def test_resolve_wechat_exe_prefers_env(tmp_path, monkeypatch):
    fake = tmp_path / "Weixin.exe"
    fake.write_bytes(b"MZ")
    monkeypatch.setenv("VM_WECHAT_EXE", str(fake))
    assert wp.resolve_wechat_exe() == fake


def test_resolve_wechat_exe_from_running_process(monkeypatch, tmp_path):
    exe = tmp_path / "Weixin.exe"
    monkeypatch.setattr(wp, "enum_wechat_windows",
                        lambda: [{"hwnd": 1, "pid": 7, "area": 10, "exe": str(exe)}])
    assert wp.resolve_wechat_exe() == exe


def test_resolve_wechat_exe_none_when_nothing_found(monkeypatch):
    assert wp.resolve_wechat_exe() is None


# ---------------- 重启判据（auto 模式） ----------------

def test_restart_mode_default_auto(monkeypatch):
    monkeypatch.delenv(wv.RESTART_MODE_ENV, raising=False)
    assert wv._restart_mode() == "auto"


def test_device_keyword_takes_first_token():
    """播放端叫 "CABLE Input"、采集端叫 "CABLE Output (...)"，只有首词相同。"""
    assert wv._device_keyword() == wv.OUTPUT_DEVICE_KEYWORD.split()[0].lower()


def test_need_restart_respects_mode_zero(monkeypatch):
    monkeypatch.setenv(wv.RESTART_MODE_ENV, "0")
    need, why = wv._need_wechat_restart()
    assert need is False
    assert "已关闭重启" in why


def test_need_restart_mode_one_always(monkeypatch):
    monkeypatch.setenv(wv.RESTART_MODE_ENV, "1")
    assert wv._need_wechat_restart()[0] is True


def test_need_restart_auto_when_wechat_closed(monkeypatch):
    monkeypatch.setenv(wv.RESTART_MODE_ENV, "auto")
    need, why = wv._need_wechat_restart()
    assert need is True
    assert "没在运行" in why


@pytest.mark.parametrize("device,expect", [
    ("CABLE Output (VB-Audio Virtual Cable)", False),
    ("麦克风阵列 (Senary Audio)", True),
])
def test_need_restart_auto_from_telemetry(monkeypatch, device, expect):
    monkeypatch.setenv(wv.RESTART_MODE_ENV, "auto")
    monkeypatch.setattr(wp, "list_wechat_processes", lambda: [{"pid": 1}])
    monkeypatch.setattr(wp, "input_device_probe",
                        lambda: {"device": device, "file": "f", "hits": 1})
    need, why = wv._need_wechat_restart()
    assert need is expect
    assert device in why          # 判据必须写进原因里，出问题好复盘


def test_need_restart_auto_without_evidence_is_conservative(monkeypatch):
    """读不到证据时必须保守重启：漏重启 = 静音语音真的发出去，代价不对称。"""
    monkeypatch.setenv(wv.RESTART_MODE_ENV, "auto")
    monkeypatch.setattr(wp, "list_wechat_processes", lambda: [{"pid": 1}])
    monkeypatch.setattr(wp, "input_device_probe",
                        lambda: {"device": None, "file": None, "hits": 0})
    need, why = wv._need_wechat_restart()
    assert need is True
    assert "保守" in why


# ---------------- _prepare_recording_env ----------------

def test_prepare_env_without_restart_just_applies(monkeypatch):
    calls = []
    monkeypatch.setattr(wv, "_run_audio", lambda a: calls.append(a) or {"ok": True})
    monkeypatch.setattr(wv, "_need_wechat_restart", lambda: (False, "无需重启"))
    info = wv._prepare_recording_env()
    assert calls == ["apply"]
    assert info["restart"] is False
    assert "麦克风已切到 CABLE Output" in info["summary"]
    assert "无需重启" in info["summary"]


def test_prepare_env_restart_order_is_kill_apply_start(monkeypatch):
    """顺序硬约束：杀微信 → 切卡 → 拉起微信。切卡夹在中间才对。"""
    order = []
    monkeypatch.setenv(wv.RESTART_MODE_ENV, "1")
    monkeypatch.setattr(wv, "_need_wechat_restart", lambda: (True, "测试强制"))
    monkeypatch.setattr(wp, "resolve_wechat_exe", lambda: Path("C:/fake/Weixin.exe"))
    monkeypatch.setattr(wp, "kill_wechat",
                        lambda grace_s=2.5: order.append("kill") or {"pids": [111]})
    monkeypatch.setattr(wv, "_run_audio", lambda a: order.append(a) or {"ok": True})
    monkeypatch.setattr(wp, "start_wechat", lambda e: order.append("start") or 555)
    monkeypatch.setattr(wp, "wait_wechat_ready",
                        lambda timeout_s=90.0: order.append("wait") or
                        {"hwnd": 9, "pid": 555, "area": 2_000_000, "waited_s": 3.2})
    info = wv._prepare_recording_env()
    assert order == ["kill", "apply", "start", "wait"]
    assert info["restart"] is True
    assert info["new_pid"] == 555
    assert info["hwnd"] == 9
    assert "已重启微信" in info["summary"]
    assert "555" in info["summary"]


def test_prepare_env_relaunches_wechat_when_apply_fails(monkeypatch):
    """切卡失败也必须把微信拉回来——绝不因一次发送失败把用户的微信丢在死状态。"""
    order = []
    monkeypatch.setattr(wv, "_need_wechat_restart", lambda: (True, "测试强制"))
    monkeypatch.setattr(wp, "resolve_wechat_exe", lambda: Path("C:/fake/Weixin.exe"))
    monkeypatch.setattr(wp, "kill_wechat",
                        lambda grace_s=2.5: order.append("kill") or {"pids": [111]})
    monkeypatch.setattr(wv, "_run_audio",
                        lambda a: order.append(a) or (_ for _ in ()).throw(RuntimeError("apply boom")))
    monkeypatch.setattr(wp, "start_wechat", lambda e: order.append("start") or 555)
    with pytest.raises(RuntimeError, match="apply boom"):
        wv._prepare_recording_env()
    assert order == ["kill", "apply", "start"]


def test_prepare_env_needs_exe_path(monkeypatch):
    monkeypatch.setattr(wv, "_need_wechat_restart", lambda: (True, "测试强制"))
    monkeypatch.setattr(wp, "resolve_wechat_exe", lambda: None)
    with pytest.raises(RuntimeError, match="VM_WECHAT_EXE"):
        wv._prepare_recording_env()


# ---------------- _PendingRecordingEnv ----------------

def test_pending_env_returns_info_and_propagates_error(monkeypatch):
    monkeypatch.setattr(wv, "_prepare_recording_env", lambda: {"kind": "recording_env"})
    assert wv._PendingRecordingEnv().result() == {"kind": "recording_env"}

    monkeypatch.setattr(wv, "_prepare_recording_env",
                        lambda: (_ for _ in ()).throw(RuntimeError("prepare boom")))
    with pytest.raises(RuntimeError, match="prepare boom"):
        wv._PendingRecordingEnv().result()


def test_pending_env_after_run_is_noop_when_restart_disabled(monkeypatch):
    """VM_WECHAT_RESTART=0 时微信死活不归这条链路管，别替用户开微信。"""
    monkeypatch.setenv(wv.RESTART_MODE_ENV, "0")
    started = []
    monkeypatch.setattr(wv, "_prepare_recording_env", lambda: {})     # 后台线程秒回
    monkeypatch.setattr(wp, "start_wechat", lambda e: started.append(e))
    monkeypatch.setattr(wp, "resolve_wechat_exe", lambda: Path("C:/fake/Weixin.exe"))
    t = wv._PendingRecordingEnv()
    t.result()
    t._after_run()
    assert started == []


def test_pending_env_after_run_relaunches_if_wechat_gone(monkeypatch):
    monkeypatch.setenv(wv.RESTART_MODE_ENV, "auto")
    started = []
    monkeypatch.setattr(wv, "_prepare_recording_env", lambda: {})     # 后台线程秒回
    monkeypatch.setattr(wp, "resolve_wechat_exe", lambda: Path("C:/fake/Weixin.exe"))
    monkeypatch.setattr(wp, "start_wechat", lambda e: started.append(e) or 1)
    t = wv._PendingRecordingEnv()
    t._after_run()
    assert started == [Path("C:/fake/Weixin.exe")]


# ---------------- _do_send / send_text 集成 ----------------

class _FakeProc:
    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def _stub_send_io(monkeypatch, tmp_path):
    monkeypatch.setattr(wv, "_uia_ready", lambda: False)
    monkeypatch.setattr(wv, "_foreground_wechat", lambda: None)
    monkeypatch.setattr(wv, "_trigger_record", lambda *a, **k: None)
    monkeypatch.setattr(wv, "_finish_record", lambda: True)
    monkeypatch.setattr(wv, "_start_play", lambda w: _FakeProc())
    monkeypatch.setattr(wv, "_wait_play_start", lambda p, t=40.0: True)
    monkeypatch.setattr(wv, "_wait_play_done", lambda p, d: None)
    monkeypatch.setattr(wv, "_wav_duration", lambda p: 1.0)
    monkeypatch.setattr(wv, "_safe_restore", lambda: (True, ""))
    import config as cfg
    monkeypatch.setattr(cfg, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(wv, "HISTORY_FILE", tmp_path / "wechat_send_history.json")
    (tmp_path / "tts_x.wav").write_bytes(b"RIFF")


def test_do_send_reports_restart_in_steps(tmp_path, monkeypatch):
    """预热任务返回 recording_env 时，steps 要说清重启了微信（否则用户不知道刚才卡了什么）。"""
    _stub_send_io(monkeypatch, tmp_path)
    monkeypatch.setattr(wv, "_prepare_recording_env", lambda: {
        "kind": "recording_env", "restart": True, "summary": "已重启微信（测试）"})

    class T:
        def result(self):
            return {"kind": "recording_env", "restart": True, "summary": "已重启微信（测试）"}
        def abandon(self):
            pass

    res = wv._do_send(wv.SendVoiceReq(wav="tts_x.wav"), pre_apply=T())
    assert res["outcome"] == "ok"
    assert any("已重启微信" in s for s in res["steps"])
    assert any("与 TTS 并行" in s for s in res["steps"])


def test_do_send_without_pre_apply_prepares_env_inline(tmp_path, monkeypatch):
    _stub_send_io(monkeypatch, tmp_path)
    monkeypatch.setattr(wv, "_need_wechat_restart", lambda: (False, "无需重启"))
    res = wv._do_send(wv.SendVoiceReq(wav="tts_x.wav"))
    assert res["outcome"] == "ok"
    assert any("麦克风已切到 CABLE Output" in s for s in res["steps"])


# ---------------- /api/wechat/precheck ----------------

def test_precheck_is_readonly_and_explains(monkeypatch):
    monkeypatch.setenv(wv.RESTART_MODE_ENV, "auto")
    monkeypatch.setattr(wp, "list_wechat_processes",
                        lambda: [{"pid": 1, "name": "Weixin.exe", "exe": "C:/wx/Weixin.exe"}])
    monkeypatch.setattr(wp, "input_device_probe",
                        lambda: {"device": "麦克风阵列 (Senary Audio)", "file": "f", "hits": 1})
    monkeypatch.setattr(wp, "enum_wechat_windows", lambda: [])
    out = wv.precheck()
    assert out["ok"] is True
    assert out["restart_needed"] is True
    assert out["last_input_device"] == "麦克风阵列 (Senary Audio)"
    assert "重启" in out["hint"]
