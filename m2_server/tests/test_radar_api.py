"""radar_api 单测：HTTP 壳的序列化与降级，不碰 GDI / 微信。

chat_radar 包本身已有 33 个纯逻辑测试；这里只测薄壳的四件事——
① Tick → JSON 的形状；② 观察失败（微信没开）必须是 200 + ok:false 而不是 500；
③ 单例复用与 reset 的转发放大；④ `/names` 的串行锁与降级。全部用桩，不打真窗口。
"""

import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import chat_read  # noqa: E402

import radar_api  # noqa: E402


def _fake_verdict(action: str = "soon", score: int = 3, reason: str = "对方最后说话 42.0秒"):
    return types.SimpleNamespace(
        as_dict=lambda: {
            "action": action,
            "label": {"soon": "可以回了"}.get(action, action),
            "score": score,
            "reason": reason,
            "source": "rule",
        }
    )


def _fake_signals(n_blocks: int = 5, **overrides):
    base = {
        "last_side": "left",
        "last_kind": "text",
        "streak": 2,
        "gap_seconds": 42.0,
        "gap_lower_bound": False,
        "my_last_reply_age": 120.0,
        "tail_meta": "",
        "changed": False,
        "stable_frames": 7,
        "n_blocks": n_blocks,
        "frames_seen": 30,
    }
    base.update(overrides)
    return types.SimpleNamespace(as_dict=lambda: dict(base), elapsed_ms=30.0)


def _ok_tick():
    window = types.SimpleNamespace(
        title="微信", rect=(100, 200, 1600, 900), hwnd=12345, cls="Qt51514QWindowIcon"
    )
    roi = types.SimpleNamespace(left=343, top=62, right=1560, bottom=1300)
    blocks = [types.SimpleNamespace(side="left", kind="text")] * 5
    return types.SimpleNamespace(
        signals=_fake_signals(),
        verdict=_fake_verdict(),
        frame=types.SimpleNamespace(window=window, image=None, roi=roi),
        layout=types.SimpleNamespace(blocks=blocks),
        error="",
        ok=True,
    )


def _err_tick(error: str = "没找到微信窗口（微信是否在运行？）"):
    return types.SimpleNamespace(
        signals=_fake_signals(n_blocks=0),
        verdict=_fake_verdict(action="unknown", score=0, reason=error),
        frame=None,
        layout=None,
        error=error,
        ok=False,
    )


class _StubRadar:
    def __init__(self, tick_result):
        self._tick_result = tick_result
        self.tick_calls = 0
        self.peek_calls = 0
        self.reset_calls = 0

    def tick(self):
        self.tick_calls += 1
        return self._tick_result

    def peek(self):
        """诊断路径：只抓图 + 解析，不喂状态机（与真 Radar.peek 同契约）。"""
        self.peek_calls += 1
        t = self._tick_result
        if not t.ok:
            return None, None, t.error
        return t.frame, t.layout, ""

    def reset(self):
        self.reset_calls += 1


def _install(stub):
    radar_api._RADAR = stub  # 直接注入，绕过懒加载（否则会真 import chat_radar.Radar）


def teardown_function():
    radar_api.reset_singleton()


def test_state_serializes_verdict_and_signals():
    stub = _StubRadar(_ok_tick())
    _install(stub)
    out = radar_api.state()
    assert out["ok"] is True
    assert "error" not in out
    assert out["verdict"]["action"] == "soon"
    assert out["verdict"]["label"] == "可以回了"
    assert out["signals"]["last_side"] == "left"
    assert out["signals"]["gap_seconds"] == 42.0
    assert out["poll_hint_ms"] == radar_api.POLL_HINT_MS
    assert isinstance(out["elapsed_ms"], float)
    assert stub.tick_calls == 1


def test_observation_failure_is_200_with_error_not_500():
    """微信没开 / 最小化是正常状态：必须 200 + ok:false + verdict=unknown。"""
    stub = _StubRadar(_err_tick())
    _install(stub)
    out = radar_api.state()
    assert out["ok"] is False
    assert "没找到微信窗口" in out["error"]
    assert out["verdict"]["action"] == "unknown"
    # 信号仍然给出来：前端可以显示「观察中」而不是整块消失
    assert out["signals"]["n_blocks"] == 0


def test_probe_serializes_window_roi_and_blocks():
    stub = _StubRadar(_ok_tick())
    _install(stub)
    out = radar_api.probe()
    assert out["ok"] is True
    assert out["window"]["title"] == "微信"
    assert out["window"]["rect"] == [100, 200, 1600, 900]
    assert out["roi"] == [343, 62, 1560, 1300]
    assert out["n_blocks"] == 5


def test_probe_does_not_advance_observation_state():
    """诊断不能改动被诊断的东西：/probe 必须走 peek，不能走 tick。

    走 tick 的代价很实：会推进 frames_seen、覆盖上一帧指纹、并可能重置
    _stable 与 _observed_since（冷启动的 gap_lower_bound 靠后者），
    表现就是「一调 probe，gap 从 0 重算」。
    """
    stub = _StubRadar(_ok_tick())
    _install(stub)
    radar_api.probe()
    assert stub.tick_calls == 0, "probe 不该喂状态机（tick 会推进观察状态）"
    assert stub.peek_calls == 1


def test_probe_failure_reports_error_without_window():
    stub = _StubRadar(_err_tick("聊天区 ROI 探测失败"))
    _install(stub)
    out = radar_api.probe()
    assert out["ok"] is False
    assert out["error"] == "聊天区 ROI 探测失败"
    assert "window" not in out


def test_reset_forwards_to_radar():
    stub = _StubRadar(_ok_tick())
    _install(stub)
    out = radar_api.reset()
    assert out == {"ok": True}
    assert stub.reset_calls == 1


def test_singleton_is_created_once_and_reset_discards_it():
    """懒加载单例：两次 _radar() 同一个对象；reset_singleton 后换新。"""
    radar_api.reset_singleton()
    first = radar_api._radar()
    second = radar_api._radar()
    assert first is second
    radar_api.reset_singleton()
    assert radar_api._radar() is not first


def test_state_serializes_under_concurrent_callers():
    """两个线程同时 /state：RLock 只保证串行，不该死锁也不该崩。"""
    stub = _StubRadar(_ok_tick())
    _install(stub)
    results: list = []

    def call():
        results.append(radar_api.state())

    threads = [threading.Thread(target=call) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    assert stub.tick_calls == 2


# ---------------------------------------------------------------- /names（认人）
# 这一路**不是只读**：会拉起视觉模型（实测 2.7s / 6G 显存）。所以壳的责任只有三条 ——
# 转发、不 500、别让两次点击同时跑模型。三条都用桩验，绝不真读微信。


def test_names_forwards_read_names_result(monkeypatch):
    payload = {
        "ok": True,
        "chat": "117",
        "learned": 2,
        "people": [{"id": 1, "name": "红姜、", "n": 3}, {"id": 2, "name": "", "n": 1}],
    }
    monkeypatch.setattr(chat_read, "read_names", lambda timeout=90.0: dict(payload))
    assert radar_api.names() == payload


def test_names_failure_is_200_with_reason_not_500(monkeypatch):
    """认不出名字是常态（单聊没昵称条 / 视觉权重没装 / 群友全发表情）：
    不能把一个锦上添花的能力打成 500，面板会整块变成错误页。"""

    def boom(timeout=90.0):
        raise RuntimeError("抓不到微信窗口")

    monkeypatch.setattr(chat_read, "read_names", boom)
    out = radar_api.names()
    assert out["ok"] is False
    assert "抓不到微信窗口" in out["error"]


def test_names_clamps_timeout(monkeypatch):
    """调用方传 10000 会一直攥着锁，后面每次点击都排队 —— 上限钉死。"""
    seen = {}

    def spy(timeout=90.0):
        seen["timeout"] = timeout
        return {"ok": True, "learned": 0}

    monkeypatch.setattr(chat_read, "read_names", spy)
    radar_api.names(timeout=10000)
    assert seen["timeout"] == 180.0
    radar_api.names(timeout=0.1)
    assert seen["timeout"] == 5.0


def test_names_refuses_a_second_call_while_the_model_is_running(monkeypatch):
    """双击按钮不该跑两遍模型（一次 2.7s + 6G 显存）：第二次直接 busy，不排队。"""
    started = threading.Event()
    release = threading.Event()
    calls: list = []

    def slow(timeout=90.0):
        calls.append(1)
        started.set()
        release.wait(timeout=10)
        return {"ok": True, "learned": 1}

    monkeypatch.setattr(chat_read, "read_names", slow)
    t = threading.Thread(target=radar_api.names, daemon=True)
    t.start()
    assert started.wait(timeout=5), "第一次调用没有跑起来"
    try:
        out = radar_api.names()
        assert out["ok"] is False and out.get("busy") is True
    finally:
        release.set()
        t.join(timeout=10)
    assert calls == [1], "第二次点击不该再跑一次模型"


def test_names_releases_the_lock_after_a_failure(monkeypatch):
    """异常路径必须放锁：否则一次失败就把「认人」永久锁死，用户只能重启应用。"""

    def boom(timeout=90.0):
        raise RuntimeError("模型崩了")

    monkeypatch.setattr(chat_read, "read_names", boom)
    assert radar_api.names()["ok"] is False
    monkeypatch.setattr(chat_read, "read_names", lambda timeout=90.0: {"ok": True, "learned": 0})
    assert radar_api.names()["ok"] is True
