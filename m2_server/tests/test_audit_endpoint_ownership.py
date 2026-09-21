"""tools/audit_endpoint_ownership.py 的单元测试 —— 「核心页裸渲染可关组件」的判据必须可信。

为什么这个工具值得单独钉住：它回答的是「**哪些核心页会点出 404**」。
核心页（`core.*`）的路由恒注册，所以用户永远能走进去；而页内如果裸渲染了可关插件
的面板，关掉那个插件后端点就没了，界面却还在 —— 用户点一下 404，且**完全看不出原因**
（"核心页怎么会坏？"）。2026-09-21 就是靠人肉 grep 才发现的音色库那个洞，
所以把它落成机器判据，必须有测试保证判据本身不退化。

三件事要钉：

1. **归属解析**：前端 `/ab/chain` ↔ 后端 `/api/ab/chain`。前端路径是**相对** API base 的
   （base 由 `BASE` 常量运行期补），比对时必须剥掉后端的 `/api` 前缀 ——
   否则 137 条路由一条都匹配不上，全报「未定位」，看起来像抽取器坏了。
2. **定位路径完整**：门控常常写在**中间层**（入口页 → VoicesPage → AbChainCard）。
   只报末端贡献者会得出「没人门控」的错误结论，把已修好的地方报成缺口。
3. **判定方向正确**：点过插件 id = 做了门控；链上一环都没点过 = 缺口。
   两个方向都要有用例（假红和假绿一样有害）。

⚠️ 真实代码会变（插件会加会删），所以**结构类断言只钉语义、不钉具体清单**；
    涉及具体文件的用例一律先判断前提是否还在，不在就 skip，不让它变成脆测试。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
WEB_SRC = ROOT / "web" / "src"


def _load(name: str):
    """按路径加载 tools/ 下的脚本（它们不是包，不能 `import`）。"""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ao():
    return _load("audit_endpoint_ownership")


# ------------------------------------------------------------ ① 路径归属解析

def test_api_prefix_is_stripped(ao):
    """前端路径不含 /api，后端含 —— 剥掉才能匹配上（否则全量假红）。"""
    routes = [("/api/ab/chain", "ab_chain")]
    assert ao.path_to_module("/ab/chain", routes) == "ab_chain"


def test_placeholder_becomes_wildcard(ao):
    """`{item_id}` 要当通配，而不是被 `re.escape` 后当成重复量词。"""
    routes = [("/api/history/{item_id}", "history_api")]
    assert ao.path_to_module("/history/abc123", routes) == "history_api"


def test_prefix_match_for_frontend_side_params(ao):
    """前端把参数拼在自己这边（`/ft/${id}` 截断成 `/ft/`）时也要认。"""
    routes = [("/api/ft/{voice_id}", "finetune")]
    assert ao.path_to_module("/ft", routes) == "finetune"


def test_query_string_is_ignored(ao):
    """带 query 的前端路径按 path 部分匹配。"""
    routes = [("/api/ft/status", "finetune")]
    assert ao.path_to_module("/ft/status?voice_id=x", routes) == "finetune"


def test_unmatched_returns_none(ao):
    """匹配不上要明确返回 None（调用方据此判「未定位」），不能瞎猜一个。"""
    routes = [("/api/ft/status", "finetune")]
    assert ao.path_to_module("/absolute/nonsense", routes) is None


# ------------------------------------------------------------ ② 前端函数抽取

def test_extract_covers_multiline_and_templates(ao):
    """跨行 jsonFetch / 模板串 / 三元都要抽到 —— 只扫单行会整条漏掉。"""
    funcs = ao.load_client_funcs()
    # 这几个历史上都是「抽不到 URL」的假红：跨行调用
    for name in ("ftCorpusRestore", "petSearch", "qcClips", "listHistory"):
        assert funcs.get(name), f"{name} 没抽到 URL —— 跨行抽取退化了吗？"


def test_url_builders_are_not_treated_as_endpoints(ao):
    """拼装函数（mediaUrl 等）不是端点，留着会变成永远抽不到 URL 的假「未定位」。"""
    funcs = ao.load_client_funcs()
    for name in ao.URL_BUILDERS:
        assert funcs.get(name) == [], f"{name} 应被清空（它在 URL_BUILDERS 里）"


def test_most_funcs_resolve(ao):
    """抽到的路径绝大多数要能定位到插件 —— 比例骤降说明匹配逻辑坏了。"""
    r = ao.analyze()
    assert r["funcs_total"] > 50, "client.ts 里函数数量骤降，抽取口径被改坏了？"
    ratio = r["funcs_resolved"] / r["funcs_total"]
    assert ratio > 0.85, (
        f"只有 {ratio:.0%} 的函数能定位到插件（{r['funcs_resolved']}/{r['funcs_total']}）。"
        f"未定位清单：{[d['fn'] for d in r['unknown_detail']][:10]}"
    )


# ------------------------------------------------------------ ③ 依赖图语义

def test_closure_includes_intermediate_files(ao):
    """★ 路径必须含中间层 —— 门控常写在中间层，少了它会误报缺口。

    这是个纯图上的用例：A → B → C，只有 C 直接调插件端点。
    从 A 出发的链必须是 {A, B, C}，不能只是 {A, C}。
    """
    direct = {"C.tsx": {"sound.x": {"f"}}}
    deps = {"A.tsx": {"B.tsx"}, "B.tsx": {"C.tsx"}}
    plugs, chain = ao.plugin_closure("A.tsx", direct, deps)
    assert plugs == {"sound.x": {"f"}}
    assert chain["sound.x"] == {"A.tsx", "B.tsx", "C.tsx"}, (
        f"依赖链少了中间层：{sorted(chain['sound.x'])} —— 门控写在中间层时会误报缺口"
    )


def test_closure_handles_cycle(ao):
    """互相 import 不能死循环（组件间循环引用真实存在）。"""
    direct = {"A.tsx": {"sound.x": {"f"}}}
    deps = {"A.tsx": {"B.tsx"}, "B.tsx": {"A.tsx"}}
    plugs, chain = ao.plugin_closure("A.tsx", direct, deps)
    assert plugs == {"sound.x": {"f"}}
    assert chain["sound.x"] == {"A.tsx", "B.tsx"}


def test_closure_merges_multiple_paths(ao):
    """同一插件经多条路径到达时，所有路径都要并进来。"""
    direct = {"C.tsx": {"sound.x": {"f"}}, "D.tsx": {"sound.x": {"g"}}}
    deps = {"A.tsx": {"B.tsx", "D.tsx"}, "B.tsx": {"C.tsx"}}
    _plugs, chain = ao.plugin_closure("A.tsx", direct, deps)
    assert chain["sound.x"] == {"A.tsx", "B.tsx", "C.tsx", "D.tsx"}


# ------------------------------------------------------------ ④ 判定方向

def _entry_fixture(ao, *, mentions: set[str], chain_files: set[str]):
    """构造一份最小 file_rows + entries，直接测 `find_ungated_core_entries`。"""
    rows = [
        {
            "file": "pages/X/index.tsx",
            "core": ["core.voices"],
            "gated": ["sound.x"],
            "funcs": {"sound.x": ["f"]},
            "chain": {"sound.x": sorted(chain_files)},
            "direct": False,
            "unresolved": [],
            "mixed": True,
        }
    ]
    entries = {"pages/X/index.tsx": "core.voices"}
    is_core = {"core.voices": True, "sound.x": False}
    src_mentions = {f: ({"sound.x"} if f in mentions else set()) for f in chain_files}
    return ao.find_ungated_core_entries(rows, entries, is_core, src_mentions, {})


def test_gate_on_chain_is_accepted(ao):
    """链上任一环点过插件 id → 视为已门控，不报。"""
    got = _entry_fixture(
        ao, mentions={"pages/X/index.tsx"}, chain_files={"pages/X/index.tsx", "a.tsx"}
    )
    assert got == [], f"有人点了 id 却仍报缺口（假红）：{got}"


def test_gate_in_intermediate_layer_is_accepted(ao):
    """★ 门控写在中层（真实场景：入口页干净，门控在 VoicesPage）不能被报成缺口。"""
    got = _entry_fixture(
        ao,
        mentions={"mid.tsx"},
        chain_files={"pages/X/index.tsx", "mid.tsx", "leaf.tsx"},
    )
    assert got == [], f"门控在中层却报缺口（假红）：{got}"


def test_no_mention_anywhere_is_flagged(ao):
    """★ 链上一环都没点过 id → 真缺口，必须报出来（假绿更危险）。"""
    got = _entry_fixture(
        ao,
        mentions=set(),
        chain_files={"pages/X/index.tsx", "mid.tsx", "leaf.tsx"},
    )
    assert len(got) == 1, f"没人为该插件门控，却没报缺口（假绿）：{got}"
    assert got[0]["gated"] == "sound.x"
    assert got[0]["entry"] == "pages/X/index.tsx"
    assert set(got[0]["chain"]) == {"pages/X/index.tsx", "mid.tsx", "leaf.tsx"}


def test_non_core_entry_is_not_flagged(ao):
    """非核心路由不需要页内门控 —— 整条路由会随插件一起消失，别误报。"""
    rows = [
        {
            "file": "pages/Y/index.tsx",
            "core": [],
            "gated": ["sound.y"],
            "funcs": {"sound.y": ["f"]},
            "chain": {"sound.y": ["pages/Y/index.tsx"]},
            "direct": True,
            "unresolved": [],
            "mixed": False,
        }
    ]
    got = ao.find_ungated_core_entries(
        rows,
        {"pages/Y/index.tsx": "sound.y"},
        {"sound.y": False},
        {},
        {},
    )
    assert got == []


# ------------------------------------------------------------ ⑤ 落到真仓库

def test_repo_route_entries_resolve(ao):
    """清单里的路由都应该解析到真实文件，否则「核心页」判定会对着不存在的文件做。"""
    entries = ao.load_route_entries()
    assert entries, "一条路由入口都没解析出来 —— pageKey 规则变了？"
    for f, pid in entries.items():
        assert (WEB_SRC / f).exists(), f"{pid} 的入口 {f} 不存在"
        assert f.endswith("index.tsx"), f"{f} 不符合 pages/<module>/index.tsx 规则"


def test_repo_has_no_ungated_core_entries(ao):
    """★ 真实仓库当前必须零缺口（这就是这次修的 bug）。

    红了的修法：在**核心路由入口页**用 `pluginVisible(catalog, "<插件id>")`
    把对应块条件渲染，或让下游组件自己门控。
    """
    r = ao.analyze()
    got = r["ungated_core_entries"]
    detail = "\n".join(f"  {u['entry']}  [{u['gated']}]  链: {' → '.join(u['chain'])}" for u in got)
    assert got == [], f"核心路由页裸渲染可关组件（关掉插件后用户会 404）：\n{detail}"


def test_check_mode_exit_code_matches_findings(ao):
    """`--check` 的退出码必须与判定一致 —— 门禁靠它，不能恒 0。

    用子进程跑真实入口：`main()` 里除了判定还打印报告，重新实现一遍只会测到我自己的
    副本。注意要传 `--no-cov` 之外的干净环境（这里直接调脚本，不经过 pytest 配置）。
    """
    import subprocess

    r = ao.analyze()
    expected = 0 if not r["ungated_core_entries"] and not r["unknown_funcs"] else 1
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "audit_endpoint_ownership.py"), "--check"],
        capture_output=True,
        text=True,
        # **必须显式钉 encoding**（同 test_githooks._run）：这个工具的报告里有 `⚠️`/`✓`
        # 等 GBK 之外的字，它自己会把 stdout reconfigure 成 UTF-8；父进程若按
        # locale(cp936) 解，就是 `UnicodeDecodeError: 'gbk' codec can't decode byte 0xaa`
        # —— 2026-09-21 实测：`tools/check.py`（全量）在 Windows 上恒红，`--fast` 却绿
        # （FAST_TESTS 里没有本文件），所以没人发现。
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
    )
    assert proc.returncode == expected, (
        f"--check 退出码 {proc.returncode} 与判定 {expected} 不一致\n{proc.stdout[-2000:]}"
    )
