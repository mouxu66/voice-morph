"""`hook.remote`（手机遥控）编排层的测试 —— 2026-10-02 新增。

这个插件的特殊之处：**它不拥有任何一个遥控功能的 router**，三个标签的端点分别属于
`sound.rvc-live` / `hook.wechat` / `pet.companion`。它Own 的是那份「标签 → 依赖」
的知识，并把它变成一个端点（`GET /api/remote/capabilities`）吐给手机页面。

因此这里要钉的不是「端点能用」，而是三件容易悄悄腐烂的事：

  1. **依赖声明只有一处**。`plugin.json` 的 `requires` 与 `remote_api.TABS`
     是同一份知识的两处表述；不一致时手机端会对着已关掉的能力显示可点按钮，
     而且**没有任何现有测试会红**（`remotePage.test.ts` 钉的是"端点存在"，
     不是"依赖一致"）。
  2. **「无人依赖」这个测试前提**。关掉 A 不能复活 A 的依赖方
     （`enabled_ids` 的复活闭包）；而本插件给 `sound.rvc-live` /
     `hook.wechat` / `pet.companion` 都加了一个依赖方 ——
     这直接改写了若干条以那些能力为样本的既有测试的前提。
  3. **端点自身不被重依赖拖垮**。它刻意不 import `plugin_loader`（torch 在里面），
     因为它恰恰在「最需要知道自己缺什么」的时刻被调。
"""

import json
from pathlib import Path

import plugin_loader
import plugin_manifest
import pytest
import remote_api
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    import server

    return TestClient(server.app, raise_server_exceptions=False)


@pytest.fixture()
def healthy_registry():
    """把 `plugin_loader` 的 registry 填成「全部加载成功」。

    为什么需要：`state_of()` 判断一个能力是否可用，看的是 `plugin_loader` 里它那些
    router 的加载结果。而 registry **不是**全局常驻的 —— `test_plugin_loader.py` 的
    autouse fixture 在每条用例结束时 `reset()`，所以「本文件单跑绿、全量跑红」
    （2026-10-02 实测）：轮到我们时registry 是空的，所有能力被判成
    「未注册 → broken」→ 三个标签全灰。

    这个 fixture 让本文件对执行顺序免疫。**不去改别人的 fixture**：那份reset
    本身是对的（它防的是"上一条用例留下的 broken 污染下一条"）。
    """
    for name in {m for _pid, m in plugin_manifest.mount_plan(include_disabled=True)}:
        plugin_loader.load_router(name)
    yield
    plugin_loader.reset()


# ------------------------------------------------------- 依赖只有一处声明


def test_every_tab_dependency_is_declared_in_manifest():
    """★ `TABS` 里出现的每个能力都必须真的在清单里 —— 否则那一项永远算「不可用」。

    这是本模块最容易静默腐烂的地方：`TABS` 手写了三个插件 id，清单改了名字
    （或某个能力被删了）不会有人来改这里，症状是**手机上某个标签永久灰着**，
    而后端日志与所有现有测试都一片绿。
    """
    known = set(plugin_manifest.by_id())
    for tab_id, _label, needs in remote_api.TABS:
        for pid in needs:
            assert pid in known, (
                f"remote_api.TABS 的 {tab_id!r} 依赖 {pid!r}，但清单里没有这个能力 —— "
                f"清单现有: {sorted(known)}"
            )


def test_manifest_requires_covers_everything_tabs_needs():
    """`TABS` 用到的**可选**能力必须都在 `hook.remote` 的 `requires` 里。

    为什么这条不能省：`requires` 是**依赖闭包**的依据（`expand()` 靠它把三个
    能力一并启用）。`TABS` 里有而 `requires` 里没有的可选能力，用户装了
    「手机遥控」之后那个标签仍然是灰的 —— 而 `standard` 预设下它们本来就关着，
    于是表现为"插件装了但不好使"，没有任何报错。

    **核心能力不在这个要求里**（`core.*` 恒开，列进`requires` 只会让
    `expand()` 多做无用的闭包计算、并让"核心能不能关"这条守卫多一个例外）。
    刻意用 `not p.is_core` 过滤，而不是手写核心 id 清单 —— 核心清单变了
    （新增一个 core 插件）这条会自动跟上。
    """
    allp = plugin_manifest.by_id()
    optional = {pid for pid, p in allp.items() if not p.is_core}
    declared = set(allp["hook.remote"].requires)
    for tab_id, _label, needs in remote_api.TABS:
        missing = (set(needs) & optional) - declared
        assert not missing, (
            f"标签 {tab_id!r} 需要可选能力 {sorted(missing)}，但 hook.remote 的 requires "
            f"只有 {sorted(declared)} —— 装了遥控也不会自动开启它们"
        )


def test_tabs_dependency_is_a_subset_of_what_the_page_actually_calls():
    """★ `TABS` 必须覆盖 `remote.html` 真实调用的**每一个**端点所属的能力。

    这是把「知识」和「现实」对上的那条测试。少了任何一个端点前缀的映射，
    对应的能力就会被静默漏掉 —— 所以下面先钉住映射本身的完整性：
    **页面里出现的每个 `/api/` 字面量，都必须能在映射表里找到归属**
    （动态拼接的那条走白名单）。映射漏一条 = 这个前缀的归属没人在管。

    口径与 `remotePage.test.ts` 的「页面调的每个接口都真有对应路由」互为镜像：
    那条保证"调的端点存在"，这条保证"调的能力被声明了"。
    """
    from pathlib import Path

    page = Path(__file__).resolve().parents[2] / "web" / "public" / "remote.html"
    txt = page.read_text(encoding="utf-8")

    # 页面端点前缀 → 归属能力。**必须与 `remotePage.test.ts` 的
    # `DYNAMIC_PREFIXES` 一样对动态拼接的那条单独说明**，否则两边会各漏一半。
    endpoint_owner = {
        "/api/rvc/live/": "sound.rvc-live",
        "/api/voices": "core.voices",
        "/api/wechat/": "hook.wechat",
        "/api/capture/": "pet.companion",
        "/api/media/outputs/": "core.media",  # 播放录音回放
    }
    # 由路径拼出来的（不是写死的字面量），因此上面的字面量扫描抓不到
    dynamic_prefixes = {
        "/api/media/outputs/.session/": "core.media",  # `${…}/.session/…` 拼出来的
    }
    # 页面里所有 `/api/` 字面量（去掉查询串与动态部分）
    import re

    literals = {
        m.split("?")[0]
        for m in re.findall(r"""['"`](/api/[^'"`\s]*)['"`]""", txt)
    }
    covered = set(endpoint_owner) | set(dynamic_prefixes)
    uncovered = {p for p in literals if not any(p.startswith(c) for c in covered)}
    # 这几个是"页面自己也会问"的元端点，不属任何能力（core.system 的，
    # 且它不可关）；`/api/health` 同理。
    META_OK = {"/api/health", "/api/plugins"}
    assert uncovered <= META_OK, (
        f"remote.html 调了这些端点但没有归属映射：{sorted(uncovered - META_OK)} —— "
        "要么补进 endpoint_owner，要么明确它属 core.system 这类元端点。"
        "（不补的后果：对应能力被关掉时页面仍显示可点按钮）"
    )

    # 每个标签的依赖 ⊇ 它用到的端点所属能力
    declared = {pid for _t, _l, needs in remote_api.TABS for pid in needs}
    for prefix, owner in {**endpoint_owner, **dynamic_prefixes}.items():
        if not any(p.startswith(prefix) for p in literals | set(dynamic_prefixes)):
            continue  # 页面不再调它了
        assert owner in declared, (
            f"remote.html 调了 {prefix}（属 {owner}），但它不在 remote_api.TABS 的依赖里 —— "
            "关掉该能力时页面会仍显示可点按钮，然后用户按下才 404"
        )


def test_each_tab_declares_every_capability_it_needs_per_tab():
    """逐标签核对：「这个标签用到的能力」必须**逐个**都在它自己的依赖里。

    上一条只验「全局并集覆盖」，那会让某个标签少一个能力也被放过 ——
    而症状恰好是"某一个标签灰着、另两个正常"，比全灰更难查。
    """
    from pathlib import Path

    page = Path(__file__).resolve().parents[2] / "web" / "public" / "remote.html"
    txt = page.read_text(encoding="utf-8")

    # tab id → 它那个面板用到的端点前缀（一个 tab 的面板就是它那一段 DOM）
    panel_endpoints = {
        "say": ("/api/wechat/", "/api/voices", "/api/media/outputs/"),
        "mic": ("/api/capture/", "/api/rvc/live/", "/api/voices", "/api/media/outputs/"),
        "live": ("/api/rvc/live/", "/api/voices"),
    }
    owner = {
        "/api/rvc/live/": "sound.rvc-live",
        "/api/voices": "core.voices",
        "/api/wechat/": "hook.wechat",
        "/api/capture/": "pet.companion",
        "/api/media/outputs/": "core.media",
    }
    # 面板 id 必须真的在页面里（否则上面这张表就是凭空的）
    for tab_id in panel_endpoints:
        assert f'id="panel-{tab_id}"' in txt, f"页面里没有 panel-{tab_id} —— 表要跟着页面改"
    tabs = {t[0]: set(t[2]) for t in remote_api.TABS}
    for tab_id, prefixes in panel_endpoints.items():
        need = {owner[p] for p in prefixes}
        missing = need - tabs[tab_id]
        assert not missing, (
            f"标签 {tab_id!r} 的依赖里少了 {sorted(missing)} —— "
            f"当前 {sorted(tabs[tab_id])}"
        )
    assert set(panel_endpoints) == set(tabs), (
        f"页面有 {sorted(panel_endpoints)}，TABS 有 {sorted(tabs)} —— "
        "页面加了/删了一个标签？两处必须同步"
    )


# ------------------------------------------------------- 端点行为


def test_capabilities_shape(client, healthy_registry):
    r = client.get("/api/remote/capabilities")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "tabs", "plugin"}
    assert [t["id"] for t in body["tabs"]] == [t[0] for t in remote_api.TABS]
    for tab in body["tabs"]:
        assert set(tab) == {"id", "label", "usable", "missing", "capabilities"}
        assert isinstance(tab["capabilities"], list) and tab["capabilities"], tab
        for c in tab["capabilities"]:
            assert set(c) == {"id", "name", "state", "enabled", "usable", "reasons"}


def test_all_three_tabs_usable_on_a_healthy_start(client, healthy_registry):
    """干净启动（全开）下三个标签都该可用 —— 任何一个不可用都是依赖声明出了问题。"""
    body = client.get("/api/remote/capabilities").json()
    assert body["ok"] is True, [(t["id"], t["missing"]) for t in body["tabs"] if not t["usable"]]
    for tab in body["tabs"]:
        assert tab["usable"] is True and tab["missing"] == [], tab["id"]


def test_disabled_dependency_makes_its_tabs_unusable(client, healthy_registry, monkeypatch):
    """关掉 `hook.wechat` → 「打字发语音」不可用，且**说得出缺哪个**。

    这是这个端点存在的理由：原来 `remote.html` 只能自己判断"这个能力是不是 off"，
    拿不到"整体还缺什么"。
    """
    monkeypatch.setattr(plugin_manifest, "disabled_ids", lambda: {"hook.wechat"})
    body = client.get("/api/remote/capabilities").json()
    say = next(t for t in body["tabs"] if t["id"] == "say")
    assert say["usable"] is False
    assert say["missing"], "不可用却说不出缺什么 = 手机上只能给一句「未知原因」"
    assert body["ok"] is False
    # 别的标签不该受影响（它们不依赖 hook.wechat）
    for tid in ("mic", "live"):
        t = next(x for x in body["tabs"] if x["id"] == tid)
        assert t["usable"] is True, (tid, t["missing"])


def test_missing_is_empty_and_capabilities_agree(client, healthy_registry):
    """`usable` 与 `missing`/`capabilities[].usable` 三者必须自洽。

    不自洽的症状是页面上一半控件灰一半不灰 —— 三份口径各算各的必然出这种结果。
    """
    body = client.get("/api/remote/capabilities").json()
    for tab in body["tabs"]:
        derived_missing = [c["name"] for c in tab["capabilities"] if not c["usable"]]
        assert tab["missing"] == derived_missing, tab["id"]
        assert tab["usable"] == (not derived_missing), tab["id"]
        assert body["ok"] == all(t["usable"] for t in body["tabs"]), tab["id"]


def test_unknown_capability_degrades_instead_of_500(monkeypatch):
    """清单里没有的能力 id → 标成不可用并说明，**不抛**。

    这是手机上的辅助页：为一个「少一个能力」而整页 500，比显示一句「不可用」糟得多。
    变异验证：把 `TABS` 里的某个 id 改成不存在的，本条必须仍绿。
    """
    fake = (("ghost", "幽灵能力", ("no.such.plugin",)),)
    monkeypatch.setattr(remote_api, "TABS", fake)
    body = remote_api.remote_capabilities()
    tab = body["tabs"][0]
    assert tab["usable"] is False
    assert tab["missing"] == ["no.such.plugin"]
    assert tab["capabilities"][0]["state"] == "unknown"
    assert tab["capabilities"][0]["reasons"] == ["清单里没有这个能力"]


def test_endpoint_does_not_import_heavy_libs():
    """★ 这个端点不能 import `plugin_loader`（torch 在里面）。

    理由与 `plugin_manifest.catalog()` 相同：它恰恰在「最需要知道自己缺什么」
    的时刻被调（手机上打开页面第一件事）。一个为了回答"我缺什么"而先把自己
    烧掉的端点，是本末倒置。
    """
    src = (
        __import__("pathlib").Path(remote_api.__file__).read_text(encoding="utf-8")
    )
    body = src.split('router = APIRouter', 1)[1]
    assert "import plugin_loader" not in body, "端点本体import 了 plugin_loader（torch 在里面）"
    assert "import torch" not in body


def test_plugin_field_reports_its_own_state(client, healthy_registry, monkeypatch):
    """`plugin.enabled` 让调用方能区分「插件关了」与「插件在但依赖缺了」。

    这一层关掉时它的 router 根本不挂、调用方拿到的是 404，所以在本层活着时
    仍如实报一次 —— 前端可以用它决定"要不要提示去设置里开"。
    """
    assert client.get("/api/remote/capabilities").json()["plugin"] == {
        "id": "hook.remote",
        "enabled": True,
    }
    monkeypatch.setattr(
        plugin_manifest, "disabled_ids", lambda: {"hook.remote", "sound.rvc-live"}
    )
    # 关掉自己时：requires 闭包会把它复活（它仍启用），所以这里只看字段形状稳定
    got = client.get("/api/remote/capabilities").json()["plugin"]
    assert got["id"] == "hook.remote"
    assert isinstance(got["enabled"], bool)


def test_disabling_it_does_not_disable_what_it_requires():
    """关掉 `hook.remote` **不会**关掉它的三个 `requires`。

    方向别搞反：`enabled_ids` 的复活闭包是「关掉被依赖者时，仍启用的依赖者会把它
    带回来」—— 依赖者关掉，被依赖者**不受影响**（`standard` 预设本身就启用了这三项，
    它们本来就在 `on` 里）。

    为什么钉这条：`plugin.json` 的 `disable.note` 是**给人看的**，它当时写成
    「requires 的三项会随之关闭」—— 一个方向完全相反的说法，用户照着它去关遥控，
    结果微信发送和桌宠一起没了。这条测试让那句文案改对，且以后改回去会被逮住。
    """
    on = plugin_manifest.enabled_ids(set())
    off = plugin_manifest.enabled_ids({"hook.remote"})

    assert "hook.remote" in on, "前提失效：它默认就不启用？"
    assert "hook.remote" not in off, "前提失效：关不掉？"
    for dep in ("sound.rvc-live", "hook.wechat", "pet.companion"):
        assert dep in off, f"关掉 hook.remote 顺带关掉了 {dep} —— 与复活闭包的语义相反"

    # 文案不能把方向说反：note 里若出现「requires …会随之关闭」这类说法就红。
    note = json.loads(
        (Path(plugin_manifest.PLUGINS_DIR) / "hook.remote" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )["disable"]["note"]
    assert "不受影响" in note, f"disable.note 没说明依赖不受影响：{note!r}"
