# -*- coding: utf-8 -*-
"""A7/A8 实时输入设备选择器 + 降噪开关测试。

覆盖两层：
  1. live_settings 纯函数：默认值/roundtrip/损坏文件回退/原子写
  2. rvc_live 新端点函数：设备校验（400 找不到设备）、成功写入、needs_restart
     （直接调用 FastAPI 端点函数，不起 app；mock 设备枚举与进程探测）
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import live_settings  # noqa: E402
import rvc_live  # noqa: E402


@pytest.fixture(autouse=True)
def _tmp_settings(tmp_path, monkeypatch):
    """每个用例独立设置文件，避免污染真实 outputs/。"""
    monkeypatch.setattr(live_settings, "SETTINGS_PATH",
                        tmp_path / "live_settings.json")
    # rvc_live 持有的是模块引用，两个模块看到的是同一个 module 对象，
    # patch 模块属性即可（上面的 setattr 已经做到）。这里仅兜底确认。
    assert rvc_live.live_settings is live_settings
    yield


def test_defaults_when_missing():
    s = live_settings.get()
    assert s == {"input_device": "", "denoise": True}


def test_roundtrip_update():
    live_settings.update(input_device="USB 麦克风", denoise=False)
    s = live_settings.get()
    assert s["input_device"] == "USB 麦克风"
    assert s["denoise"] is False
    # 部分更新：只改一个字段，另一个保持
    live_settings.update(denoise=True)
    s = live_settings.get()
    assert s["input_device"] == "USB 麦克风"
    assert s["denoise"] is True


def test_empty_input_device_means_follow_default():
    live_settings.update(input_device="  ")
    assert live_settings.get()["input_device"] == ""


def test_corrupt_file_falls_back(tmp_path, monkeypatch):
    p = tmp_path / "live_settings.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(live_settings, "SETTINGS_PATH", p)
    assert live_settings.get() == {"input_device": "", "denoise": True}
    # 保存覆盖坏文件后恢复正常
    live_settings.update(denoise=False)
    assert live_settings.get()["denoise"] is False


def test_get_returns_copy():
    s1 = live_settings.get()
    s1["denoise"] = False  # 改副本不影响存储
    assert live_settings.get()["denoise"] is True


# ---------------- 端点函数 ----------------

_DEVICES = [{"name": "麦克风阵列 (Senary Audio)", "is_default": True},
            {"name": "CABLE Output (VB-Audio Virtual C", "is_default": False}]


def test_get_endpoint_shape(monkeypatch):
    monkeypatch.setattr(rvc_live, "_mme_input_devices", lambda: _DEVICES)
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: False)
    r = rvc_live.rvc_live_audio_devices()
    assert r["ok"] is True
    assert r["items"] == _DEVICES
    assert r["explicit"] == ""
    assert r["denoise"] is True
    assert r["running"] is False


def test_post_unknown_device_400(monkeypatch):
    monkeypatch.setattr(rvc_live, "_mme_input_devices", lambda: _DEVICES)
    from rvc_live import LiveDevicesPayload
    with pytest.raises(Exception) as ei:
        rvc_live.rvc_live_audio_devices_set(LiveDevicesPayload(input_device="不存在的麦"))
    assert "找不到输入设备" in str(getattr(ei.value, "detail", ei.value))


def test_post_save_device_and_denoise(monkeypatch):
    monkeypatch.setattr(rvc_live, "_mme_input_devices", lambda: _DEVICES)
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: False)
    r = rvc_live.rvc_live_audio_devices_set(
        rvc_live.LiveDevicesPayload(input_device="cable output", denoise=False))
    # 关键词大小写不敏感模糊匹配（MME 截断名也能对上）
    assert r["ok"] is True
    assert r["input_device"] == "cable output"
    assert r["denoise"] is False
    assert r["needs_restart"] is False
    # 真实落盘
    assert live_settings.get()["input_device"] == "cable output"


def test_post_running_needs_restart(monkeypatch):
    monkeypatch.setattr(rvc_live, "_mme_input_devices", lambda: _DEVICES)
    monkeypatch.setattr(rvc_live, "_live_proc_alive", lambda: True)
    r = rvc_live.rvc_live_audio_devices_set(
        rvc_live.LiveDevicesPayload(input_device="麦克风阵列"))
    assert r["running"] is True and r["needs_restart"] is True


def test_resolve_prefers_explicit_device(monkeypatch):
    """显式选择优先于系统默认：候选列表第一项必须是 live_settings 的值。"""
    live_settings.update(input_device="CABLE Output")
    seen = []

    def fake_default():
        seen.append("default")
        return "麦克风阵列 (Senary Audio)"

    monkeypatch.setattr(rvc_live, "_system_default_input", fake_default)

    # 捕获 candidates 顺序：模拟枚举结果命中显式选择（api 字段必须为 MME，
    # 与真实枚举一致——函数按 hostapi==MME 过滤）
    mme = [{"name": "麦克风阵列 (Senary Audio)", "api": "MME", "in": 2, "out": 0},
           {"name": "CABLE Output (VB-Audio Virtual C", "api": "MME", "in": 2, "out": 0},
           {"name": "CABLE Input (VB-Audio Virtual C", "api": "MME", "in": 0, "out": 2}]
    monkeypatch.setattr(rvc_live, "VENV_PY", Path("python"), raising=False)
    import subprocess as _sp

    class _R:
        stdout = __import__("json").dumps(mme) + "\n"
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _R())
    inp, _out = rvc_live._resolve_device_names()
    assert inp == "CABLE Output (VB-Audio Virtual C"
