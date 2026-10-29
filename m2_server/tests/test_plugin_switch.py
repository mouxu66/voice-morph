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
    plugin_manifest.write_disabled({"sound.audiobook", "sound.course", "sound.tts"})
    on = plugin_manifest.enabled_ids()
    assert "sound.tts" not in on
    assert "sound.audiobook" not in on


def test_dependents_of_names_the_dependents():
    # sound.tts 有两个依赖方（2026-10-05 加 sound.course「讲稿录课」）：
    # 有声书挂在输字变声页 tab 上，讲稿录课同样挂在那一页 → 都 requires tts。
    # ★ 这条断言曾因漏改红过：方案④加了 sound.course 却没同步这里，
    # 而门禁 --fast 的 FAST_TESTS 白名单不含本文件 → 漏到全量才炸（2026-10-05 修）。
    assert plugin_manifest.dependents_of("sound.tts") == ["sound.audiobook", "sound.course"]
    assert plugin_manifest.dependents_of("sound.offline-vc") == ["sound.audition"]
    assert sorted(plugin_manifest.dependents_of("sound.workshop")) == ["sound.ft", "sound.mine"]
    # pet.companion 现有两个依赖方：桌宠皮肤与手机遥控（2026-10-02 加hook.remote）。
    # 顺序按 id 升序（`dependents_of` 里sorted 的口径），别写成集合 —— 那样
    # 顺序 regressions 就看不见了，而它恰好是前端提示文案里会用到的那一个。
    assert plugin_manifest.dependents_of("pet.companion") == ["hook.remote", "pet.market"]
    # 遥控依赖的三项：实时变声 / 微信发送 / 桌宠采集
    assert plugin_manifest.dependents_of("sound.rvc-live") == ["hook.remote"]
    # hook.assistant（2026-10-23）：会话助理要发东西，所以 requires hook.wechat ——
    # 依赖的方向是"助理 → 发送"，不是反过来。
    assert plugin_manifest.dependents_of("hook.wechat") == ["hook.assistant", "hook.remote"]


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
    # 只剩 sound.course 还开着 → 它仍是依赖者，不能算 []。
    assert plugin_manifest.dependents_of("sound.tts") == ["sound.course"]
    plugin_manifest.write_disabled({"sound.audiobook", "sound.course"})
    assert plugin_manifest.dependents_of("sound.tts", {"sound.audiobook", "sound.course"}) == []


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


# ---------------------------------------------------------------- 1.5 停用路线（RETIRED）
#
# 2026-10-14 用户拍板：克隆自己的声音**统一走「音色训练」(RVC)**，千问微调不再用
# （依据 `docs/product/research/m1f_千问微调vsRVC-声音克隆裁决_2026-10-14.md`）。
#
# 这一节把那个产品决定钉成断言，因为它的失效方式**全是静默的**：
#   ① 停用清单里 id 拼错一个字母 → 什么都没停用，但没有任何报错；
#   ② 某个预设（尤其「全能」）留个后门 → 用户换个套餐，那条路线又悄悄回来；
#   ③ 有人给停用路线加了硬依赖 → "停用"与"被依赖则保留"两条语义打架，
#      结果是它被复活，而界面上看着像开关坏了。


def test_retired_ids_are_real_plugins():
    """① 停用清单里的 id 必须真实存在 —— 拼错一个字母等于什么都没停。"""
    unknown = set(plugin_manifest.RETIRED) - _ids()
    assert not unknown, f"RETIRED 里有不存在的插件 id：{sorted(unknown)}"


def test_no_preset_ships_a_retired_route():
    """② 任何预设（**含「全能」**）都不带停用路线 —— 留个后门等于口径没落地。"""
    for name in plugin_manifest.PRESETS:
        leaked = plugin_manifest.preset_ids(name) & set(plugin_manifest.RETIRED)
        assert not leaked, f"预设 {name} 带上了停用路线 {sorted(leaked)}"


def test_retired_is_never_a_hard_dependency():
    """③ 停用路线不能被任何人 `requires` —— 否则它会在启用侧被复活。"""
    plugins = plugin_manifest.by_id()
    for pid in plugin_manifest.RETIRED:
        depended = [q.id for q in plugins.values() if pid in q.requires]
        assert not depended, (
            f"{pid} 被 {depended} 硬依赖，无法停用（先解依赖，或把停用决定收回）"
        )


def test_retired_route_is_still_manually_enableable():
    """停用是**默认口径**，不是封杀：设置页手动开启仍然可用（可逆）。

    这条守住「不删代码」那一半 —— 若哪天有人把 RETIRED 接进 `disabled_ids()`，
    这里会红：那样写会让"用户手动开回来"变成不可能，而这个决定本来是错的。
    """
    pid = next(iter(plugin_manifest.RETIRED))
    plugin_manifest.set_enabled(pid, False)
    assert pid not in plugin_manifest.enabled_ids()
    plugin_manifest.set_enabled(pid, True)
    assert pid in plugin_manifest.enabled_ids(), f"{pid} 停用后应当仍能被手动开回来"


def test_plugin_json_retired_flag_matches_manifest():
    """`disable.retired`（插件自己的声明）与 `RETIRED`（逻辑）必须点名同一批 —— 防漂移。

    两处是两个读者：`plugin.json` 是给人看的（为什么停用），`RETIRED` 是给
    `preset_ids()` 用的。只改一处会让"文档说停了、代码还在装"。
    """
    declared: set[str] = set()
    plugins_dir = Path(plugin_manifest.__file__).parent / "plugins"
    for p in sorted(plugins_dir.glob("*/plugin.json")):
        entry = json.loads(p.read_text(encoding="utf-8"))
        if (entry.get("disable") or {}).get("retired"):
            declared.add(entry["id"])
    assert declared == set(plugin_manifest.RETIRED), (
        f"声明侧 {sorted(declared)} ≠ 逻辑侧 {sorted(plugin_manifest.RETIRED)}"
    )


def test_unknown_preset_raises():
    with pytest.raises(KeyError, match="未知预设"):
        plugin_manifest.preset_ids("standrad")


def test_apply_preset_is_additive_by_default():
    """★ 2026-10-15 用户拍板：套用预设**默认只增不减**，绝不新增 `disabled`。

    改之前是「整套重设」（`off = 全部 - 套餐`），于是点一下模式就把用户手动开的能力
    全部写进 `disabled`。用户原话：「切换模式会把我自己手动打开的功能给关掉 ……
    不能这么搞」——2026-10-08 那次只加了「会关掉哪几项」的小字提示（知情权），
    2026-10-15 明确：知情权不够，语义本身要改。

    所以：默认路径只做加法（套餐点名的从 `disabled` 里移除），
    收窄必须显式 `strict=True`（见下一条）。
    """
    # 先手动开一项套餐外的能力（模拟用户逐项开过）
    plugin_manifest.set_enabled("sound.audiobook", True)
    rep = plugin_manifest.apply_preset("light")
    assert rep["mode"] == "merge"
    assert "sound.audiobook" not in rep["disabled"], "★ 默认路径关掉了用户手动开启的能力"
    assert "sound.audiobook" in rep["enabled"], "用户手动开的没保住"
    assert "sound.audiobook" in rep["kept"], "kept 没如实报出被保留的手动项"
    # 加法的另一半：套餐点名的能力被补齐
    assert "sound.offline-vc" in rep["enabled"]


def test_apply_preset_strict_is_the_old_whole_set_reset():
    """「收窄到只剩这个套餐」必须**显式** —— 旧语义整段保留在 `strict=True` 上。

    写进文件的是「用户想关的」，不是「最终启用的」：被依赖而保留的能力不该被记成
    「用户主动开的」，否则切走预设再切回来，会凭空多出几个开启项，用户根本不知道
    自己"开"过它们（这条语义没变，变的只是它现在不再跟着默认路径走）。
    """
    rep = plugin_manifest.apply_preset("light", strict=True)
    keep = plugin_manifest.preset_ids("light")
    assert rep["mode"] == "strict"
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

    为什么本体里没有 `sound.tts`（输字变声）—— 本体是**声音→声音**的变声，
    文字→语音是另一条生成路线；它同时是边际依赖最大的那个（`qwen-tts` + 4.9G 模型），
    正是"按需下载"最该覆盖的。训练 / 微调 / 发掘 / 效果器同理。
    """
    standard = plugin_manifest.preset_ids("standard")
    body = set(plugin_manifest.BODY_IDS)
    assert standard == plugin_manifest.expand(body), "standard 不再是 BODY_IDS 的闭包"
    assert standard - {p.id for p in plugin_manifest.load_all() if p.is_core} == body
    # 被排除的可选项逐个点名 —— 漏掉一个就是"本体"惄悄变胖
    # （2026-10-05 加 sound.watermark：溯源水印是可选增强，不进变声最小闭环）
    # （2026-10-05 加 sound.blindtest：盲听评测是主观听测工具，零 GPU 依赖，按需开启）
    for pid in (
        "sound.workshop",
        "sound.ft",
        "sound.mine",
        "sound.tts",
        "sound.audiobook",
        "sound.effects",
        "sound.watermark",
        "sound.blindtest",
        "pet.companion",
        "pet.market",
        "hook.wechat",
    ):
        assert pid not in standard, f"{pid} 不该在变声本体里"
    # 本体必须真的能"变"和"听"：三个入口页都得在（少一个就是"本体不完整"）
    routes = {r["path"] for p in plugin_manifest.load_all() if p.id in standard for r in p.routes}
    assert {"/offlinevc", "/live", "/audition"} <= routes, sorted(routes)


# 2026-10-03 用户拍板：三个**场景模式**——按「用户是谁」分组（打游戏的不装微信、
# 录课老师不用实时变声）。同样钉成字面量：改这三个套餐必须来改这里，
# 并且回答"这个场景为什么多了/少了一项"。套餐只是起点，套用后仍可逐项开关。

def test_scene_presets_are_pinned_literals():
    """三个场景套餐的**起点集**钉死：微信变声 / 声音保存（护嗓）/ 游戏变声。"""
    # hook.radar（2026-10-23）：微信场景天然要「该不该回」雷达 —— 只看屏幕不发消息，
    # 与 hook.wechat 互不依赖，所以单独列出而不是挂进 wechat 的 requires。
    # hook.assistant（2026-10-23）：会话助理 = 把"发歌/发语音"变成待确认提案，
    # 是这个场景的主闭环，和雷达一起进套餐；它 requires hook.wechat（依赖闭包自动补）。
    assert plugin_manifest.PRESETS["wechat"] == [
        "sound.offline-vc", "sound.tts", "hook.wechat", "sound.fx-board", "sound.course",
        "hook.radar", "hook.assistant"]
    assert plugin_manifest.PRESETS["preserve"] == [
        "sound.preserve", "sound.tts", "sound.audiobook", "sound.course"]
    # 游戏变声 = 变声本体本身（实时变声走虚拟声卡进游戏/直播/会议）
    assert plugin_manifest.PRESETS["game"] == list(plugin_manifest.BODY_IDS)
    # 标签直接说场景 —— 设置页那一排按钮是用户唯一看得见套餐含义的地方
    for name in ("wechat", "preserve", "game"):
        assert plugin_manifest.PRESET_LABELS[name], f"{name} 缺中文标签"


def test_no_preset_label_says_bianshen_with_body_character():
    """★ 术语守卫：预设标签一律「变**声**」，不许出现「变**身**」。

    2026-10-05 用户在侧栏一眼看错：「微信变身 / 游戏变身」在这产品里
    字面义是"变换身体"，而产品是变声（变声工坊 / 输字变声 / 实时变声）。
    错字已全量改正，这里钉住 —— 标签是纯文案，没有别的门禁会拦它，
    下一个人抄旧 commit 就可能写回去。
    """
    offenders = {
        pid: label
        for pid, label in plugin_manifest.PRESET_LABELS.items()
        if "变身" in label
    }
    assert not offenders, f"预设标签误用「变身」（应为「变声」）：{offenders}"


def test_preset_labels_are_about_voice_not_body():
    """正向钉住三个场景标签的字面值：改文案就是改产品口径，必须有意为之。"""
    assert plugin_manifest.PRESET_LABELS["wechat"] == "微信变声"
    assert plugin_manifest.PRESET_LABELS["game"] == "游戏变声"
    assert plugin_manifest.PRESET_LABELS["preserve"] == "声音保存（护嗓）"
    # "本体"要写进标准档标签：设置页那一排按钮是用户唯一能看见"标准含什么"的地方
    assert "变声本体" in plugin_manifest.PRESET_LABELS["standard"]


def test_scene_presets_expand_to_right_scopes():
    """展开后的作用域各归各的场景：带什么、**不带什么**都要点名。"""
    wechat = plugin_manifest.preset_ids("wechat")
    preserve = plugin_manifest.preset_ids("preserve")
    game = plugin_manifest.preset_ids("game")
    # 微信：tab 宿主页（输字变声，/tts?tab=wechat）+ 发送 + 特效声板 + 录课
    # （2026-10-08：course 零额外依赖，不加重装机，不该被场景切换埋掉）；
    # 实时变声 / 声音保存不该混进来
    assert {"sound.tts", "hook.wechat", "sound.fx-board", "sound.offline-vc", "sound.course"} <= wechat
    assert "sound.rvc-live" not in wechat and "sound.preserve" not in wechat
    # 保存：三件套齐（有声书靠 requires 闭包带上 tts）+ 录课（2026-10-08：
    # preserve 的目标用户就是录课老师，PPT→讲课视频是主打闭环）；不碰微信 / 实时
    assert {"sound.preserve", "sound.tts", "sound.audiobook", "sound.course"} <= preserve
    assert "hook.wechat" not in preserve and "sound.rvc-live" not in preserve
    # 游戏 = 标准（同一套展开结果，只是叫用户听得懂的名字）
    assert game == plugin_manifest.preset_ids("standard")


def test_scene_presets_have_pages():
    """每个场景至少有一张自己的入口页 —— 套完模式进去是空首页就是事故。"""
    for name, expected in (
        # 微信发送页没有独立路由 —— 它是输字变声页的 tab（/tts?tab=wechat），
        # 所以入口页判 /tts；preserve 的建档页、游戏的实时页都有独立路由
        ("wechat", "/tts"),
        ("preserve", "/preserve"),
        ("game", "/live"),
    ):
        routes = {
            r["path"]
            for p in plugin_manifest.load_all()
            if p.id in plugin_manifest.preset_ids(name) for r in p.routes
        }
        assert expected in routes, f"{name} 模式没有入口页 {expected}（现有：{sorted(routes)}）"


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


def test_deleted_state_file_after_seeding_means_all_on(monkeypatch, tmp_path):
    """播种过之后配置文件**被删** → 不再重置成默认套餐，按全开（fail-open）处理。

    真实事故（2026-10-08）：用户删了 plugins.json 想重置，重启后被静默关回 18 项 ——
    旧播种逻辑把「文件缺失」一律当「全新安装」。播种标记把两者分开：
    全新安装照旧播种 `standard`；删过之后的缺失 = 全开。
    恢复默认套餐必须是设置页里套用预设的显式动作，不该由删除操作隐式触发。
    """
    monkeypatch.delenv(plugin_manifest.SEED_ENV, raising=False)
    state = tmp_path / "plugins.json"
    monkeypatch.setattr(plugin_manifest, "STATE_FILE", state)
    assert not state.exists(), "夹具给的必须是空状态"

    first = plugin_manifest.ensure_state_file()
    assert first == _ids() - plugin_manifest.preset_ids("standard")
    assert plugin_manifest._seed_marker().exists(), "首次播种必须留下标记"

    # ↓↓ 与旧语义的分岔点：标记在而文件没了 = 误删，不重播种
    state.unlink()
    assert plugin_manifest.ensure_state_file() is None
    assert not state.exists(), "不许把默认套餐悄悄写回去"
    assert plugin_manifest.disabled_ids() == set(), "误删 = 全开（fail-open），不是 standard"

    # write_disabled 也会补标记：从未播种过的机器上，用户手动开关过 → 同样受保护
    plugin_manifest._seed_marker().unlink()
    plugin_manifest.write_disabled({"sound.tts"})
    assert plugin_manifest._seed_marker().exists()


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
    assert rep["blocked_by"] == ["sound.audiobook", "sound.course"]
    assert rep["enabled"] is True
    assert plugin_manifest.disabled_ids() == set()


def test_disable_succeeds_once_the_dependent_is_off():
    # 两个依赖方（audiobook / course）都得先关 —— 少关一个 tts 就仍被拉住（2026-10-05）
    plugin_manifest.set_enabled("sound.audiobook", False)
    plugin_manifest.set_enabled("sound.course", False)
    rep = plugin_manifest.set_enabled("sound.tts", False)
    assert rep["enabled"] is False
    assert plugin_manifest.disabled_ids() == {"sound.tts", "sound.audiobook", "sound.course"}


def test_enable_clears_the_flag():
    plugin_manifest.set_enabled("sound.audiobook", False)
    plugin_manifest.set_enabled("sound.course", False)
    plugin_manifest.set_enabled("sound.tts", False)
    plugin_manifest.set_enabled("sound.tts", True)
    assert plugin_manifest.disabled_ids() == {"sound.audiobook", "sound.course"}


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
    plugin_manifest.write_disabled({"sound.audiobook", "sound.course", "sound.tts"})
    plan = plugin_manifest.mount_plan()
    ids = {pid for pid, _ in plan}
    assert "sound.tts" not in ids
    assert "sound.audiobook" not in ids
    # 对照：结构全貌仍然包含它们
    assert "sound.tts" in {pid for pid, _ in plugin_manifest.mount_plan(include_disabled=True)}


def test_mount_plan_marks_skipped_modules():
    """跳过必须**记账**，否则 `state_of()` 会把"你关的"报成"未注册"。"""
    plugin_manifest.write_disabled({"sound.audiobook", "sound.course", "sound.tts"})
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
    plugin_manifest.write_disabled({"sound.audiobook", "sound.course", "sound.tts"})
    plugin_manifest.mount_plan()

    _, reasons_off = plugin_manifest.state_of(_p("sound.tts"), {"sound.tts"})
    assert reasons_off == [], reasons_off

    _, reasons_on = plugin_manifest.state_of(_p("sound.ft"), {"sound.tts"})
    assert any("未注册" in r for r in reasons_on), "没被关的能力仍该照报"


def test_hooks_for_skips_disabled():
    """`sound.tts` 的钩子就是那个"起线程加载 4.9G 模型"的 warmup ——
    关掉 TTS 却照跑 warmup，等于省了个入口没省显存。"""
    plugin_manifest.write_disabled({"sound.audiobook", "sound.course", "sound.tts"})
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
    assert client.post("/api/plugins/sound.course/disable").status_code == 200
    resp = client.post("/api/plugins/sound.tts/disable")
    assert resp.status_code == 200
    assert resp.json()["restartRequired"] is True, "开关是重启生效的，接口必须说清"
    assert plugin_manifest.disabled_ids() == {"sound.tts", "sound.audiobook", "sound.course"}
    assert client.post("/api/plugins/sound.tts/enable").status_code == 200
    assert plugin_manifest.disabled_ids() == {"sound.audiobook", "sound.course"}


def test_preset_endpoint_is_additive_by_default(client):
    """端点默认也是只增不减（`strict` 缺省 false）—— 前端不传就是安全的那个。"""
    plugin_manifest.set_enabled("sound.audiobook", True)
    resp = client.post("/api/plugins/preset", json={"preset": "light"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["preset"] == "light"
    assert body["mode"] == "merge"
    assert "sound.audiobook" in body["kept"]
    assert "sound.audiobook" not in body["disabled"]


def test_preset_endpoint_strict_resets_the_whole_set(client):
    """显式 `strict=true` 才是旧的整套重设（写给「我就是要只剩这一个模式」）。"""
    resp = client.post("/api/plugins/preset", json={"preset": "light", "strict": True})
    assert resp.status_code == 200
    assert resp.json()["mode"] == "strict"
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
    """派生口径没变：禁用集与该套餐的补集**逐字相等**时才报那个套餐名。

    ⚠️ 只增不减之后，点模式**不一定**能让 `preset` 变成该模式：你手动开的项被保留，
    禁用集就不再等于该套餐的补集 —— 这正是「保留」的代价，如实报 `custom` 比
    冒充套餐好（前端也用响应里的 `kept` 把这件事说出来，而不是在这儿假装）。
    想得到确定的 `standard`，走显式 strict。
    """
    plugin_manifest.apply_preset("standard", strict=True)
    assert client.get("/api/plugins").json()["preset"] == "standard"


def test_merge_keeps_the_preset_field_honest(client):
    """只增不减下 `preset` 报 `custom`：保留了你开的项，就不再是那个套餐的补集。

    这条是给人看的护栏：任何「让点模式后仍然高亮该模式」的 UI 小聪明，
    都必须绕开这个派生口径 —— 而绕开它就会在侧栏说一个后端不认的模式名。
    """
    plugin_manifest.set_enabled("sound.workshop", False)  # 你已经逐项改过
    client.post("/api/plugins/preset", json={"preset": "light"})
    assert client.get("/api/plugins").json()["preset"] == "custom"


def test_blocked_by_tells_you_what_to_close_first(client):
    body = client.get("/api/plugins").json()
    by_id = {p["id"]: p for p in body["plugins"]}
    # sound.tts 的依赖方有两个：sound.audiobook（有声书）+ sound.course（讲稿录课），
    # 两者都挂在输字变声页 tab 上 → 想关 tts 得先关这两个（2026-10-05 随 sound.course 增补）。
    assert by_id["sound.tts"]["blockedBy"] == ["sound.audiobook", "sound.course"]
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
    plugin_manifest.write_disabled({"sound.tts", "sound.audiobook", "sound.course"})
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
    from fastapi.routing import APIRoute

    c = TestClient(server.app, raise_server_exceptions=False)
    # ⚠️ `raise_server_exceptions=False` 是**必需**的，不是偷懒。
    # 探针扫的是全部无参 GET 路径（现算，不手抄），只要有**一条**在当前机器上
    # 抛了非 HTTP 异常（实测：`/api/audition/*` 会去 ffmpeg 探一个坏 wav，
    # `Error opening input file .../reference.wav`），默认的 TestClient 会把异常
    # 重新抛到探针里 —— 整个子进程非零退出，这条测试就以
    # `assert proc.returncode == 0` 的形式红，而失败信息里只看得见 ffmpeg 的报错，
    # 完全看不出「真正在测的那件事」是什么。
    # 关掉重抛之后：坏端点老实报自己的状态码（多半 500），
    # 探针跑完把全部结果交回来 —— A/B 比对照样成立，还顺手留下"这个端点坏了"的痕迹。
    result = {
        "modules": [m for _pid, m in pm.mount_plan()],
        "mounted": len(server._ROUTER_ORDER),
        "broken": [r.module for r in server.plugin_loader.broken()],
    }

    def api_routes(routes):
        \"\"\"递归取出所有 APIRoute —— **不假设 app.routes 的形状**。

        这版 FastAPI（0.141+）把 include_router 的结果包成 `_IncludedRouter`，
        子路由在 `.original_router.routes` 里，第一层 `app.routes` 直接过滤
        `APIRoute` 只能捞到 SPA 兜底那一条。写成递归是为了FastAPI 换回
        扁平结构时也能用（那时`original_router` 不存在，routes 自己就是 APIRoute）。
        \"\"\"
        for r in routes:
            if isinstance(r, APIRoute):
                yield r
            inner = getattr(getattr(r, "original_router", None), "routes", None)
            if inner:
                yield from api_routes(inner)

    paths = sorted(
        {r.path for r in api_routes(server.app.routes)
         if "GET" in getattr(r, "methods", set()) and "{" not in r.path}
    )
    # 报回去让断言能验「探针真的扫到东西了」—— 抽取逻辑一失效，
    # 下面所有 `off[path] == 404` 都会变成 KeyError 或者（更糟）空集合全过。
    result["paths"] = paths
    for path in paths:
        r = c.get(path)
        result[path] = [r.status_code, r.headers.get("content-type", "").split(";")[0]]
    print("RESULT " + json.dumps(result))
    """
)


def _boot_and_probe(out_dir: Path, disabled: str | None) -> dict:
    """在独立子进程里真启动一次后端并探几个端点（挂载发生在 import 时，没得兼）。

    ⚠️ **`plugins.json` 两边都显式写，让 A/B 差**只有**那一个 id**。
    原来只写 `off` 侧、`on` 侧留空目录，正确性靠的是
    `m2_server/conftest.py:38` 那个全局 `VM_PLUGIN_SEED=0`（没有它，
    `ensure_state_file()` 会在空目录上按下默认套餐播种，A 侧就不是"全开"
    而是 standard，被关掉的能力压根没挂过）。这是个**跨文件的环境变量约定** ——
    一旦有人为别的理由把它挪掉或改成条件性的，这里会静默退化成"测不到东西"。
    自己写 `disabled: []` 不依赖任何外部开关。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    # 全开基线：`disabled: []` ≠ 不写文件（不写 = 让播种决定，而播种写的是 standard）
    (out_dir / "plugins.json").write_text(
        json.dumps({"disabled": [disabled] if disabled else []}), encoding="utf-8"
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
    """A/B：同一段脚本、只差一个状态文件 —— 关掉的能力端点必须 404 + JSON。

    ⚠️ **挑的插件必须「无人 requires 它」**，否则 `enabled_ids` 的复活闭包会把它
    留下来（"关掉 A 不能把还在用的 B 弄坏"），端点就永远 404 不了 ——
    这条会以一种极不像真bug 的方式红：`off` 与 `on` 完全一样。
    2026-10-02 加了 `hook.remote`（requires `sound.rvc-live`）之后，
    原本挑的 `sound.rvc-live` 就失去了这个前提，改用 `sound.cover`
    （无人依赖、有 `/api/cover/status`）。前提本身由
    `test_the_disabled_probe_target_is_still_undepended_on` 钉住。
    """
    on = _boot_and_probe(tmp_path / "on", None)
    off = _boot_and_probe(tmp_path / "off", "sound.cover")

    assert on["broken"] == [] and off["broken"] == []
    # A 侧：干净启动时这些模块真在挂载计划里（否则 B 侧的"消失了"是废话）
    # ⚠️ **从清单推导**，不写死模块名/个数 —— 给一个插件加一个 router 是常规操作，
    # 写死会让这条与「关了端点就没」无关的用例变红（2026-09-23 就因为多了一个
    # `rvc_dataset_api` 假红过一次）。
    owned = set(plugin_manifest.by_id()["sound.cover"].routers)
    assert owned, "sound.cover 一个 router 都没有？"
    assert owned <= set(on["modules"])
    assert not owned & set(off["modules"]), "关了却还在挂载计划里"
    assert off["mounted"] == on["mounted"] - len(owned)

    # 探针扫的是**全部已挂载的无参 GET 路径**（现算，不手抄），
    # 所以「cover 自己的路径集」可以直接从 on 的键里筛出来 ——
    # 关掉 cover_api 后它整体消失，于是这些键在 off 里要么不存在、要么 404。
    gone = {p for p in on if p.startswith("/api/cover/")}
    # 这一条同时在钉**A 侧真的是"全开"**：`/api/cover/*` 只在 cover 被挂载时存在。
    # 若 A 侧读到的是按下默认套餐播种出来的状态，cover 压根没被挂载、`gone` 为空，
    # 本条就静默通过（后面所有 `off[path] == 404` 都成了废话）。
    assert gone, (
        f"探针没扫到任何 /api/cover/* 路径（扫到 {len(on['paths'])} 条）—— "
        "要么抽取逻辑失效了，要么 A 侧不是全开（`_boot_and_probe` 必须自己写 "
        "`disabled: []`，不能靠空目录 + 外部的 VM_PLUGIN_SEED=0）。别让它假绿"
    )
    assert set(off["paths"]) == set(on["paths"]) - gone, (
        f"关掉 sound.cover 改变的路径集与预期不同：\n"
        f"  多出/变了的: {sorted(set(off['paths']) - (set(on['paths']) - gone))}\n"
        f"  少了的    : {sorted((set(on['paths']) - gone) - set(off['paths']))}"
    )
    # 关掉的路径在 off 侧要么不存在（探针扫不到），要么明确是 404+JSON。
    # 必须是 JSON：200 text/html 意味着被 SPA 兜底吞了，
    # 前端只会看到 `Unexpected token '<'`（这正是本节最初要拦的那个坑）。
    for path in gone:
        if path in off:
            assert off[path] == [404, "application/json"], (
                f"{path} 关掉后应当是 404 + JSON，实际 {off[path]}"
            )
    # 反向：**别的路径一条都不能变** —— 前面那半句只证明"关掉了"，
    # 这一半才证明"只关掉了它"。
    # ⚠️ 唯一的例外：这条路径背后是**真的外网**（抖音/B站榜单，见 music_api.charts），
    # 两次启动各自成败不同（502「拉不到」vs 200）与"关掉 sound.cover"毫无关系。
    # 2026-10-29 实测：全量 fast 里 on=502 / off=200 假红一次，单跑又全绿。
    # 只跳过**状态码相同**的比较，仍要求它在 off 侧存在（路径集不变）。
    _network_paths = {"/api/music/charts"}
    for path in set(on["paths"]) - gone - _network_paths:
        assert path in off, f"探针两次扫到的路径集不同 —— on 有 {path} 而 off 没有"
        assert off[path] == on[path], (
            f"关掉 sound.cover 顺带改了 {path}：{on[path]} → {off[path]}"
        )
    assert on["/api/health"] == off["/api/health"] == [200, "application/json"]


def test_the_disabled_probe_target_is_still_undepended_on():
    """上一条关掉的是 `sound.cover` —— 它必须仍无人 `requires`，否则端点不会消失。

    这条是给"换样本"这件事上的锁：清单以后加依赖时，**要换样本的是测试，
    不是放宽断言**（放宽就会变成"关掉也能在"这种静默腐烂）。
    """
    depended = [p.id for p in plugin_manifest.by_id().values() if "sound.cover" in p.requires]
    assert not depended, (
        f"sound.cover 现在被 {depended} 依赖了 —— 关掉它时 enabled_ids 会把它复活，"
        "test_a_disabled_capability_endpoint_is_gone_at_the_http_level 就测不到东西了。"
        "请换一个仍无人依赖、且有 status 端点的插件"
    )


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
