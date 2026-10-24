"""`radar_api.peek_side()`：只读「最后一条是谁发的」，不喂状态机。

托管敢不敢回，全押在这个值上 —— 而 UIA 拿不到发送方，所以这里必须有回归测试：
unknown 时必须返回 ok=false（上层据此**不发**），不能猜 left。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import radar_api  # noqa: E402


@pytest.fixture(autouse=True)
def clean_singleton():
    radar_api.reset_singleton()
    yield
    radar_api.reset_singleton()


def _fake(monkeypatch, *, err="", blocks=None):
    radar = SimpleNamespace(peek=lambda: (SimpleNamespace(), SimpleNamespace(blocks=blocks or [], last_chat=(blocks or [None])[-1]), err))
    monkeypatch.setattr(radar_api, "_radar", lambda: radar)


def test_peek_side_returns_left(monkeypatch):
    _fake(monkeypatch, blocks=[SimpleNamespace(side="left", kind="text")])
    out = radar_api.peek_side()
    assert out["ok"] and out["side"] == "left" and out["kind"] == "text" and out["n_blocks"] == 1


def test_peek_side_returns_right(monkeypatch):
    _fake(monkeypatch, blocks=[SimpleNamespace(side="right", kind="text")])
    assert radar_api.peek_side()["side"] == "right"


def test_peek_side_reports_error(monkeypatch):
    _fake(monkeypatch, err="微信最小化")
    out = radar_api.peek_side()
    assert out["ok"] is False and "最小化" in out["error"]


def test_peek_side_no_blocks(monkeypatch):
    _fake(monkeypatch, blocks=[])
    out = radar_api.peek_side()
    assert out["ok"] is False and out["side"] == ""
