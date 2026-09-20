"""路由遮蔽守护：任意两条 router 路由不得互相遮蔽。

为什么需要这条测试
------------------
FastAPI 按注册顺序匹配路由，两条路由互相遮蔽时**先注册的那个接** —— 所以
「挂载顺序」原则上是**行为**，`server.py` 里原来写着「顺序有语义，别动」。

2026-09-20 第 3 步把这句话量了一遍（139 条真实路由）：

| 检查项 | 结果 |
|---|---|
| 完全相同的 `(method, path)` | 0 条 |
| router 之间互相遮蔽 | **0 条** |
| 65 条遮蔽关系 | 全部是 router vs SPA 兜底 `/{full_path:path}`，且全部「先注册、无害」 |
| 低优先级路由 / websocket / Mount | 0 个 |

所以那次把「历史交错序」换成「清单 `order`」是**行为等价**的（第 3 步的迁移依据）。
但「顺序是行为」这句话本身仍然成立 —— 它守的是**将来**。与其冻结一份对今天的
行为没有任何影响、却会在每次加插件时逼人做一次无意义取舍的历史顺序，不如直接
断言真正在意的那件事：

    **不许出现两条互相遮蔽的 router 路由。**

一旦出现，本用例点名是哪两条路径、分别属于哪个插件，然后由人决定谁优先 ——
那才是一个真需要人判断的决策。

比「路径字符串相同」更隐蔽的一类
--------------------------------
`/api/voices/{name}` 与 `/api/voices/market` 的 **path 字符串并不相同**，只比字符串的
检测会漏掉它；但请求 `/api/voices/market` 确实会被先注册的那条吃掉。所以这里把每条
路径编译成正则，再拿**具体化样本路径**去撞其它路由。
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

pytest.importorskip("fastapi", reason="本用例要 import 各 router 模块")

import plugin_loader  # noqa: E402
import plugin_manifest  # noqa: E402

#: 这两个方法由 Starlette 自动补，不是真实端点，不该参与遮蔽判定。
_SKIP_METHODS = frozenset({"HEAD", "OPTIONS"})


def _routes() -> list[tuple[str, str, str, str]]:
    """`[(plugin_id, module, method, path), ...]`，顺序即清单挂载顺序。

    刻意**不走 `import server`**：那会触发 warmup 等启动副作用，而这里只需要
    router 对象本身。挂载顺序取自 `plugin_manifest.mount_plan()` —— 与 `server.py`
    实际调用的是同一个函数，所以两者不会漂。
    """
    out: list[tuple[str, str, str, str]] = []
    not_loaded: list[str] = []
    empty: list[str] = []
    for plugin_id, module in plugin_manifest.mount_plan():
        router = plugin_loader.load_router(module)
        if router is None:
            # 静默跳过会在守护上留一个洞（那个模块的路由没被检查）。
            # 收集起来统一报，并指向真正该修的那条用例。
            not_loaded.append(module)
            continue
        before = len(out)
        for route in router.routes:
            for method in sorted(getattr(route, "methods", None) or []):
                if method in _SKIP_METHODS:
                    continue
                # ⚠️ 这里**不要**再拼 `router.prefix`。FastAPI 的 `add_api_route` 已经
                # 把 prefix 烘进 `route.path` 了：`system_api.prefix` 是 `/api`，
                # 而它的 `route.path` 就是 `/api/health`。再拼一次会得到
                # `/api/api/health` —— 而这个错误**不会**让任何用例变红，
                # 只会让下面两条守护**静默失效**（2026-09-20 变异测试抓到的，
                # 见 `docs/犯错指南.md` §8.19）。对账见
                # `test_rebuilt_route_table_matches_the_app`。
                out.append((plugin_id, module, method, route.path))
        if len(out) == before:
            empty.append(module)
    assert not not_loaded, (
        f"这些模块没加载成功，本用例无法检查它们的路由：{not_loaded}\n"
        "（加载失败本身由 test_plugin_loader.py::test_healthy_module_list_handles_"
        "every_router_module 负责报，这里只是拒绝在信息不全的情况下装绿）"
    )
    assert not empty, f"这些模块一个路由都没贡献出来（重建逻辑多半漏了）：{empty}"
    return out


def _pattern(path: str) -> re.Pattern[str]:
    """FastAPI 路径 → 正则。`{p}` 匹配一段（不含 `/`），`{p:path}` 匹配剩余全部。"""
    parts: list[str] = []
    for seg in re.split(r"(\{[^}]*\})", path):
        if not seg.startswith("{"):
            parts.append(re.escape(seg))
            continue
        conv = seg[1:-1].split(":", 1)
        parts.append(".+" if len(conv) > 1 and conv[1] == "path" else "[^/]+")
    return re.compile("^" + "".join(parts) + "$")


def _sample(path: str) -> str:
    """把参数段换成占位值，得到一个**具体**路径，用来撞别的路由的正则。"""
    return re.sub(r"\{[^}]*\}", "ZZZ", path)


def test_rebuilt_route_table_matches_the_app():
    """★ 地基：本模块重建的路由表必须与 `server.app` 上**真实挂载**的逐条一致。

    下面两条守护建立在「从清单 + router 对象重建路由表」之上，而**重建是会错的** ——
    2026-09-20 实测踩到过：多拼了一次 `router.prefix`（FastAPI 早已把 prefix 烘进
    `route.path`），得到 `/api/api/health`。后果不是红，而是**两条守护全部静默失效**
    （变异测试注入真重叠后仍全绿）。所以拿真源对一次账：重建错了，这条先红。

    只在 `_IncludedRouter` 包装里取路由，天然排除了 FastAPI 自带的
    `/openapi.json` `/docs` 与 SPA 兜底（它们不是 router 挂上来的）。
    """
    import server
    from fastapi.routing import APIRoute

    mounted: set[tuple[str, str]] = set()
    for wrapper in server.app.routes:
        inner = getattr(wrapper, "original_router", None)
        if inner is None or not hasattr(inner, "routes"):
            continue
        for route in inner.routes:
            if not isinstance(route, APIRoute):
                continue
            mounted.update((m, route.path) for m in (route.methods or []) if m not in _SKIP_METHODS)

    rebuilt = {(m, p) for _, _, m, p in _routes()}
    assert rebuilt, "重建出来是空的 —— 那下面两条守护都是空的"
    assert not rebuilt - mounted, (
        f"重建出了 app 上并不存在的路由（多半是前缀算重了）：{sorted(rebuilt - mounted)[:5]}"
    )
    assert not mounted - rebuilt, (
        f"app 上有、重建里没有的路由（多半是漏了某个 router）：{sorted(mounted - rebuilt)[:5]}"
    )


def test_no_duplicate_method_and_path_across_routers():
    """两条 router 不得声明**完全相同**的 `(method, path)`。

    比下面那条正则版更宽松，但报错信息直白得多 —— 真踩到时先看这条。
    """
    routes = _routes()
    counts = Counter((m, p) for _, _, m, p in routes)
    dupes = sorted(k for k, v in counts.items() if v > 1)
    if not dupes:
        return
    lines = []
    for method, path in dupes:
        who = [f"{mod}（{pid}）" for pid, mod, m, p in routes if (m, p) == (method, path)]
        lines.append(f"  {method:6} {path}\n        " + "、".join(who))
    pytest.fail(
        "同一个 (method, path) 被多个 router 声明 —— 先注册的会吃掉其余：\n"
        + "\n".join(lines)
        + "\n\n要么改路径，要么把「谁该优先」写进清单（挂载顺序 = 插件 order）。"
    )


def test_no_router_route_shadows_another():
    """★ 核心守护：不许出现两条互相遮蔽的 router 路由（含参数路由吃静态路由）。

    这条测试就是「注册顺序是行为」这句话的**唯一守卫**。它绿 = 挂载顺序对行为
    没有任何影响；它红 = 出现了真需要人判断的取舍，而不是可以随手重排的排版。
    """
    routes = _routes()
    pats = [_pattern(p) for _, _, _, p in routes]
    samples = [_sample(p) for _, _, _, p in routes]

    conflicts: list[tuple[int, int, str]] = []
    for i in range(len(routes)):
        for j in range(i + 1, len(routes)):
            # 方法不同 = 不会抢同一个请求
            if routes[i][2] != routes[j][2]:
                continue
            # 找一条**同时匹配两条模式**的具体路径作为证据
            if pats[i].match(samples[j]):
                conflicts.append((i, j, samples[j]))
            elif pats[j].match(samples[i]):
                conflicts.append((i, j, samples[i]))

    if not conflicts:
        return

    lines = [f"发现 {len(conflicts)} 对互相遮蔽的路由（先注册的会接走请求）：", ""]
    for winner, loser, witness in conflicts:
        for tag, idx in (("先注册", winner), ("后注册", loser)):
            pid, mod, method, path = routes[idx]
            lines.append(f"  #{idx:<4}{tag}  {method:6} {path}")
            lines.append(f"              {mod}（插件 {pid}）")
        lines.append(f"        → 具体路径 {witness} 同时匹配两者，由 #{winner} 接走")
        lines.append("")
    lines.append(
        "先想清楚谁该优先（改路径？还是接受这个顺序？），再决定怎么改 ——\n"
        "挂载顺序由清单的插件 `order` 决定，改它会影响所有插件。"
    )
    pytest.fail("\n".join(lines))


def test_spa_catch_all_is_mounted_after_every_router():
    """SPA 兜底 `/{full_path:path}` 必须挂在**所有 router 之后**。

    上面那条 65 个「router vs SPA 兜底」的遮蔽关系之所以无害，全靠这一条。
    它是唯一的万能匹配，一旦排到前面，每个 API 请求都会返回 index.html ——
    症状是「接口全挂了」，而不是「路由配错了」，很难往回查。
    """
    import server
    from fastapi.routing import APIRoute

    spa_idx = [
        i
        for i, r in enumerate(server.app.routes)
        if isinstance(r, APIRoute) and r.path == "/{full_path:path}"
    ]
    if not spa_idx:
        pytest.skip("没有 web_dist（前端未构建），SPA 兜底不存在")
    assert len(spa_idx) == 1, f"SPA 兜底出现了 {len(spa_idx)} 次"
    assert spa_idx[0] == len(server.app.routes) - 1, (
        "SPA 兜底必须排在最后一条，否则它会吃掉排在它后面的所有 API 路由"
    )
