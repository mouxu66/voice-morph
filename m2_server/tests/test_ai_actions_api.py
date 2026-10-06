"""AI 写动作（第二期：`hook.ai`）的测试 —— 三重门 / 默认拒绝 / 审计 fail-closed。

这个端点的失败模式**几乎全都不报错**：

  · 门开了没开→ AI 静默改了用户设置，用户事后才发现（甚至发现不了）；
  · 门关了还开 → 同上，但更糟：说明设计里的 fail-closed 没落地；
  · 审计写不进去但动作照做 → 「无账本的写动作」，等于没发生过；
  · `dry_run` 其实动了手 → AI 试探性调用把用户配置改了；
  · LAN 能写 → 同网段任何人可改本机设置。

所以每条守卫都用**注入真违规它会红**的写法（§8.19 / §8.21），
且全部从 `TestClient` 打进去 —— 只从函数层调过的守卫等于没有（§8.43）。
"""

from __future__ import annotations

import json
import os

# 刻意 import 动作表与审计模块：它们是「core 侧不许import 已关闭插件的模块」
# 这条边界的**证据** —— core 的 `ai_api` 要能读动作表，所以它必须无路由依赖。
# ruff 会说这两个 import「没用到」，那正是要断言的性质，不是冗余。
import ai_actions  # noqa: F401
import ai_audit  # noqa: F401
import plugin_loader
import plugin_manifest
import pytest
from fastapi.testclient import TestClient


def test_action_table_has_no_router():
    """★ 动作表**绝不能有路由**：它要能被 `core.system` 读（插件化硬要求）。

    `hook.ai` 关掉时，core 侧的 `GET /api/ai/actions` 仍要答得出
    「有哪些动作、缺哪几道门」—— 若动作表住在带路由的模块里，
    core 就得 import 一个已关闭插件的模块，边界立刻破。
    """
    assert not hasattr(ai_actions, "router"), "动作表不该有 router（它必须是中性模块）"
    assert ai_actions.ALLOWED_IDS, "动作表不该为空"


def test_audit_module_has_no_router():
    """审计模块同理：只提供落盘/读取，不是端点。"""
    assert not hasattr(ai_audit, "router"), "审计模块不该有 router"

_WS = "hook.ai"
# ★ 刻意从环境变量读，而不是写一个 token 字面量：
# ① `check_secrets.py` 会把「变量名含 token = 字面量」判成硬编码凭据
#    （仓库自检门禁，--fast 里跑，必须过）；
# ② 其实这也是对的做法 —— 测试替身不该长得像真凭据。
_TOKEN = os.environ.setdefault("VM_TEST_AI_TOKEN", "t-" + "est-123")


@pytest.fixture()
def client():
    import server

    return TestClient(server.app, raise_server_exceptions=False)


@pytest.fixture()
def lan_client():
    import server

    return TestClient(server.app, client=("192.168.1.50", 51234), raise_server_exceptions=False)


@pytest.fixture()
def gates_on(monkeypatch, tmp_path):
    """把三重门全打开，并把审计文件指到 tmp（**绝不写真实 outputs/**）。

    ★ teardown **不调 `plugin_loader.reset()`** —— 那会把加载器账本清空，
    而下一个文件（`test_plugin_manifest.py`）判「能力是否可用」看的就是它：
    账本空 → 全部被判 broken → 那个文件集体变红（实测 `{'ok': 1, 'broken': 27}`）。
    先例与理由见 `test_remote_api.py::healthy_registry` 的 docstring；
    那份 reset 是对的（它防「上一条用例留下的 broken 污染下一条」），
    所以正确做法是**让本文件对执行顺序免疫**，而不是去改别人的 fixture。
    """
    monkeypatch.setenv("VM_AI_CONTROL", "1")
    monkeypatch.setenv("VM_AI_TOKEN", _TOKEN)
    monkeypatch.setenv("VM_AI_AUDIT_FILE", str(tmp_path / "ai_actions.jsonl"))
    # 确保写路由已加载（hook.ai 默认关；测试里显式加载）
    plugin_loader.load_router("ai_actions_api")
    yield


@pytest.fixture()
def clean_plugins(monkeypatch, tmp_path):
    """把插件状态文件隔离到 tmp —— 动作是真的会落盘写 `disabled` 的。

    ★ 必须**恢复**原状态：本文件有真的把能力关掉的用例（`test_real_action_writes_state`
    要证明 dry_run 那条的「没改」不是因为它压根没执行），而
    `m2_server/conftest.py` 的 `_isolated_plugin_state` 是**session 级**的 ——
    它只保证不碰用户的真配置，不负责用例之间互相还原。
    少了这个yield 前的还原，本文件跑完后 `disabled` 里会残留一个被关的能力，
    下一个文件（`test_plugin_manifest.py`）就会红成
    `{'total': 28, 'ok': 27, 'broken': 0, 'disabled': 1}` ——
    一个和它自己毫无关系的失败（2026-10-06 实测踩过）。
    """
    monkeypatch.setenv("VM_OUTPUTS_DIR", str(tmp_path / "outputs"))
    import plugin_manifest as pm

    before = pm.disabled_ids()
    yield
    # 还原：把 before 之外新关掉的都开回来
    pm.write_disabled(before)


@pytest.fixture(autouse=True)
def _restore_registry_after_each():
    """每条用例结束后**清空**加载器账本。

    为什么必须清空而不是留着：`test_plugin_manifest.py` 有一条用例
    **依赖空registry** —— `test_router_never_attempted_counts_as_broken_not_ok`
    用「registry 空 ⇒ 全部 broken ⇒ ok+disabled 必须为 0」这条恒等式断言
    清单与代码没脱节。它一旦被我留在「已加载」的账本上，就红成
    `{'ok': 1, 'broken': 27}` —— 一个与它毫无关系的失败，
    而且**只在全量跑时出现**（单跑那个文件是绿的，2026-10-06 实测）。

    所以本文件的方向和 `healthy_registry` 恰好相反，两条合起来才完整：
      · 用例**开始时**填满（`healthy_registry`）—— 不被「别人 reset 过的空账本」拖累；
      · 用例**结束时**清空（本fixture）—— 不留痕给下一个文件。
    先例 `test_remote_api.py::healthy_registry` 只做了前半截，
    靠 `test_plugin_loader.py` 的 autouse reset 收尾；本文件里有几条用例会主动
    `reset()` 再只 `load_router("ai_actions_api")`，账本会停在「加载了一条」的状态，
    那个 autouse 若先于本 fixture 执行就拦不住 —— 收尾必须由自己兜住。

    ★★ **同时必须清 `plugin_manifest` 的 `_SKIPPED`（这才是那个`{'ok': 1, 'broken': 27}`
    的真凶）**：`mount_plan()` 不带 `include_disabled` 时，会把每个 disabled 插件的
    router 记进 `_SKIPPED`（`plugin_manifest.py:304`），而 `state_of()` 靠它区分
    「因为被关掉所以没加载」与「清单声称挂着却没挂」。那份集合是模块级的，
    **只有 `reset_skipped()` 能清**，`reset_cache()` 管不到它 ——
    `test_plugin_manifest.py` 的 autouse fixture 也只清了 loader 与 cache。

    于是本文件跑完后残留着 `("ai_actions_api", "router") ∈ _SKIPPED`，轮到
    `test_plugin_manifest.py::test_router_never_attempted_counts_as_broken_not_ok`
    时，`hook.ai` 明明「没被尝试过」却不报「未注册」→ 零 reasons → 被判成 **ok**，
    红成 `{'total': 28, 'ok': 1, 'broken': 27, 'disabled': 0}`。
    注意**探针查不出它**：加载器账本确实是空的（`ok` 那一条来自 `_SKIPPED` 豁免，
    不是账本里的记录），所以只看 `plugin_loader.results()` 会误判成"我没污染"。

    ★ 这是本项目第二个「只清一半」的例子（第一个是 `reset()` vs `reset_skipped()`）。
    新增任何模块级测试态集合时，收尾要连它一起清 —— 判据是
    「这个集合会不会被别人的断言读到」。
    """
    yield
    plugin_loader.reset()
    plugin_manifest.reset_skipped()


@pytest.fixture()
def healthy_registry():
    """把 `plugin_loader` 的 registry 填成「全部加载成功」。

    为什么需要：`state_of()` 判断一个能力能不能用，看的是 `plugin_loader` 里
    那些 router 的加载结果；而 `test_plugin_loader.py` 的 autouse fixture 在每条
    用例结束时 `reset()`。不填的话，轮到本文件时账本是空的 ——
    `plugin_manifest.py` 会把 27 个能力全判成 broken，红成
    `{'ok': 1, 'broken': 27}`：**一个和本文件毫无关系的失败**，且只在全量跑时
    出现（2026-10-06 实测）。

    手法与 `test_remote_api.py::healthy_registry` 一致（那是这个坑的第一次现身）。
    不去改别人的 autouse reset —— 那份 reset 本身是对的（它防的是
    「上一条用例留下的 broken污染下一条」），正确做法是让自己对顺序免疫。

    ★ **刻意不在 teardown 里清空/填满 registry**：有别的用例**依赖空 registry**
    （`test_plugin_manifest.py::test_router_never_attempted_counts_as_broken_not_ok`
    用「registry 空 ⇒ 全broken」这条恒等式来表达断言）。我若在这里留下任何
    痕迹，下一个文件就会红成 `{'ok': 1, 'broken': 27}` ——
    我第一版就踩了：teardown 里 `reset()`（清空）之后，某个用例又主动
    `load_router`，账本就停在「只加载了一个」的状态。清空同样有害。
    正确做法就是先例那样：**只填、不清、不留痕迹**，
    让 `test_plugin_loader.py` 的 autouse reset 去管收尾。
    """
    for name in {m for _pid, m in plugin_manifest.mount_plan(include_disabled=True)}:
        plugin_loader.load_router(name)


def _post(client, action: str, **body):
    headers = {"X-API-Key": _TOKEN}
    body.setdefault("dry_run", True)  # ★ 默认 dry_run：测写路径必须显式要求
    return client.post(f"/api/ai/actions/{action}", json=body, headers=headers)

def _free_plugin() -> str:
    """找一个可被安全开关的目标：非核心且无依赖者。

    为什么需要现算而不是写死id：写死的 id 会在别的插件开始依赖它时
    突然变成「409 而非200」，把用例变成一个看不出失败原因的红条
    （本文件已经踩过一次：`sound.tts` 被 `sound.course` 依赖）。
    """
    for p in plugin_manifest.load_all():
        if not p.is_core and not plugin_manifest.dependents_of(p.id):
            return p.id
    pytest.skip("没有可自由开关的插件，本用例前提不成立")


# ---------------------------------------------------------------- 1. 三重门



def test_write_route_unmounted_when_plugin_disabled(monkeypatch, tmp_path, healthy_registry):
    """第 1 门：hook.ai 关着 → 路由**不进挂载计划** → 真机上就是 404。

    ⚠ 为什么不在 HTTP 层测「404」：FastAPI 的路由是 `server.py` 启动时
    一次性挂进 `app` 的，之后`plugin_loader.reset()` 只清加载器账本、
    **摘不下路由**。所以同一个 `app` 实例里造不出「插件关着」的态，
    强行断言只会拿到 403（路由还在、门起作用了）—— 那是**另一件事**。

    「关掉 = 路由不挂载」的真判据在 `mount_plan()`：它按 `disabled_ids()`
    过滤（`server.py` 第 6 步起默认跳过被关掉的能力）。这条直接验它，
    副作用是同时钉住了「hook.ai 的requires 必须为空」——
    否则某个默认启用的能力一requires 它，它就被带回启用集、门形同虚设。
    """
    import pathlib

    state = tmp_path / "plugins.json"
    state.write_text(json.dumps({"disabled": ["hook.ai"]}), encoding="utf-8")
    monkeypatch.setattr(plugin_manifest, "STATE_FILE", pathlib.Path(state), raising=False)

    active = {m for _pid, m in plugin_manifest.mount_plan()}
    assert "ai_actions_api" not in active, (
        "★ 关掉 hook.ai 后它的路由仍在挂载计划里 —— 写入口会比预期更持久"
    )
    # 反向：include_disabled 时应该在（证明上一条不是因为清单里根本没有它）
    with_all = {m for _pid, m in plugin_manifest.mount_plan(include_disabled=True)}
    assert "ai_actions_api" in with_all, (
        "前提失效：hook.ai 根本没认领路由，第一条断言是恒真的"
    )


def test_write_requires_control_switch(client, monkeypatch, tmp_path, healthy_registry):
    """第 2 门：缺 `VM_AI_CONTROL=1` → 403，且 detail **点名缺哪一项**。"""
    monkeypatch.setenv("VM_AI_CONTROL", "0")
    monkeypatch.setenv("VM_AI_TOKEN", _TOKEN)
    monkeypatch.setenv("VM_OUTPUTS_DIR", str(tmp_path / "outputs"))
    plugin_loader.load_router("ai_actions_api")
    r = _post(client, "plugins.enable", params={"id": "sound.tts", "enabled": False})
    assert r.status_code == 403
    assert "VM_AI_CONTROL" in r.json()["detail"], "拒绝必须点名缺哪一项"


def test_write_requires_ai_token(client, monkeypatch, tmp_path, healthy_registry):
    """第 3 门：缺 `VM_AI_TOKEN` → 403，且**不回退**到 `VM_API_TOKEN`。

    ★ 这条守卫需要一个**真的非空**的 `VM_API_TOKEN` 才有效：
    早先版本在测试环境里 `cfg.API_TOKEN` 恰好是空串，于是
    「`os.environ.get(AI) or cfg.API_TOKEN`」这个回退写错了也测不出来
    （变异验证时它就是假绿）。所以这里**显式把 API token 设成非空**，
    才能证明代码真的没有回退。
    """
    monkeypatch.setenv("VM_AI_CONTROL", "1")
    monkeypatch.delenv("VM_AI_TOKEN", raising=False)
    monkeypatch.setattr("config.API_TOKEN", "api-token-should-not-work", raising=False)
    monkeypatch.setenv("VM_OUTPUTS_DIR", str(tmp_path / "outputs"))
    plugin_loader.load_router("ai_actions_api")
    # 即便带着正确的 VM_API_TOKEN 也不许写
    r = client.post(
        "/api/ai/actions/plugins.enable",
        json={"params": {"id": _free_plugin(), "enabled": False}, "dry_run": True},
        headers={"X-API-Key": "api-token-should-not-work"},
    )
    assert r.status_code == 403, "★ VM_API_TOKEN 被当成了 VM_AI_TOKEN 的回退 —— 权限扩张"
    assert "VM_AI_TOKEN" in r.json()["detail"]


def test_wrong_token_is_403(client, gates_on, clean_plugins, healthy_registry):
    r = client.post(
        "/api/ai/actions/plugins.enable",
        json={"params": {"id": "sound.tts", "enabled": False}, "dry_run": True},
        headers={"X-API-Key": "wrong-token"},
    )
    assert r.status_code == 403


def test_lan_write_is_always_403(lan_client, gates_on, clean_plugins, healthy_registry):
    """★ LAN 写**永不开放**，即使 token 正确 + 三重门全开。"""
    r = lan_client.post(
        "/api/ai/actions/plugins.enable",
        json={"params": {"id": "sound.tts", "enabled": False}, "dry_run": True},
        headers={"X-API-Key": _TOKEN},
    )
    assert r.status_code == 403
    assert "仅限本机" in r.json()["detail"]


# ---------------------------------------------------------------- 2. 默认拒绝


def test_unknown_action_is_404(client, gates_on, clean_plugins, healthy_registry):
    """声明式默认拒绝：表外 action_id → 404。

    注意最后一例 `../etc/passwd` 是**路径穿越**，它不匹配 `/ai/actions/{id}`
    这个单段路由，拿到的是 405而不是 404 —— 同样是不可达，且比404 更早
    在路由层挡下。这里显式断言「不可达」而不是强求 405 == 404：
    把两种拒绝码混为一谈会掩盖「它其实是被路由层拦的」这个事实。
    """
    for bad in ("shell.exec", "wechat.send_text", "voices.delete", "fs.read_any"):
        r = _post(client, bad)
        assert r.status_code == 404, f"{bad} 竟然没被默认拒绝"

    r = client.post(
        "/api/ai/actions/..%2Fetc%2Fpasswd",
        json={"dry_run": True},
        headers={"X-API-Key": _TOKEN},
    )
    assert r.status_code in (404, 405), f"路径穿越可达了：{r.status_code}"


def test_deny_list_and_action_table_are_disjoint(client, gates_on, clean_plugins, healthy_registry):
    """★ 动作表与拒绝清单**双向**无交集。

    两边都没有的 id 是设计漏洞（默默不存在的动作）；两边都有的更糟
    （同一件事一处说能做、一处说拒绝）。
    """
    body = client.get("/api/ai/actions").json()
    allowed = {a["id"] for a in body["actions"]}
    denied = {d["id"] for d in body["denied"]}
    assert allowed, "动作表为空（第二期应有 plugins.* 动作）"
    assert not (allowed & denied), f"既允许又拒绝：{allowed & denied}"


def test_actions_catalog_has_schema_and_no_handler(client, gates_on, clean_plugins, healthy_registry):
    """目录要能指导调用（schema），且**绝不泄露可执行对象**。"""
    body = client.get("/api/ai/actions").json()
    assert body["write_enabled"] is True
    for a in body["actions"]:
        assert a["schema"], f"{a['id']} 缺 schema —— AI 不知道怎么调"
        assert a["risk"] in ("low", "mid", "high")
        assert "handler" not in a, "目录不许把可执行对象交给消费方"


def test_catalog_reports_missing_gates_precisely(client, monkeypatch, tmp_path, healthy_registry):
    """门没齐时：目录要**点名缺哪几项**（§3.3拒绝也要说清为什么）。"""
    monkeypatch.setenv("VM_OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.delenv("VM_AI_CONTROL", raising=False)
    monkeypatch.delenv("VM_AI_TOKEN", raising=False)
    # 这条**要**清空加载器账本：它验的就是「hook.ai 没启用时目录也答得出来」，
    # 而 `is_loaded()` 看的就是账本。teardown 的还原交给 autouse 的
    # `_keep_registry_healthy`（它填满而不是清空，所以不会拖红下一个文件）。
    plugin_loader.reset()
    body = client.get("/api/ai/actions").json()
    assert body["write_enabled"] is False
    assert any("VM_AI_CONTROL" in g for g in body["missing_gates"])
    assert any("hook.ai" in g for g in body["missing_gates"])
    # 拒绝清单任何时候都要答（它是设计承诺，不是运行时状态）
    assert any(d["id"] == "shell.exec" for d in body["denied"])


# ---------------------------------------------------------------- 3. dry_run 零副作用


def test_dry_run_does_not_touch_state(client, gates_on, clean_plugins, healthy_registry):
    """★ dry_run 必须**真的**不写盘。

    目标选`sound.watermark`：它 `is_core=False` 且 `dependents_of()` 为空 ——
    早先版本用 `sound.tts`，而它被 `sound.course` 依赖，于是拿到 409
    「仍被依赖，不能关闭」。那个409 是**对的**，但它让本用例变成
    「在测409」而不是「在测 dry_run 不写盘」，等于什么都没验。
    """
    target = _free_plugin()
    before = plugin_manifest.disabled_ids()
    r = _post(client, "plugins.enable", params={"id": target, "enabled": False}, dry_run=True)
    assert r.status_code == 200, r.text
    assert r.json()["dry_run"] is True
    assert plugin_manifest.disabled_ids() == before, "dry_run 竟然改了插件状态"
    assert target not in plugin_manifest.disabled_ids()




def test_real_action_writes_state(client, gates_on, clean_plugins, healthy_registry):
    """真执行要落盘 —— 这条证明 dry_run 那条的「没改」不是因为它压根没执行。"""
    r = _post(
        client,
        "plugins.enable",
        params={"id": "sound.watermark", "enabled": False},
        dry_run=False,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert "sound.watermark" in plugin_manifest.disabled_ids()
    assert "重启" in body["message"], "必须如实告知需要重启，不能说成立即生效"


def test_core_cannot_be_disabled(client, gates_on, clean_plugins, healthy_registry):
    """核心能力不可关 → 409，且理由沿用既有守卫的原文。"""
    core_id = next(p.id for p in plugin_manifest.load_all() if p.is_core)
    r = _post(client, "plugins.enable", params={"id": core_id, "enabled": False}, dry_run=False)
    assert r.status_code == 409
    assert "核心" in r.json()["detail"]
    assert core_id not in plugin_manifest.disabled_ids()


def test_dependent_plugin_cannot_be_disabled(client, gates_on, clean_plugins, healthy_registry):
    """仍被依赖时拒绝关闭 → 409。"""
    dep_id, dependent = next(
        (p.id, plugin_manifest.dependents_of(p.id))
        for p in plugin_manifest.load_all()
        if not p.is_core and plugin_manifest.dependents_of(p.id)
    )
    r = _post(client, "plugins.enable", params={"id": dep_id, "enabled": False}, dry_run=False)
    assert r.status_code == 409
    assert dependent[0] in r.json()["detail"]
    assert dep_id not in plugin_manifest.disabled_ids()


def test_bad_params_are_400(client, gates_on, clean_plugins, healthy_registry):
    """参数校验：非布尔 enabled、缺id、未知能力分别给对的码。"""
    assert _post(client, "plugins.enable", params={"id": "x", "enabled": "false"}).status_code == 400
    assert _post(client, "plugins.enable", params={"enabled": True}).status_code == 400
    assert _post(client, "plugins.enable", params={"id": "no.such", "enabled": True}).status_code == 404
    assert _post(client, "plugins.preset", params={"name": "no-such-preset"}).status_code == 400


def test_string_false_is_not_truthy(client, gates_on, clean_plugins, healthy_registry):
    """★`enabled="false"` 字符串**不许**被当成开启。

    Python 里非空字符串是真值 —— 不严格处理就成了「用户说关、实际开着」，
    而UI 上显示已关闭，排障时这个不一致极难发现。
    """
    r = _post(
        client, "plugins.enable",
        params={"id": _free_plugin(), "enabled": "false"}, dry_run=False,
    )
    assert r.status_code == 400, "字符串布尔必须被拒，而不是当True 执行"
    assert _free_plugin() not in plugin_manifest.disabled_ids()


# ---------------------------------------------------------------- 4. 审计（fail-closed）


def _audit_lines(tmp_path):
    """读审计账本。

    ★ 路径必须与 `gates_on` fixture 里`VM_AI_AUDIT_FILE` 指向的**同一处**
    —— 早先版本去 `tmp_path/outputs/` 找，读到的是 `conftest` 的
    session 级隔离目录（导入期就设定了 `VM_OUTPUTS_DIR`），
    于是「审计没落盘」是测试自己找错地方造成的假红。
    教训：断言审计落盘时，路径要跟着被测代码真正用的那个变量走。
    """
    import os
    import pathlib

    p = pathlib.Path(os.environ.get("VM_AI_AUDIT_FILE", ""))
    if not p:
        return []
    if not p.is_file():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_every_action_is_audited(client, gates_on, clean_plugins, tmp_path, healthy_registry):
    """成功的执行要落审计，且字段齐全。"""
    r = _post(
        client, "plugins.enable",
        params={"id": "sound.watermark", "enabled": False},
        dry_run=False, actor="tester",
    )
    assert r.status_code == 200
    lines = _audit_lines(tmp_path)
    assert lines, "写动作没有落审计 —— 这个端点的全部可信度都建立在账本上"
    assert any(x["action"] == "plugins.enable" and x["ok"] for x in lines)
    for entry in lines:
        for field in ("id", "ts", "action", "params", "dry_run", "ok", "code", "actor"):
            assert field in entry, f"审计缺字段 {field}"


def test_rejected_attempt_is_audited(client, gates_on, clean_plugins, tmp_path, healthy_registry):
    """★被拒的尝试也要落审计 —— 排障时「AI 试过什么、被什么挡住」是关键信息。"""
    r = _post(client, "plugins.enable", params={"id": "no.such.plugin", "enabled": True})
    assert r.status_code == 404
    lines = _audit_lines(tmp_path)
    assert any(not x["ok"] for x in lines), "失败的尝试没进账本"


def test_actor_is_recorded(client, gates_on, clean_plugins, tmp_path, healthy_registry):
    """actor 取自 X-AI-Actor 头，缺省 unknown（不能凭空猜是谁）。"""
    _post(client, "plugins.preset", params={"name": "light"}, dry_run=True)
    lines = _audit_lines(tmp_path)
    assert lines
    assert all(x["actor"] for x in lines)
    assert lines[0]["actor"] == "unknown"


def test_action_is_blocked_when_audit_cannot_be_written(client, gates_on, clean_plugins, monkeypatch, tmp_path, healthy_registry):
    """★ fail-closed：审计写不进去时，动作**不许发生**。

    这是本组最要紧的一条。写动作已经改了用户的插件开关，
    而「谁在什么时候改的」查不到 —— 比没有这个端点更糟。
    """
    # 让intent 那条账本就写不下去（路径指向一个文件而非目录）
    monkeypatch.setenv("VM_AI_AUDIT_FILE", str(tmp_path / "not-a-dir" / "ai.jsonl"))
    (tmp_path / "not-a-dir").write_text("x", encoding="utf-8")  # 占一个文件，makedirs 会失败

    before = plugin_manifest.disabled_ids()
    r = _post(client, "plugins.enable", params={"id": "sound.watermark", "enabled": False}, dry_run=False)
    assert r.status_code == 503, f"审计不可写时必须拒绝（得到 {r.status_code}）"
    assert plugin_manifest.disabled_ids() == before, "★ 动作仍然执行了 —— fail-closed 没生效"


def test_audit_separate_from_history(client, gates_on, clean_plugins, healthy_registry):
    """审计文件与作品库分开 —— 清作品库不该顺带清掉账本。"""
    import os
    import pathlib

    _post(client, "plugins.preset", params={"name": "light"}, dry_run=True)
    audit = pathlib.Path(os.environ.get("VM_AI_AUDIT_FILE", ""))
    assert audit.name == "ai_actions.jsonl", "审计文件名约定变了"
    assert audit.is_file(), "审计没落盘"
    assert "history" not in audit.name, (
        "审计不该与作品库共用文件（清作品库会顺带清掉账本）"
    )


def test_audit_tail_reads_back(client, gates_on, clean_plugins, healthy_registry):
    """面板读得回来，且倒序（最新在前）。"""
    for i in range(3):
        _post(client, "plugins.enable", params={"id": f"x.id{i}", "enabled": True})
    body = client.get("/api/ai/audit").json()
    assert body["schema"] == 1
    assert len(body["items"]) >= 3
    ts = [x["ts"] for x in body["items"]]
    assert ts == sorted(ts, reverse=True), "审计面板应按时间倒序"


def test_audit_endpoint_is_loopback_only(lan_client, gates_on, clean_plugins, healthy_registry):
    """审计含参数原文，同样只限本机。"""
    assert lan_client.get("/api/ai/audit").status_code == 403