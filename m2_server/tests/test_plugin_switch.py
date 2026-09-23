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

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

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


def _mk(pid: str, requires: tuple[str, ...] = ()) -> plugin_manifest.Plugin:
    """造一个虚拟可选插件（不落盘），专给「真实清单里还没有的图」用。"""
    return plugin_manifest.Plugin(
        id=pid,
        name=pid,
        kind="builtin",
        category="sound",
        order=9,
        summary="",
        routers=(f"{pid}_api",),
        requires=requires,
    )


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


def test_enabled_ids_revives_transitively(monkeypatch):
    """复活是传递闭包：B 依赖 A、C 依赖 B，关掉 A+B 只留 C → A、B 都得回来。

    真实清单里没有「可选→可选→可选」的链（现在唯一的第二跳是核心，永远开着），
    所以用虚拟图钉住 —— 别等清单以后加了这么一条、上线才被咬。
    """
    graph = {
        "C": _mk("C", ("B",)),
        "B": _mk("B", ("A",)),
        "A": _mk("A", ()),
    }
    monkeypatch.setattr(plugin_manifest, "by_id", lambda: graph)
    on = plugin_manifest.enabled_ids({"A", "B"})
    assert on == {"A", "B", "C"}, "C 开着 → B 复活 → B 依赖的 A 也必须复活"


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


# ---------------------------------------------------------------- 2.5 「本体」与首次运行
#
# 2026-09-23 用户拍板：一"本体"只留**变声**，其余 9 项默认关闭、按需开启。
# 这两条用例把那个产品决定钉成**字面量** —— 改 `PRESETS["standard"]` 就必须来改这里，
# 而且得回答"本体为什么多了/少了一项"。


def test_default_preset_is_the_voice_morph_body():
    """「标准」= 变声本体：**选音色 → 变 → 听** 的最小闭环。

    为什么本体里没有 `sound.tts`（输字变声）—— 本体是**声音→声音**的变身，
    文字→语音是另一条生成路线；它同时是边际依赖最大的那个（`qwen-tts` + 4.9G 模型），
    正是"按需下载"最该覆盖的。训练 / 微调 / 发掘 / 效果器同理。
    """
    standard = plugin_manifest.preset_ids("standard")
    body = set(plugin_manifest.BODY_IDS)
    assert standard == plugin_manifest.expand(body), "standard 不再是 BODY_IDS 的闭包"
    assert standard - {p.id for p in plugin_manifest.load_all() if p.is_core} == body
    # 被排除的 9 项逐个点名 —— 漏掉一个就是"本体"惄悄变胖
    for pid in (
        "sound.workshop",
        "sound.ft",
        "sound.mine",
        "sound.tts",
        "sound.audiobook",
        "sound.effects",
        "pet.companion",
        "pet.market",
        "hook.wechat",
    ):
        assert pid not in standard, f"{pid} 不该在变声本体里"
    # 本体必须真的能"变"和"听"：三个入口页都得在（少一个就是"本体不完整"）
    routes = {r["path"] for p in plugin_manifest.load_all() if p.id in standard for r in p.routes}
    assert {"/offlinevc", "/live", "/audition"} <= routes, sorted(routes)


def test_first_run_seeds_the_default_preset_once(monkeypatch):
    """首次运行写一份**显式**状态，之后永不覆盖用户的选择。

    缺文件时 `disabled_ids()` 是空集（全开），所以"本体"若不落盘就只是**纸上**的
    默认值 —— 新用户装完照样看到 20 项全开，而 `_current_preset()` 还会把它报成 `full`。
    """
    monkeypatch.delenv(plugin_manifest.SEED_ENV, raising=False)
    assert not plugin_manifest.STATE_FILE.exists(), "夹具给的必须是空状态"

    seeded = plugin_manifest.ensure_state_file()
    assert seeded == _ids() - plugin_manifest.preset_ids("standard")
    assert plugin_manifest.disabled_ids() == seeded, "写进去的和读出来的是同一份"
    assert plugin_manifest.enabled_ids() == plugin_manifest.preset_ids("standard")

    # 用户改过之后，再播种必须是 no-op —— 否则重启一次就把用户的选择抹了
    plugin_manifest.write_disabled({"sound.tts"})
    assert plugin_manifest.ensure_state_file() is None
    assert plugin_manifest.disabled_ids() == {"sound.tts"}


def test_seeding_is_off_by_default_in_tests():
    """测试环境不播种（`m2_server/conftest.py` 的 `VM_PLUGIN_SEED=0`）。

    否则 `import server` 在**收集阶段**就会按默认套餐写掉状态文件，
    `server._ROUTER_ORDER` 会只剩本体的 router，而期望"20 项全挂"的那批用例集体变红 ——
    且红的直接原因看起来与插件开关毫无关系。这条用例守住那个开关。
    """
    assert os.environ.get(plugin_manifest.SEED_ENV) == "0"
    assert plugin_manifest.ensure_state_file() is None
    assert not plugin_manifest.STATE_FILE.exists()


#: 探针：在**子进程**里把 `import server` 跑一遍（挂载只在这一刻发生）。
#: 上面的单测只能证明"文件被写了"，证明不了"**这次启动真的按它挂了**" ——
#: 而后者需要一个从零开始的 `mount_plan()`，同进程里拿不到（`import server` 只跑一次）。
#:
#: 复用同一个 `VM_SEED_PROBE_OUT` 跑两次就是「重装 → 用 → 重启」的最小复现：
#:   · `fresh`    —— 新机器，无配置文件；
#:   · `existing` —— 开机前用户已把 `sound.audiobook` 关掉（直接写盘，不经过播种）。
#:     选它是因为它是**叶子**（`dependents_of` 为空），关掉会真的少挂 router，
#:     断言才能看出"用户的选择被尊重了"。换 `sound.tts` 就不行 —— 它会被
#:     `sound.audiobook` 复活，看起来就像关不掉（那是另一条不变量，见上面的单测）。
_SEED_PROBE = """
import json
import os
import pathlib
import sys

os.environ["VM_OUTPUTS_DIR"] = os.environ["VM_SEED_PROBE_OUT"]
os.environ["VM_WARMUP"] = "0"
os.environ.pop("VM_PLUGIN_SEED", None)

if sys.argv[1] == "existing":
    (pathlib.Path(os.environ["VM_SEED_PROBE_OUT"]) / "plugins.json").write_text(
        json.dumps({"disabled": ["sound.audiobook"]}), encoding="utf-8")

import plugin_manifest as pm  # noqa: E402
import server  # noqa: E402

print("RESULT " + json.dumps({
    "file": pm.STATE_FILE.exists(),
    "disabled": sorted(pm.disabled_ids()),
    "routers": sorted(server._ROUTER_ORDER),
    "preset": pm._current_preset(pm.disabled_ids(), pm.load_all()),
}))
"""


def _run_seed_probe(out_dir: Path, mode: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _SEED_PROBE, mode],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "VM_SEED_PROBE_OUT": str(out_dir), "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, f"探针（{mode}）就失败了：\n{proc.stdout}\n{proc.stderr}"
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, f"探针没打出结果：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(line.removeprefix("RESULT "))


def test_first_run_really_mounts_only_the_body(tmp_path):
    """★ 端到端：**新机器装完启动一次**，挂载的就是本体 —— 不是纸上默认值。

    这条同时守住一个顺序不变量：`ensure_state_file()` 必须在 `mount_plan()` **之前**调用
    （`server.py::_mount_all()` 开头）。写反了就是"种了一份状态、但这次启动仍按旧的算"，
    而表面上文件也写了、下次启动也对 —— 只有真跑一次才能发现。

    顺带钉住 `_current_preset()`：以前缺文件时它把全开报成 `full`，
    所以新用户在设置页看到的是「全能」而不是「标准」。
    """
    body = plugin_manifest.preset_ids("standard")
    expected = sorted(
        m
        for p in plugin_manifest.load_all()
        if p.id in body
        for m in p.routers
    )
    assert expected, "本体一个 router 都没有？"
    assert len(expected) < len(
        [m for p in plugin_manifest.load_all() for m in p.routers]
    ), "本体等于全量，这条用例就没有区分力了"

    got = _run_seed_probe(tmp_path, "fresh")
    assert got["file"] is True, "首次运行没有落盘"
    assert got["routers"] == expected, (
        f"挂载集与本体不符。多了 {sorted(set(got['routers']) - set(expected))}，"
        f"少了 {sorted(set(expected) - set(got['routers']))}"
    )
    assert got["preset"] == "standard"
    assert got["disabled"] == sorted(_ids() - body)


def test_restart_after_user_choice_is_not_reseeded(tmp_path):
    """★ 端到端：同一个 OUTPUTS_DIR 再启动一次 —— 播种必须让路。

    这是播种最危险的失效形态：把"初始化默认值"写成"每次开机都重置"，
    用户关掉的能力会在重启后全部弹回来（而且看起来就像开关坏了）。
    """
    first = _run_seed_probe(tmp_path, "fresh")
    assert first["file"] is True

    again = _run_seed_probe(tmp_path, "existing")
    assert again["disabled"] == ["sound.audiobook"], "播种覆写了用户的选择"
    all_routers = sorted(m for p in plugin_manifest.load_all() for m in p.routers)
    dropped = set(plugin_manifest.by_id()["sound.audiobook"].routers)
    assert dropped, "sound.audiobook 没有 router，这条用例就白测了"
    assert again["routers"] == [m for m in all_routers if m not in dropped], (
        "用户只关了有声书，挂载集却变了 —— 播种是不是把本体又按了回去？"
    )
    assert again["preset"] == "custom"


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


# ---------------------------------------------------------------- 6. 体检跟着启用集走
#
# 设计稿 §八把这条列为「可关」的收益之三：现在是"没装 RVC 就一片红"，
# 噪声掩盖真问题。关掉相关能力后，体检不该再为它报红。


def test_diagnose_owners_exist_in_the_manifest():
    """`_DIAGNOSE_OWNERS` 是手工维护的表，会漂 —— 至少钉住「id 都在清单里」。

    写错 id 的症状极坏：那个体检项会**永远**被跳过（没人启用一个不存在的插件），
    用户永远看不到「缺 RVC」的提示，而界面上是一片绿。
    """
    known = _ids()
    import system_api

    for key, owners in system_api._DIAGNOSE_OWNERS.items():
        assert owners, f"{key} 的 owner 是空的 —— 那这项永远会被跳过"
        for pid in owners:
            assert pid in known, f"{key} 指向不存在的能力 {pid!r}"


def _all_optional_off_except_rvc_live():
    """关掉除「实时变声」外的**全部**可选能力。

    用集合算而不是手写 id 列表：只关 `sound.tts` 关不掉它 —— `sound.audiobook`
    依赖它，会被"被依赖而保留"拉回来；只关 `sound.workshop` 同理（`sound.ft`/`sound.mine`
    依赖它）。手写列表会随着清单新增依赖而悄悄失效。
    """
    plugin_manifest.write_disabled(
        {p.id for p in plugin_manifest.load_all() if not p.is_core and p.id != "sound.rvc-live"}
    )


def test_diagnose_skips_items_whose_capabilities_are_all_off(client):
    _all_optional_off_except_rvc_live()
    body = client.get("/api/diagnose").json()
    keys = {i["key"] for i in body["items"]}

    assert "tts_models" not in keys, "输字变声关了，不该再为它的模型报红"
    assert "torch" not in keys and "cuda" not in keys, "需要 torch 的能力全关了"
    # 没关的照旧检查（否则这条断言会被"全都跳过"蒙过去）
    assert "backend" in keys
    assert "rvc_root" in keys, "实时变声还开着，RVC 整合包仍要检查"

    # 跳过不是"假装通过" —— 原因要原样列出来
    skipped = {s["key"]: s["reason"] for s in body["skipped"]}
    assert "tts_models" in skipped
    assert "sound.tts" in skipped["tts_models"]


def test_diagnose_skipping_goes_away_when_nothing_is_disabled(client):
    """对照：什么都不关时，一项都不该被跳过。"""
    body = client.get("/api/diagnose").json()
    assert body["skipped"] == []


def test_diagnose_skip_is_driven_by_the_owner_table(monkeypatch, client):
    """变异：清空 owner 表 → 跳过必须全部消失。

    若「跳过」是靠别的东西实现的（比如写死一份 key 列表），这条不会红，
    而 owner 表以后改错了也不会有人发现。
    """
    import system_api

    monkeypatch.setattr(system_api, "_DIAGNOSE_OWNERS", {})
    _all_optional_off_except_rvc_live()
    body = client.get("/api/diagnose").json()
    assert "tts_models" in {i["key"] for i in body["items"]}
    assert body["skipped"] == []


# ---------------------------------------------------------------- 7. 健康探针
#
# 第 2 步只把它写进清单（"只声明不接线"），设计稿 §6.3 却说 `GET /api/plugins`
# 要带「健康探针结果」—— 又一个"声称了没接上"。第 6 步补掉。


def test_health_probe_runs_and_returns_the_raw_snapshot():
    """跑起来就回原始快照。**没有 `ok` 字段** —— 那是刻意的。"""
    p = _p("sound.tts")
    assert p.health, "sound.tts 应该声明了探针"
    probe = plugin_manifest.probe_health(p)
    assert probe is not None and probe["ran"] is True
    assert isinstance(probe["data"], dict)


def test_health_probe_is_none_when_not_declared():
    assert plugin_manifest.probe_health(_p("core.voices")) is None


def test_health_probe_degrades_instead_of_killing_the_catalog(monkeypatch):
    """★ 探针自己炸了，也必须变成一条结论 —— 不能让 `/api/plugins` 500。

    这条端点的存在理由就是「在最坏的时候还能答」（缺依赖时最需要看它），
    让它依赖探针等于在最坏的时候把它关掉。
    """
    import importlib

    def boom(name):
        raise ImportError(f"假装 {name} 装不上")

    monkeypatch.setattr(importlib, "import_module", boom)
    probe = plugin_manifest.probe_health(_p("hook.wechat"))
    assert probe["ran"] is False
    assert "ImportError" in (probe["error"] or "")


def test_disabled_plugin_is_not_probed():
    """关掉的能力不跑探针 —— 它连模块都没加载，问它健康没有意义。"""
    plugin_manifest.write_disabled({"sound.tts", "sound.audiobook"})
    catalog = plugin_manifest.catalog()
    by_id = {p["id"]: p for p in catalog["plugins"]}
    assert by_id["sound.tts"]["healthProbe"] is None
    assert by_id["hook.wechat"]["healthProbe"] is not None


# ---------------------------------------------------------------- 8. 变异验证
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


# ---------------------------------------------------------------- 9. 关了之后，端点真的没了（HTTP 层）
#
# 上面第 6 节只钉到 `mount_plan()` 这一层（"清单说跳过"）。而用户/前端感受到的是
# **HTTP 语义**，两者之间恰好隔着一个坑：最后的 SPA 兜底 `@app.get("/{full_path:path}")`
# 会吞掉一切没匹配上的 GET 路径，包括 `/api/*`。
#
# 2026-09-21 在**已安装副本**上实测：关掉 `sound.rvc-live` 后 `/api/rvc/live/status`
# 返回 **200 `text/html`**（index.html）。后果比 404 难查得多：前端抛
# `Unexpected token '<'`（像前端 bug）、Network 里没有红、而验收清单里
# 「点一下应该 404」那条判据永远不成立。所以必须把断言落到响应上，不是落到内部变量上。

_M2 = Path(plugin_manifest.__file__).resolve().parent

_PROBE = textwrap.dedent(
    """
    import json, sys
    sys.path.insert(0, ".")
    import plugin_manifest as pm, server
    from fastapi.testclient import TestClient

    c = TestClient(server.app)
    result = {
        "modules": [m for _pid, m in pm.mount_plan()],
        "mounted": len(server._ROUTER_ORDER),
        "broken": [r.module for r in server.plugin_loader.broken()],
    }
    # `/api/rvc/dataset` 是 `rvc_dataset_api` 的端点 —— 它 2026-09-23 从 `sound.ft`
    # 划到 `sound.rvc-live`（服务的是 Live 页驱动的 RVC 训练流程，跟 `/ft/*` 的
    # 「用录音继续训练 TTS 音色」是两件事）。探它 = 把新归属钉在 HTTP 层。
    for path in ("/api/rvc/live/status", "/api/cascade/status", "/api/rvc/dataset", "/api/health"):
        r = c.get(path)
        result[path] = [r.status_code, r.headers.get("content-type", "").split(";")[0]]
    print("RESULT " + json.dumps(result))
    """
)


def _boot_and_probe(out_dir: Path, disabled: str | None) -> dict:
    """在独立子进程里真启动一次后端并探几个端点（挂载发生在 import 时，没得兼）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    if disabled:
        (out_dir / "plugins.json").write_text(
            json.dumps({"disabled": [disabled]}), encoding="utf-8"
        )
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "VM_WARMUP": "0",
        "VM_BACKEND_AUTOSYNC": "0",
        "VM_OUTPUTS_DIR": str(out_dir),
    }
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(_M2),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, f"后端没起来：\n{proc.stderr[-1500:]}"
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, f"探针没打出结果：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(line[len("RESULT ") :])


def test_a_disabled_capability_endpoint_is_gone_at_the_http_level(tmp_path):
    """A/B：同一段脚本、只差一个状态文件 —— 关掉的能力端点必须 404 + JSON。"""
    on = _boot_and_probe(tmp_path / "on", None)
    off = _boot_and_probe(tmp_path / "off", "sound.rvc-live")

    assert on["broken"] == [] and off["broken"] == []
    # A 侧：干净启动时这些模块真在挂载计划里（否则 B 侧的"消失了"是废话）
    # ⚠️ **从清单推导**，不写死模块名/个数 —— 给一个插件加一个 router 是常规操作，
    # 写死会让这条与「关了端点就没」无关的用例变红（2026-09-23 就因为多了一个
    # `rvc_dataset_api` 假红过一次）。
    owned = set(plugin_manifest.by_id()["sound.rvc-live"].routers)
    assert owned, "sound.rvc-live 一个 router 都没有？"
    assert owned <= set(on["modules"])
    assert not owned & set(off["modules"]), "关了却还在挂载计划里"
    assert off["mounted"] == on["mounted"] - len(owned)

    assert on["/api/rvc/live/status"] == [200, "application/json"]
    for path in ("/api/rvc/live/status", "/api/cascade/status", "/api/rvc/dataset"):
        assert off[path] == [404, "application/json"], (
            f"{path} 关掉后应当是 404 + JSON，实际 {off[path]} —— "
            "如果是 200 text/html，就是被 SPA 兜底吞了（前端只会看到 `Unexpected token '<'`）"
        )
    assert on["/api/health"] == off["/api/health"] == [200, "application/json"]


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
