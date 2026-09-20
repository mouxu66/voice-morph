"""插件开关（第 6 步）：预设、启用集、关闭守卫、以及"关掉就真的不挂载"。

第 2~5 步都在建**声明** —— 清单写了什么但没有一样东西会因此改变行为。
第 6 步把声明接成行为，于是冒出三类新的失效模式，本文件逐个钉住：

1. **关掉 A 把 B 弄砖**：A 被 B `requires`。守卫必须在**写**之前拦（409），
   而读侧（`enabled_ids`）还得处理「先关 A、后开 B」这种历史状态 —— 两处都要有。
2. **关了等于没关**：前端不显示了，后端照样 import、照样起 warmup 加载 4.9G 模型。
   省了个入口没省资源，比不做更糟（用户以为省了）。
3. **"你关的"被报成"坏掉的"**：关掉的插件没被挂载，`state_of` 若照旧报
   「未注册」，界面上就会把用户主动关的能力列进异常清单 —— 与三态分开是同一个道理。

测试隔离：每个用例都拿到独立的 `STATE_FILE`（`tmp_path`），且 `reset_skipped()`
清空跳过记账 —— 否则"某个用例写了禁用集"会污染后面的挂载断言。

变异验证（证明这些断言不是恒真的）见文件末尾两条 `*_goes_red_*` 用例。
"""

from __future__ import annotations

import plugin_loader
import plugin_manifest
import pytest
import server
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_manifest, "STATE_FILE", tmp_path / "plugins.json")
    plugin_manifest.reset_skipped()
    yield
    plugin_manifest.reset_skipped()


def _p(pid: str) -> plugin_manifest.Plugin:
    return plugin_manifest.by_id()[pid]


def _ids() -> set[str]:
    return set(plugin_manifest.by_id())


# ---------------------------------------------------------------- 1. 启用集


def test_enabled_ids_is_all_when_nothing_is_disabled():
    assert plugin_manifest.enabled_ids() == _ids()


def test_disabled_but_depended_on_is_kept():
    """关掉的 A 若仍被启用中的 B 依赖 → 必须保留。

    场景：用户先关 `sound.offline-vc`（当时没人依赖它），后来开了 `sound.audition`。
    这条守的是读侧的兜底；写侧的拦截（不许关）在下面 `test_disable_is_blocked_*`。
    """
    plugin_manifest.write_disabled({"sound.offline-vc"})
    on = plugin_manifest.enabled_ids()
    assert "sound.offline-vc" in on, "被 sound.audition 依赖，关掉会让试音间变砖"
    assert "sound.audition" in on


def test_enabled_ids_drops_when_nobody_needs_it():
    """反过来：关掉一个**没人依赖**的，就该真的生效 —— 否则上一条的"保留"会吞掉一切。"""
    plugin_manifest.write_disabled({"sound.audiobook", "sound.tts"})
    on = plugin_manifest.enabled_ids()
    assert "sound.tts" not in on
    assert "sound.audiobook" not in on


def test_dependents_of_names_the_dependents():
    assert plugin_manifest.dependents_of("sound.tts") == ["sound.audiobook"]
    assert plugin_manifest.dependents_of("sound.offline-vc") == ["sound.audition"]
    assert sorted(plugin_manifest.dependents_of("sound.workshop")) == ["sound.ft", "sound.mine"]
    assert plugin_manifest.dependents_of("pet.companion") == ["pet.market"]


def test_dependents_of_ignores_disabled_dependents():
    """依赖者自己也被关了 → 不算依赖者（否则就关不掉任何东西了）。"""
    plugin_manifest.write_disabled({"sound.audiobook"})
    assert plugin_manifest.dependents_of("sound.tts", {"sound.audiobook"}) == []


# ---------------------------------------------------------------- 2. 预设


def test_presets_are_nested():
    light = plugin_manifest.preset_ids("light")
    standard = plugin_manifest.preset_ids("standard")
    full = plugin_manifest.preset_ids("full")
    assert light < standard < full


def test_every_preset_contains_all_core():
    core = {p.id for p in plugin_manifest.load_all() if p.is_core}
    for name in plugin_manifest.PRESETS:
        assert core <= plugin_manifest.preset_ids(name), f"{name} 缺核心能力"


def test_presets_are_closed_under_requires():
    """预设里点名的能力，它依赖的也必须进来 —— 否则装出来就是 broken。"""
    plugins = plugin_manifest.by_id()
    for name in plugin_manifest.PRESETS:
        on = plugin_manifest.preset_ids(name)
        for pid in on:
            assert set(plugins[pid].requires) <= on, f"{name}: {pid} 的依赖没被一起启用"


def test_unknown_preset_raises():
    with pytest.raises(KeyError, match="未知预设"):
        plugin_manifest.preset_ids("standrad")


def test_apply_preset_records_only_what_the_user_turned_off():
    """写进文件的是"用户想关的"，不是"最终启用的"。

    被依赖而保留的能力不该被记成"用户主动开的" —— 否则切走预设再切回来，
    会凭空多出几个开启项，用户根本不知道自己"开"过它们。
    """
    rep = plugin_manifest.apply_preset("light")
    keep = plugin_manifest.preset_ids("light")
    assert set(rep["disabled"]) == _ids() - keep
    # 保留逻辑只留在读侧
    assert "sound.offline-vc" in rep["enabled"]


# ---------------------------------------------------------------- 3. 关闭守卫


def test_core_cannot_be_disabled():
    rep = plugin_manifest.set_enabled("core.voices", False)
    assert rep["reason"] == "core"
    assert rep["enabled"] is True
    assert plugin_manifest.disabled_ids() == set(), "核心被拦下时不该留下半截状态"


def test_disable_is_blocked_while_dependents_are_enabled():
    rep = plugin_manifest.set_enabled("sound.tts", False)
    assert rep["blocked_by"] == ["sound.audiobook"]
    assert rep["enabled"] is True
    assert plugin_manifest.disabled_ids() == set()


def test_disable_succeeds_once_the_dependent_is_off():
    plugin_manifest.set_enabled("sound.audiobook", False)
    rep = plugin_manifest.set_enabled("sound.tts", False)
    assert rep["enabled"] is False
    assert plugin_manifest.disabled_ids() == {"sound.tts", "sound.audiobook"}


def test_enable_clears_the_flag():
    plugin_manifest.set_enabled("sound.audiobook", False)
    plugin_manifest.set_enabled("sound.tts", False)
    plugin_manifest.set_enabled("sound.tts", True)
    assert plugin_manifest.disabled_ids() == {"sound.audiobook"}


def test_set_enabled_on_unknown_id_raises():
    with pytest.raises(KeyError, match="没有这个能力"):
        plugin_manifest.set_enabled("sound.nope", False)


def test_state_file_roundtrip_and_atomicity(tmp_path):
    """写状态必须是原子替换：写一半崩溃留下的坏 JSON 会被 `disabled_ids()`
    当成"一个都没关"，也就是**全部启用** —— 那正是最危险的方向。"""
    plugin_manifest.write_disabled({"sound.tts"})
    assert plugin_manifest.disabled_ids() == {"sound.tts"}
    assert not list(tmp_path.glob("*.tmp")), "临时文件没被 replace 掉"


def test_corrupt_state_file_falls_back_to_all_enabled():
    """损坏 → 全开。宁可多开（用户看得见、能自己关），也不要静默关掉一堆能力。"""
    plugin_manifest.STATE_FILE.write_text("{ not json", encoding="utf-8")
    assert plugin_manifest.disabled_ids() == set()


# ---------------------------------------------------------------- 4. 关了就真的不挂


def test_mount_plan_skips_disabled():
    plugin_manifest.write_disabled({"sound.audiobook", "sound.tts"})
    plan = plugin_manifest.mount_plan()
    ids = {pid for pid, _ in plan}
    assert "sound.tts" not in ids
    assert "sound.audiobook" not in ids
    # 对照：结构全貌仍然包含它们
    assert "sound.tts" in {pid for pid, _ in plugin_manifest.mount_plan(include_disabled=True)}


def test_mount_plan_marks_skipped_modules():
    """跳过必须**记账**，否则 `state_of()` 会把"你关的"报成"未注册"。"""
    plugin_manifest.write_disabled({"sound.audiobook", "sound.tts"})
    plugin_manifest.mount_plan()
    skip = plugin_manifest.skipped()
    for mod in _p("sound.tts").routers:
        assert (mod, plugin_loader.ROUTER_PURPOSE) in skip


def test_skipped_is_not_reported_as_unregistered(monkeypatch):
    """关掉的能力不该出现在异常清单里 —— 但它**没被关的邻居**必须照报。

    对照组是关键：只断言"关掉的没有 reason"会被一个恒真的实现蒙过去
    （比如把"未注册"这条整个删掉）。
    """
    monkeypatch.setattr(plugin_loader, "results", lambda: [])
    plugin_manifest.write_disabled({"sound.audiobook", "sound.tts"})
    plugin_manifest.mount_plan()

    _, reasons_off = plugin_manifest.state_of(_p("sound.tts"), {"sound.tts"})
    assert reasons_off == [], reasons_off

    _, reasons_on = plugin_manifest.state_of(_p("sound.ft"), {"sound.tts"})
    assert any("未注册" in r for r in reasons_on), "没被关的能力仍该照报"


def test_hooks_for_skips_disabled():
    """`sound.tts` 的钩子就是那个"起线程加载 4.9G 模型"的 warmup ——
    关掉 TTS 却照跑 warmup，等于省了个入口没省显存。"""
    plugin_manifest.write_disabled({"sound.audiobook", "sound.tts"})
    off = {h["module"] for h in plugin_manifest.hooks_for("import")}
    on = {h["module"] for h in plugin_manifest.hooks_for("import", include_disabled=True)}
    assert "warmup" not in off
    assert "warmup" in on


# ---------------------------------------------------------------- 5. 端点


@pytest.fixture()
def client():
    return TestClient(server.app)


def test_disable_endpoint_returns_409_and_names_the_dependent(client):
    resp = client.post("/api/plugins/sound.tts/disable")
    assert resp.status_code == 409
    assert "sound.audiobook" in resp.json()["detail"]


def test_disable_endpoint_returns_400_for_core(client):
    resp = client.post("/api/plugins/core.voices/disable")
    assert resp.status_code == 400
    assert "核心" in resp.json()["detail"]


def test_disable_endpoint_returns_404_for_unknown(client):
    assert client.post("/api/plugins/sound.nope/disable").status_code == 404


def test_enable_disable_roundtrip_via_api(client):
    assert client.post("/api/plugins/sound.audiobook/disable").status_code == 200
    resp = client.post("/api/plugins/sound.tts/disable")
    assert resp.status_code == 200
    assert resp.json()["restartRequired"] is True, "开关是重启生效的，接口必须说清"
    assert plugin_manifest.disabled_ids() == {"sound.tts", "sound.audiobook"}
    assert client.post("/api/plugins/sound.tts/enable").status_code == 200
    assert plugin_manifest.disabled_ids() == {"sound.audiobook"}


def test_preset_endpoint(client):
    resp = client.post("/api/plugins/preset", json={"preset": "light"})
    assert resp.status_code == 200
    assert resp.json()["preset"] == "light"
    assert set(resp.json()["disabled"]) == _ids() - plugin_manifest.preset_ids("light")


def test_preset_endpoint_rejects_unknown(client):
    assert client.post("/api/plugins/preset", json={"preset": "nope"}).status_code == 404


def test_catalog_exposes_switch_state(client):
    plugin_manifest.write_disabled({"sound.audiobook"})
    body = client.get("/api/plugins").json()
    by_id = {p["id"]: p for p in body["plugins"]}

    assert by_id["sound.audiobook"]["enabled"] is False
    assert by_id["sound.tts"]["enabled"] is True
    # 「被依赖而保留」的能力 `state` 是 disabled 但 `enabled` 是 True ——
    # 前端开关必须读 `enabled`，不能拿 `state` 推。
    assert by_id["sound.tts"]["state"] != "disabled" or by_id["sound.tts"]["enabled"]

    assert body["restartRequired"] is True
    assert {p["id"] for p in body["presets"]} == set(plugin_manifest.PRESETS)
    assert body["preset"] == "custom", "逐项改过之后不该再声称是某个预设"


def test_catalog_reports_the_current_preset(client):
    plugin_manifest.apply_preset("standard")
    assert client.get("/api/plugins").json()["preset"] == "standard"


def test_blocked_by_tells_you_what_to_close_first(client):
    body = client.get("/api/plugins").json()
    by_id = {p["id"]: p for p in body["plugins"]}
    assert by_id["sound.tts"]["blockedBy"] == ["sound.audiobook"]
    # 叶子能力（没人依赖它）→ 想关就关
    assert by_id["sound.audiobook"]["blockedBy"] == []
    # 核心能力被一堆人依赖，但拦它的**不是**依赖守卫而是"核心不可关"（400）——
    # 所以这里 `blockedBy` 照样如实列出，别拿它当"能不能关"的唯一判据。
    assert "sound.workshop" in by_id["core.voices"]["blockedBy"]


# ---------------------------------------------------------------- 6. 变异验证
#
# 上面所有断言在真代码上都是绿的。要区分"没有违规"与"检查失效"，
# 必须往真对象里注入真违规，看断言**会不会红**。


def test_409_goes_red_when_the_guard_is_removed(monkeypatch, client):
    """变异：把关闭守卫摘掉 → 409 必须消失。

    若 409 是写死的（比如接口里硬编码一份"这些不许关"），这条不会红，
    而清单以后加了新依赖时守卫就会静默失效。
    """
    monkeypatch.setattr(plugin_manifest, "dependents_of", lambda pid, d=None: [])
    assert client.post("/api/plugins/sound.tts/disable").status_code == 200


def test_400_goes_red_when_core_is_no_longer_protected(monkeypatch, client):
    """变异：让 `is_core` 恒 False → 核心能力就能被关掉了，400 必须消失。

    挑 `core.history` 而不是 `core.voices`：后者还被 8 个能力 `requires`，
    摘掉 `is_core` 后会先撞上**依赖守卫**（409）而不是 400 ——
    那样这条变异验证的就是守卫顺序，不是"核心不可关"这条规则本身。
    """
    monkeypatch.setattr(
        plugin_manifest.Plugin, "is_core", property(lambda self: False), raising=True
    )
    assert client.post("/api/plugins/core.history/disable").status_code == 200
