"""A1 发送链路自检单测（mock 掉 PowerShell，纯函数 + 端点行为两层）。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

pytest.importorskip("fastapi")

import audio_api as mod  # noqa: E402


def _diag(devices):
    return {"ok": True, "devices": devices}


# 常见设备清单：扬声器 + 麦克风 + VB-CABLE 两端
GOOD = _diag(
    [
        {"flow": 0, "name": "扬声器 (Realtek Audio)", "state": 1, "roles": [0, 1, 2]},
        {"flow": 0, "name": "CABLE Input (VB-Audio Virtual Cable)", "state": 1, "roles": []},
        {"flow": 1, "name": "麦克风 (Realtek Audio)", "state": 1, "roles": []},
        {
            "flow": 1,
            "name": "CABLE Output (VB-Audio Virtual Cable)",
            "state": 1,
            "roles": [0, 1, 2],
        },
    ]
)


def by_key(report, key):
    return next(i for i in report["items"] if i["key"] == key)


def test_all_good():
    r = mod.build_send_chain_report(GOOD, stale=False)
    assert r["ok"] is True and r["all_ok"] is True and r["stale"] is False
    assert by_key(r, "cable")["ok"] is True
    assert by_key(r, "default_capture")["ok"] is True
    assert by_key(r, "default_render")["ok"] is True
    assert len(r["items"]) == 3


def test_cable_missing():
    r = mod.build_send_chain_report(
        _diag(
            [
                {"flow": 0, "name": "扬声器 (Realtek)", "state": 1, "roles": [0]},
                {"flow": 1, "name": "麦克风 (Realtek)", "state": 1, "roles": [0]},
            ]
        ),
        stale=False,
    )
    cable = by_key(r, "cable")
    assert cable["ok"] is False and "CABLE Input" in cable["detail"]
    assert "VB-Audio" in cable["hint"]
    assert r["all_ok"] is False


def test_default_capture_is_real_mic():
    devices = [
        {"flow": 0, "name": "扬声器 (Realtek)", "state": 1, "roles": [0]},
        {"flow": 0, "name": "CABLE Input (VB-Audio)", "state": 1, "roles": []},
        {"flow": 1, "name": "麦克风 (Realtek)", "state": 1, "roles": [0]},
        {"flow": 1, "name": "CABLE Output (VB-Audio)", "state": 1, "roles": []},
    ]
    r = mod.build_send_chain_report(_diag(devices), stale=False)
    cap = by_key(r, "default_capture")
    assert cap["ok"] is False
    assert "CABLE Output" in cap["hint"]  # 修复指引必须提到目标设备名
    assert by_key(r, "default_render")["ok"] is True


def test_default_render_is_cable_warns():
    devices = [
        {"flow": 0, "name": "CABLE Input (VB-Audio)", "state": 1, "roles": [0]},
        {"flow": 0, "name": "扬声器 (Realtek)", "state": 1, "roles": []},
        {"flow": 1, "name": "CABLE Output (VB-Audio)", "state": 1, "roles": [0]},
        {"flow": 1, "name": "麦克风 (Realtek)", "state": 1, "roles": []},
    ]
    r = mod.build_send_chain_report(_diag(devices), stale=False)
    ren = by_key(r, "default_render")
    assert ren["ok"] is False and ren.get("warn") is True
    assert by_key(r, "default_capture")["ok"] is True


def test_cable_render_localized_names_count():
    """中文 Windows 的本地化名「扬声器 (VB-Audio Virtual Cable)」必须认成 CABLE 播放端。

    2026-10-15 实机回归：播放端被本地化后，旧判据（只认 "CABLE Input"）把
    装好的 CABLE 误报成「未检测到播放端」——判据必须跟上下游播放链路同源的
    驱动名双候选（见 test_cable_keyword_consistency.py 的模块说明）。
    """
    devices = [
        {"flow": 0, "name": "扬声器 (VB-Audio Virtual Cable)", "state": 1, "roles": []},
        {"flow": 0, "name": "CABLE In 16 Ch (VB-Audio Virtual Cable)", "state": 1, "roles": []},
        {"flow": 1, "name": "CABLE Output (VB-Audio Virtual Cable)", "state": 1, "roles": [0, 1, 2]},
    ]
    r = mod.build_send_chain_report(_diag(devices), stale=False)
    assert by_key(r, "cable")["ok"] is True


def test_cable_render_16ch_variant_alone_does_not_count():
    """反面守卫：只有 16 通道变体时不算就绪。

    它的 MME 枚举名会被截断成 `CABLE In 16 Ch (VB-Audio Virtua`，播放链路的
    双候选关键词谁都命不中它（同 test_cable_keyword_consistency 的反面守卫）。
    """
    devices = [
        {"flow": 0, "name": "CABLE In 16 Ch (VB-Audio Virtual Cable)", "state": 1, "roles": []},
        {"flow": 1, "name": "CABLE Output (VB-Audio Virtual Cable)", "state": 1, "roles": [0, 1, 2]},
    ]
    r = mod.build_send_chain_report(_diag(devices), stale=False)
    cable = by_key(r, "cable")
    assert cable["ok"] is False and "CABLE Input" in cable["detail"]


def test_default_render_localized_cable_warns():
    """默认播放是本地化名（扬声器 (VB-Audio Virtual Cable)）时，也要提示「本机听不到声音」。"""
    devices = [
        {"flow": 0, "name": "扬声器 (VB-Audio Virtual Cable)", "state": 1, "roles": [0, 1]},
        {"flow": 0, "name": "扬声器 (Senary Audio)", "state": 1, "roles": [2]},
        {"flow": 1, "name": "CABLE Output (VB-Audio Virtual Cable)", "state": 1, "roles": [0, 1, 2]},
    ]
    r = mod.build_send_chain_report(_diag(devices), stale=False)
    ren = by_key(r, "default_render")
    assert ren["ok"] is False and ren.get("warn") is True
    assert by_key(r, "cable")["ok"] is True


def test_stale_backup_appends_warn_item():
    r = mod.build_send_chain_report(GOOD, stale=True)
    item = by_key(r, "stale_backup")
    assert item.get("warn") is True and item["ok"] is False
    assert r["stale"] is True
    assert r["all_ok"] is False


def test_diag_failure_single_item():
    r = mod.build_send_chain_report({"ok": False, "error": "脚本无输出"}, stale=False)
    assert r["ok"] is False and r["all_ok"] is False
    assert len(r["items"]) == 1 and r["items"][0]["key"] == "diag"
    assert "脚本无输出" in r["items"][0]["detail"]


def test_endpoint_uses_diag_and_stale(monkeypatch):
    """端点层：确认调用 diag 动作并把 _audio_stale 结果透传。"""
    calls = []
    monkeypatch.setattr(mod, "_run_audio_config", lambda a: calls.append(a) or GOOD)
    monkeypatch.setattr(mod, "_audio_stale", lambda: True)
    r = mod.audio_send_chain()
    assert calls == ["diag"]
    assert r["stale"] is True and r["ok"] is True
