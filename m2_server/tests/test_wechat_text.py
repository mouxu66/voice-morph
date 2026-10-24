"""微信文本发送的单元测试（假控件，不碰真微信）。

钉住三件事：
① 判据是**发送按钮真的可用**（SetValue 不抛异常不算成功）；
② 用户输入框里的草稿绝不覆盖；
③ 失败时把输入框清干净，不留半截文字。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wechat_text as wt  # noqa: E402


class _ValuePattern:
    def __init__(self, ctrl: "_FakeInput"):
        self._ctrl = ctrl

    @property
    def Value(self) -> str:
        return self._ctrl.value

    def SetValue(self, v: str) -> None:
        if self._ctrl.accept_setvalue:
            self._ctrl.value = v


class _SendButton:
    def __init__(self, inp: "_FakeInput"):
        self._inp = inp
        self.invoked = 0

    @property
    def IsEnabled(self) -> bool:
        return bool(self._inp.value.strip())

    def GetInvokePattern(self):
        return self

    def Invoke(self) -> None:
        self.invoked += 1
        self._inp.value = ""


class _FakeInput:
    """`accept_setvalue=False` 模拟「Qt 没吃下 ValuePattern.SetValue」。"""

    def __init__(self, name: str = "117", value: str = "", accept_setvalue: bool = True):
        self.Name = name
        self.value = value
        self.accept_setvalue = accept_setvalue
        self.focused = False

    def GetValuePattern(self):
        return _ValuePattern(self)

    def SetFocus(self) -> None:
        self.focused = True


@pytest.fixture()
def fake(monkeypatch):
    inp = _FakeInput()
    btn = _SendButton(inp)
    monkeypatch.setattr(wt, "_input_ctrl", lambda: inp)
    monkeypatch.setattr(wt, "_send_button", lambda: btn)
    return inp, btn


def test_input_state_reads_chat_name_and_button(fake):
    inp, _btn = fake
    st = wt.input_state()
    assert st["ok"] and st["chat"] == "117" and st["can_send"] is False
    inp.value = "在"
    assert wt.input_state()["can_send"] is True


def test_send_text_success_path_is_background(fake):
    inp, btn = fake
    res = wt.send_text("在的，你说", expect_chat="117")
    assert res["ok"] and res["method"] == "value"
    assert btn.invoked == 1
    assert inp.value == ""  # 发送后清空
    assert inp.focused is False  # 后台路径不该碰焦点


def test_send_text_refuses_to_overwrite_draft(fake):
    inp, _btn = fake
    inp.value = "我还没写完"
    res = wt.send_text("在的", expect_chat="117")
    assert not res["ok"] and "草稿" in res["error"]
    assert inp.value == "我还没写完"


def test_send_text_checks_target_chat(fake):
    inp, _btn = fake
    inp.Name = "妈妈"
    res = wt.send_text("在的", expect_chat="117")
    assert not res["ok"] and "会话不对" in res["error"]
    assert inp.value == ""


def test_send_text_rejects_empty_and_multiline():
    assert not wt.send_text("   ")["ok"]
    assert not wt.send_text("第一行\n第二行")["ok"]


def test_send_text_falls_back_to_typing(monkeypatch):
    inp = _FakeInput(accept_setvalue=False)
    btn = _SendButton(inp)
    monkeypatch.setattr(wt, "_input_ctrl", lambda: inp)
    monkeypatch.setattr(wt, "_send_button", lambda: btn)
    monkeypatch.setattr(wt, "_type_unicode", lambda text: setattr(inp, "value", text))
    monkeypatch.setitem(sys.modules, "wechat_voice", types.SimpleNamespace(_foreground_wechat=lambda: 0))

    res = wt.send_text("在的，你说", expect_chat="117")
    assert res["ok"] and res["method"] == "typing"
    assert inp.focused is True
    assert btn.invoked == 1


def test_send_text_clears_when_button_never_enables(monkeypatch):
    inp = _FakeInput(accept_setvalue=False)
    btn = _SendButton(inp)
    monkeypatch.setattr(wt, "_input_ctrl", lambda: inp)
    monkeypatch.setattr(wt, "_send_button", lambda: btn)
    monkeypatch.setattr(wt, "_type_unicode", lambda text: None)
    monkeypatch.setitem(sys.modules, "wechat_voice", types.SimpleNamespace(_foreground_wechat=lambda: 0))

    res = wt.send_text("在的", expect_chat="117")
    assert not res["ok"]
    assert btn.invoked == 0
    assert inp.value == ""


def test_select_session_verifies_by_input_name(monkeypatch):
    state = {"chat": "orig"}

    class _Item:
        def __init__(self, title):
            self.title = title
            self.selected = 0

        def GetSelectionItemPattern(self):
            return self

        def Select(self):
            self.selected += 1
            state["chat"] = self.title

    items = [("117", _Item("117")), ("妈妈", _Item("妈妈"))]
    monkeypatch.setattr(wt, "_session_items", lambda: items)
    monkeypatch.setattr(wt, "input_state", lambda: {"ok": True, "chat": state["chat"], "value": ""})

    res = wt.select_session("117")
    assert res["ok"] and res["chat"] == "117"
    assert items[0][1].selected == 1


def test_select_session_reports_missing_chat(monkeypatch):
    monkeypatch.setattr(wt, "_session_items", lambda: [("妈妈", object())])
    res = wt.select_session("117")
    assert not res["ok"] and "没有「117」" in res["error"]


def test_scroll_zero_is_noop():
    assert wt.scroll_messages(0)["ok"] is True
