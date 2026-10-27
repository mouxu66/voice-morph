"""`/api/ai/*`（AI 调试接口，第一期只读）的测试 —— 2026-10-05 新增。

这个端点的存在理由是「用户报障时，AI 能看到应用内部状态」（见
`docs/交接-AI必读.md`）。它退化后的症状**大多不报错**：

  · 报告不全 → AI 拿着半份信息给出错误结论（用户还以为没救了）；
  · 报告漏脱敏 → 用户的私密文字/路径被交出去（事后收不回）；
  · 鉴权松了 → 同一份含路径与日志的快照被局域网里任何人读到。

所以本文件的四组断言都按「退化后不报错」的判据设计：

  1. **来源对账**：快照里的 `diagnose` 等段必须来自既有函数（不重算），
     `plugins.loaders` 的账本必须与 `plugin_loader.results()` 双向一致。
  2. **鉴权矩阵**：回环放行、LAN 403、非法 format 400 —— 逐个打 HTTP 验（§8.43
     的教训：只从函数层调过的守卫等于没有）。
  3. **脱敏是负面断言**：构造含 `input_text`、含主目录路径的历史与日志，
     断言这些字符串**不出现在响应里**（只断言"该在的在"会假绿）。
  4. **拒绝清单是声明式**：表外动作不可达靠默认拒绝，所以这里断言
     `wechat.send_*` 既不在 actions 里、也没有任何可执行入口。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import ai_api
import plugin_loader
import plugin_manifest
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    import server

    return TestClient(server.app, raise_server_exceptions=False)


@pytest.fixture()
def lan_client():
    """模拟局域网来源的 TestClient（`client=` 是 Starlette TestClient 的合成对端地址）。"""
    import server

    return TestClient(server.app, client=("192.168.1.50", 51234), raise_server_exceptions=False)


@pytest.fixture()
def healthy_registry():
    """把 registry 填成「全部加载成功」，让本文件对执行顺序免疫。

    为什么需要：`state_of()` 看的是 `plugin_loader` registry，而
    `test_plugin_loader.py` 的 autouse fixture 在每条用例结束时 `reset()` ——
    不填的话轮到本文件时所有能力都被判成「未注册 → broken」。
    手法与 `test_remote_api.py` 的 `healthy_registry` 一致（那是这个坑的第一次现身）。
    """
    for name in {m for _pid, m in plugin_manifest.mount_plan(include_disabled=True)}:
        plugin_loader.load_router(name)
    yield
    plugin_loader.reset()


# ---------------------------------------------------------------- 1. 形状与来源


def test_context_shape(client):
    r = client.get("/api/ai/context")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "schema", "generated_ts", "app", "health", "plugins", "diagnose",
        "audio", "warmup", "wechat", "history", "errors", "notes", "redaction",
    }
    assert body["schema"] == 1
    assert r.headers.get("cache-control") == "no-store", "诊断快照不该被缓存"


def test_reuses_existing_sections_instead_of_recomputing(client, monkeypatch):
    """★ `diagnose` / `health` 必须是**现有函数的结果原样透传**。

    在快照里重写一遍探测 = 造第二真相源：两边漂移时没人知道该信谁。
    这条用哨兵值钉住 —— 透传成立时哨兵原样出现；一旦有人在快照里"顺手重算"，
    哨兵就会被真实值顶掉，本条红。
    """
    sentinel_diag = {"all_ok": True, "cuda": False, "items": [], "skipped": [], "sentinel": 7}
    sentinel_health = {"status": "ok", "cuda": None, "sentinel": 8}
    monkeypatch.setattr(ai_api.system_api, "diagnose", lambda: sentinel_diag)
    monkeypatch.setattr(ai_api.system_api, "health", lambda: sentinel_health)

    body = client.get("/api/ai/context").json()
    assert body["diagnose"] == sentinel_diag
    assert body["health"] == sentinel_health


def test_plugins_ledger_matches_loader_results(client, healthy_registry):
    """`plugins.loaders` 与加载器账本**双向**一致（不是"都非空"这种恒真断言）。"""
    body = client.get("/api/ai/context").json()
    results = [r for r in plugin_loader.results() if r.purpose == plugin_loader.ROUTER_PURPOSE]
    ok_modules = sorted(r.module for r in results if r.ok)
    bad_modules = sorted(r.module for r in results if not r.ok)

    assert body["plugins"]["loaders"]["routers"] == len(results)
    assert body["plugins"]["loaders"]["loaded"] == len(ok_modules)
    assert sorted(b["module"] for b in body["plugins"]["loaders"]["broken"]) == bad_modules


def test_enabled_set_is_self_consistent(client, healthy_registry):
    """`enabled` / `disabled` / 每个 item 的 `enabled` 必须自洽（三份口径不许各算各的）。"""
    body = client.get("/api/ai/context").json()
    pl = body["plugins"]
    enabled, disabled = set(pl["enabled"]), set(pl["disabled"])
    assert enabled | disabled == set(plugin_manifest.by_id())
    assert enabled & disabled == set()
    for item in pl["items"]:
        assert item["enabled"] == (item["id"] in enabled), item["id"]
        # 用户关掉的不许被算成「坏掉的」（三态分离，见 plugin_manifest 模块注释）
        if item["id"] in disabled:
            assert item["state"] == plugin_manifest.STATE_DISABLED, item["id"]


def test_section_failure_degrades_with_reason(client, monkeypatch):
    """★ 采集某一段失败时：端点仍 200，该段为空，且 `notes` **点名**是哪个 section。

    这是「缺得最多时最该活着」的机器判据（同 /diagnose 与 /capabilities 的取舍）。
    """
    def _boom():
        raise RuntimeError("模拟采集失败")

    monkeypatch.setattr(ai_api.system_api, "diagnose", _boom)
    r = client.get("/api/ai/context")
    assert r.status_code == 200
    body = r.json()
    assert body["diagnose"] is None
    assert any(n["section"] == "diagnose" for n in body["notes"]), body["notes"]


# ---------------------------------------------------------------- 2. 鉴权矩阵


def test_loopback_is_allowed(client):
    assert client.get("/api/ai/context").status_code == 200
    assert client.get("/api/ai/actions").status_code == 200


def test_lan_is_rejected(lan_client):
    """局域网来源一律 403（含只读面）—— 比现有 API 更严，是刻意的（快照含路径与日志）。"""
    r = lan_client.get("/api/ai/context")
    assert r.status_code == 403
    assert "仅限本机" in r.json()["detail"]
    assert lan_client.get("/api/ai/actions").status_code == 403


def test_every_ai_endpoint_is_loopback_only(lan_client):
    """★ 不变量：**每一个** `/api/ai/*` 端点都必须拒 LAN —— 自动遍历，不靠人记得写。

    这条替代「每个端点手写 `_require_loopback()`」那种写法。手写的致命处：
    新增端点时忘了写 = 默认对 LAN 开放，而快照里含本机路径与日志尾部。
    本用例把已挂载的 `/api/ai/*` 路由全部拉出来逐个打一遍，
    所以**将来新增端点漏了保护也会红**（门禁的全部意义就在这里）。

    路径参数用占位符替换 —— 只验「来源被拒」，不验业务逻辑。

    ⚠ 遍历方式必须走 `original_router.routes`：本项目这个 FastAPI 版本里
    `app.include_router()` 塞进去的是 `_IncludedRouter` **包装器**，
    它自己的 `.path` 是空串、`.routes` 是 `None`（实测 38 个 router 挂载后
    `app.routes` 里38 条全是空 path）。早先版本直接遍历 `app.routes` 于是
    收集到 0 条 ——而 `assert paths` 那句让它「红」了，可红的理由是
    「没找到路由」而不是「LAN 被放行」，**差点把一个恒真断言当成门禁**。
    """
    import server

    paths: list[str] = []
    for route in server.app.routes:
        inner = getattr(route, "original_router", None)
        if inner is None:
            continue
        paths += [getattr(r, "path", "") for r in inner.routes]
    ai_paths = [p for p in paths if p.startswith("/api/ai/")]
    assert len(ai_paths) >= 3, f"只找到 {len(ai_paths)} 条 /ai/* 路由，遍历方式不对"
    for p in ai_paths:
        concrete = p.replace("{action_id}", "plugins.enable")
        # ★ 必须按端点自己的方法打：拿 GET 去打 POST 端点得到的是 405/404
        # （路由匹配上了但方法不对），而本用例断言的是 403 ——
        # 那种红是「测法错了」，不是「保护没生效」，等于白测一条。
        methods = _methods_of(server.app, p)
        assert methods, f"{concrete} 没找到任何方法"
        for method in methods:
            r = lan_client.request(method, concrete)
            # 403 = 回环守卫生效（目标）；404/405 = 被更早的守卫拦下（可接受，
            # 例如 hook.ai 关着时写路由压根不存在）。其它状态码 = 守卫漏了。
            assert r.status_code in (403, 404, 405), (
                f"{method} {concrete} 未经守卫就返回了 {r.status_code}"
            )


def _methods_of(app, path: str) -> list[str]:
    """取某条挂载路由上真实注册的方法。"""
    for route in app.routes:
        inner = getattr(route, "original_router", None)
        if inner is None:
            continue
        for r in inner.routes:
            if getattr(r, "path", "") == path:
                return sorted((getattr(r, "methods", None) or set()) - {"HEAD", "OPTIONS"})
    return []


def test_bad_format_is_400(client):
    assert client.get("/api/ai/context", params={"format": "xml"}).status_code == 400


def test_logs_zero_skips_log_section(client):
    body = client.get("/api/ai/context", params={"logs": 0}).json()
    assert body["errors"]["items"] == []
    assert "跳过" in body["errors"]["note"]


# ---------------------------------------------------------------- 3. 脱敏（负面断言）


def test_redaction_never_leaks_private_text_or_home(client, monkeypatch):
    """★ 私密文字与主目录路径**不许出现**在响应里。

    用 `json.dumps` 全文搜（对象里藏着、嵌套在 params 里都算），而不是逐字段断言 ——
    逐字段断言漏一个新加的字段就假绿，而这类泄漏是收不回来的。
    """
    secret_text = "这是一句绝对不该交出去的用户私密文字"
    home = str(Path.home())
    fake_items = [
        {
            "id": "1_x",
            "ts": 1,
            "kind": "tts",
            "voice_id": "kangaroo",
            "duration_s": 1.2,
            "input_text": secret_text,
            "params": {"text": secret_text},
            "wav": f"{home}\\outputs\\a.wav",
            "url": "/api/media/outputs/a.wav",
            "starred": False,
            "tags": [],
        }
    ]
    monkeypatch.setattr(
        ai_api.history, "query", lambda **kw: {"items": fake_items, "total": 1, "limit": 20, "offset": 0}
    )

    body = client.get("/api/ai/context").json()
    raw = json.dumps(body, ensure_ascii=False)
    assert secret_text not in raw, "input_text/params 里的用户文字被原样带出去了"
    assert home not in raw, "用户主目录没被替换成 %USERPROFILE%"
    # 注意口径：`input_text` 这几个字会出现在 `redaction.rules` 的说明文字里（那是
    # 规则描述，不是数据），所以只对 history 段断言字段本身没被带出。
    assert "input_text" not in json.dumps(body["history"], ensure_ascii=False)

    # 结构化字段该在的还在（不是"什么都没返回"式假绿）
    assert body["history"]["items"][0]["id"] == "1_x"
    assert body["history"]["items"][0]["duration_s"] == 1.2


def test_redaction_applies_to_log_lines(client, monkeypatch, tmp_path):
    """日志行同样要脱敏 + 截断，且只返回**疑似错误**行。"""
    home = str(Path.home())
    log = tmp_path / "backend.log"
    log.write_text(
        f"[plugins] 一切正常的一行\n[plugin_loader] 不可用: {home}\\x.py —— 缺依赖\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ai_api, "_log_path", lambda: log)

    body = client.get("/api/ai/context").json()
    joined = "\n".join(body["errors"]["items"])
    assert home not in joined
    assert "%USERPROFILE%" in joined
    assert "一切正常的一行" not in joined, "疑似错误筛选失效（把正常行也带上了）"


# ---------------------------------------------------------------- 3b. ★ P0 脱敏回归


def _write_log(monkeypatch, tmp_path, line: str) -> None:
    log = tmp_path / "backend.log"
    log.write_text(line + "\n", encoding="utf-8")
    monkeypatch.setattr(ai_api, "_log_path", lambda: log)


def test_spoken_asr_text_never_reaches_report(client, monkeypatch, tmp_path):
    """★ P0 回归：日志里 ASR 识别出的**用户原话**不许进报告。

    泄漏源是 `cascade_stream.py` 的
        print(f"[live-asr] 「{text}」 ({...}s)")
    —— 用户原话进 stdout → backend.log → 本报告。而这行**恰好**很容易命中
    `_LOG_SUSPECT`（用户抱怨的「这局失败了」「不行」自带「失败/错误」），
    于是用户点一次「复制诊断报告」就把刚说的银行卡号发给了第三方 AI。

    本条断言用**全文搜**（含号码与原话片段），不是逐字段断言：逐字段会假绿。
    """
    _write_log(
        monkeypatch,
        tmp_path,
        "[live-asr] 「我银行卡密码是 6222020200112233 这局失败了帮我改一下」 (1.2s)",
    )
    body = client.get("/api/ai/context").json()
    raw = json.dumps(body, ensure_ascii=False)
    assert "6222020200112233" not in raw, "ASR 用户原话里的银行卡号被原样带出去了"
    assert "银行卡密码" not in raw
    # 也不能「为了安全」把整行丢掉 —— 引号与耗时是诊断信号（发生过一次转写 + 耗时）
    joined = "\n".join(body["errors"]["items"])
    assert "<用户原话已省略>" in joined, "占位符缺失：要么没脱敏，要么整行被吞掉"
    assert "1.2s" in joined, "行尾耗时被误伤：脱敏不该毁掉诊断信息"


def test_id_card_and_phone_in_log_are_redacted(client, monkeypatch, tmp_path):
    """号码类兜底（不依赖引号）：异常文本里直接夹带号码同样要隐藏。"""
    _write_log(
        monkeypatch,
        tmp_path,
        "[analyze] FAIL ref.wav: OSError 出错了 13800138000 身份证 110101199001011234",
    )
    body = client.get("/api/ai/context").json()
    raw = json.dumps(body, ensure_ascii=False)
    assert "13800138000" not in raw
    assert "110101199001011234" not in raw
    assert "<疑似号码已隐藏>" in raw


def test_number_redaction_does_not_eat_diagnostics(client, monkeypatch, tmp_path):
    """★ 反向守卫：号码正则**不许误伤**正常诊断数字。

    没有这条，一个「宁滥勿缺」的号码正则会把耗时(0.35)、端口、音色编号全抹成
    星号，报告变得无从解读 —— 而这类退化同样不报错。
    """
    _write_log(
        monkeypatch,
        tmp_path,
        "[cascade] 端到端滞后 0.35s (avg 0.30) 失败重试 id=1_abc1234 port=8080 took=1.5s",
    )
    body = client.get("/api/ai/context").json()
    joined = "\n".join(body["errors"]["items"])
    for keep in ("0.35", "0.30", "1_abc1234", "8080", "1.5s"):
        assert keep in joined, f"正常诊断数字 {keep} 被误抹了（正则边界写太宽）"


def test_number_redaction_never_leaves_partial_digits(client, monkeypatch, tmp_path):
    """★ 正则边界（`(?<!\\d)` / `(?!\\d)`）真正的作用：防止**部分**吞噬。

    变异验证时实测的差异 —— 去掉边界后，一串 13 位数字会被从中间截断、
    尾部数字**原样留下**：

        '1380013800012' 有边界 → 原样（不是号码）
                     无边界 → '<X>12'  ← 尾巴 12 泄漏，且报告里出现无意义残缺号

    半截号码比完整号码更糟：它看起来像被「部分识别」过的真实号码，
    排障时会把人带偏。所以这条钉住「要么完整隐藏、要么原样保留」，
    不接受「吃掉一半」。
    """
    _write_log(
        monkeypatch,
        tmp_path,
        "[cascade] 块处理失败(3): 13 位串 1380013800012 出错了；18位 110101199001011234567 也出错",
    )
    body = client.get("/api/ai/context").json()
    joined = "\n".join(body["errors"]["items"])
    # 非号码的长串：要么原样保留，要么整段隐藏 —— 不接受残缺的尾巴
    assert "1380013800012" in joined or "<疑似号码已隐藏>" in joined
    assert not re.search(r"<疑似号码已隐藏>12", joined), (
        "号码被部分吞噬：占位符后面还留着数字尾巴 —— 正则的左右边界写丢了"
    )


def test_redaction_runs_before_truncation(client, monkeypatch, tmp_path):
    """★ 顺序不变量：脱敏在截断**之前**。

    反过来的话，200 字符的截断会把「」拦腰切断（用户原话通常很长），
    闭合引号没了 → 引号规则匹配不上 → 报告照样带出半句原话。
    这条用「原话长度 > 截断上限」的真实边界钉住，不靠注释说明。
    """
    spoken = "这是一句很长的用户原话" * 30  # > 200 字符，必被截断
    _write_log(monkeypatch, tmp_path, f"[live-asr] 「{spoken} 失败了」 (2.0s)")
    body = client.get("/api/ai/context").json()
    raw = json.dumps(body, ensure_ascii=False)
    assert "很长的用户原话" not in raw, "先截断再脱敏：引号被切断 → 原话泄漏"
    assert len(body["errors"]["items"][0]) <= 200, "截断上限失效"


# ---------------------------------------------------------------- 4. 拒绝清单（声明式）


def test_actions_are_declarative_denials(client):
    body = client.get("/api/ai/actions").json()
    assert body["schema"] == 1
    assert body["write_enabled"] is False
    assert body["write_reason"], "写能力关闭时必须说明原因"
    assert body["actions"] == []
    # ★ 断言「不存在」同样要写：send 类动作不该出现在任何可执行入口
    assert not any(a.get("id", "").startswith("wechat.send") for a in body["actions"])
    denied = {d["id"] for d in body["denied"]}
    for required in ("shell.exec", "wechat.send_text", "wechat.send_voice", "voices.delete"):
        assert required in denied, f"拒绝清单少了 {required}"
    assert all(d["reason"] for d in body["denied"]), "拒绝条目必须带理由（不许静默拒绝）"


# ---------------------------------------------------------------- 5. 诚实性：能力关掉要说清


def test_disabled_optional_sections_say_why(client, monkeypatch):
    """`hook.wechat` 关着时：wechat 段如实报关，且 notes 说明 —— 不 404 不 500。

    这条同时钉住插件化的边界：关掉的能力**不 import**（`wechat_voice` 不加载），
    只报「它关着」。
    """
    # ⚠ 必须用「standard 预设的禁用集」模拟，而不是简单地加两个 id：
    # `enabled_ids()` 有复活闭包（启用中的插件 requires 一个被关的 → 那个会复活），
    # 而测试环境（`VM_PLUGIN_SEED=0`，conftest 刻意不播种）下默认是**全开** ——
    # 只加两个 id 会让 hook.remote 仍在启用集里，从而把 hook.wechat 复活，
    # 这条用例就测不到「关着」的分支了（第一版就是这么写错的）。
    off = {
        p.id
        for p in plugin_manifest.load_all()
        if p.id not in plugin_manifest.preset_ids("standard")
    }
    assert {"hook.wechat", "sound.tts"} <= off, "前提失效：standard 应当不含这两项"
    monkeypatch.setattr(plugin_manifest, "disabled_ids", lambda: set(off))
    body = client.get("/api/ai/context").json()
    assert "hook.wechat" not in body["plugins"]["enabled"], "前提失效：复活闭包把它带回来了"
    assert body["wechat"]["enabled"] is False
    assert body["warmup"]["enabled"] is False
    sections = {n["section"] for n in body["notes"]}
    assert {"wechat", "warmup"} <= sections
    assert "wechat_voice" not in sys.modules or "hook.wechat" not in body["plugins"]["enabled"]


def test_markdown_report_contains_redaction_and_header(client):
    r = client.get("/api/ai/context", params={"format": "md"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    text = r.text
    assert "变声工坊 · 诊断报告" in text
    assert "脱敏" in text
    assert "环境体检" in text
