"""radar_api 单测：HTTP 壳的序列化与降级，不碰 GDI / 微信。

chat_radar 包本身已有 33 个纯逻辑测试；这里只测薄壳的三件事——
① Tick → JSON 的形状；② 观察失败（微信没开）必须是 200 + ok:false 而不是 500；
③ 单例复用与 reset 的转发放大。全部用桩 Radar，不打真窗口。
"""

import sys
import threading
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
        self.reset_calls = 0

    def tick(self):
        self.tick_calls += 1
        return self._tick_result

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
