#!/usr/bin/env python
"""端点归属清点：前端每个 API 函数 → 后端 router 模块 → 插件 id（含「可关/内核」）。

为什么要有这个：
  C 类「UI 按清单门控」是按 **page** 粒度做的，但门控的正确性依赖两件事——
  ① 每个页面知道自己渲染的组件**打的是哪个插件**的端点；
  ② 出现新的「核心页里嵌可关组件」时有人会发现。
  这两件事靠人记必错。2026-09-21 就踩到：`/voices` 是核心页所以整页没门控，
  可它渲染的 `AbChainCard` 打的是 `sound.audition`（可关）→ 关掉试音间后
  核心页点一下就 404。那次是靠人肉 grep 追出来的，追完还差点没落到机器上。

本脚本把「前端函数 → 插件」这条链算清楚，并给两类问题报警：
  · **[未定位]** 函数的 URL 没抽到，或路径在后端找不到定义 → 归属未知，门控无据可依
  · **[跨能力]** 某文件同时用到「内核」与「可关」两边的端点 → 该文件就是
    「核心页里嵌可关组件」的候选，必须显式想清楚要不要门控

用法：
    python tools/audit_endpoint_ownership.py            # 人看的报告
    python tools/audit_endpoint_ownership.py --check    # 门禁：有问题就 exit 1
    python tools/audit_endpoint_ownership.py --json     # 机器读

`--check` 用 `tools/endpoint_ownership_baseline.json` 里的**已知未定位**白名单做
非对称降级：已登记的不再报，**新出现的**一律报红。白名单只许缩小不许扩大 ——
条目消失（修好了）时脚本会提示删掉它。
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_SRC = os.path.join(ROOT, "web", "src")
M2 = os.path.join(ROOT, "m2_server")
BASELINE = os.path.join(ROOT, "tools", "endpoint_ownership_baseline.json")


# --------------------------------------------------------------- plugin.json

def load_plugins() -> tuple[dict[str, str], dict[str, bool], set[str]]:
    """→ (router 模块 → 插件 id, 插件 id → 是否 core, 全部插件 id)"""
    module2plugin: dict[str, str] = {}
    is_core: dict[str, bool] = {}
    all_ids: set[str] = set()
    for f in sorted(glob.glob(os.path.join(M2, "plugins", "*", "plugin.json"))):
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        pid = d["id"]
        all_ids.add(pid)
        is_core[pid] = pid.startswith("core.")
        for m in d.get("routers", []):
            # 首个声明者胜：一个 router 模块只该属一个插件，重复声明是清单出错
            module2plugin.setdefault(m, pid)
    return module2plugin, is_core, all_ids


# --------------------------------------------------------------- 路由入口

def load_route_entries() -> dict[str, str]:
    """清单里的路由 → 它的入口文件（相对 web/src）。

    入口文件的解析规则见 `web/src/lib/pluginRoutes.tsx` 的 `pageKey`：
    一个 `{module: "Voices"}` 路由对应 `pages/Voices/index.tsx`。这里必须跟它一致，
    否则「核心页有没有嵌可关组件」这条判定会对着错的文件做。
    """
    entries: dict[str, str] = {}
    for f in sorted(glob.glob(os.path.join(M2, "plugins", "*", "plugin.json"))):
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        pid = d["id"]
        for r in d.get("routes", []):
            mod = r.get("module")
            if not mod:
                continue
            for cand in (f"pages/{mod}/index.tsx", f"pages/{mod}/index.ts"):
                if os.path.exists(os.path.join(WEB_SRC, cand)):
                    entries[cand] = pid
                    break
    return entries


# --------------------------------------------------------------- 后端路由表

ROUTE_RE = re.compile(r'@router\.(?:get|post|put|delete|patch)\(\s*"([^"]+)"')

# 后端所有 router 的公共前缀。前端 client.ts 的路径是**相对**它的
# （base 由 `BASE` 常量运行期补），比对时必须剥掉，见 path_to_module 的说明①。
API_PREFIX = "/api"


def load_routes() -> list[tuple[str, str]]:
    """→ [(路径, 定义它的模块名)]，路径已含各自 APIRouter 的 prefix。"""
    m2 = os.path.join(M2, "*.py")
    py = os.path.join(M2, "*.py")
    out: list[tuple[str, str]] = []
    for f in sorted(glob.glob(py) + glob.glob(m2)):
        base = os.path.basename(f)[:-3]
        try:
            with open(f, encoding="utf-8") as fh:
                s = fh.read()
        except OSError:
            continue
        m = re.search(r'APIRouter\(\s*prefix\s*=\s*(["\'])(.*?)\1', s)
        prefix = m.group(2) if m else ""
        for p in ROUTE_RE.findall(s):
            out.append((prefix + p, base))
    # 去重（glob 两次可能取到同一文件）
    return sorted(set(out))


def path_to_module(url: str, routes: list[tuple[str, str]]) -> str | None:
    """前端相对路径 → 后端定义模块。`{param}` 当通配。

    ⚠️ 两个坑，都踩过（2026-09-21）：

    ① **前端路径不含 `/api` 前缀，后端路径含。** `client.ts` 里写的是 `/ab/chain`，
       而 `ab_chain.py` 的 `APIRouter(prefix="/api")` 让真实路径成为 `/api/ab/chain`。
       base 由 `BASE` 常量（`import.meta.env` 的 API base）在运行时补上。
       所以匹配时要把后端路径的 `/api` 剥掉再比 —— 否则 137 条路由**一条都匹配不上**，
       表现为「40 个端点全报未定位」，很容易误判成抽取器坏了（当时就是这么误判的）。

    ② 不能用 `re.escape(p)` 再手工把 `\\{` 还原成 `{`：`re.escape` 在较新 Python 里
       **不会**转义 `{`/`}`（它们不是正则元字符），于是 `.replace(r"\\{", "{")` 找不到
       目标，`{voice_id}` 原样留在模式里被当成**重复量词**解析。只对**非占位符片段**
       做转义，占位符整体替换成 `[^/]+`。

    ③ **前端路径可能是参数化路由的前缀。** 前端写
       `` jsonFetch(`/ft/${encodeURIComponent(id)}`) ``，抽出来（在 `${` 处截断）是
       `/ft/`；后端是 `@router.delete("/ft/{voice_id}")`。这时按「前端路径 == 后端路径」
       的精确匹配永远失败。所以除精确匹配外再补一层：把后端路由的**参数段截掉**后
       如果等于前端路径，也算匹配（前端只是把参数拼在了自己这边）。
    """
    u = url.split("?")[0]
    for p, base in routes:
        p = p.removeprefix(API_PREFIX)
        parts = re.split(r"(\{[^}]+\})", p)
        candidates = ["".join(
            r"[^/]+" if part.startswith("{") and part.endswith("}") else re.escape(part)
            for part in parts
        )]
        # 补：截断掉尾部的参数段（`/ft/{voice_id}` → `/ft/`），配合 ③
        if len(parts) > 1:
            head = "".join(
                part for part in parts
                if not (part.startswith("{") and part.endswith("}"))
            ).rstrip("/")
            if head:
                candidates.append(re.escape(head) + "/?")
        for pat in candidates:
            try:
                if re.fullmatch(pat, u):
                    return base
            except re.error:
                continue
    return None


# --------------------------------------------------------------- 前端函数抽取

# client.ts 里前端真正发起请求的写法（见 web/src/api/client.ts）：
#   jsonFetch<T>("/health")          jsonFetch("/health")      ← 无泛型也要认
#   jsonFetch(`/history/${id}`)                                ← 模板串
#   jsonFetch(qs ? `/history?${qs}` : "/history")              ← 三元，取两支
#   fetch(BASE + "/tts", ...)        fetch(BASE + `/x/${id}`)
#   fetch(`${BASE}/voicebank/...`)                             ← BASE 在模板串里
#   xhr.open("POST", BASE + "/upload/video")                   ← XHR
# 注意：文件里还有 `return `${BASE}/voicebank/...`` 这种**只拼 URL 不请求**的
# 辅助函数（voicePackUrl 等）。它们不是端点，抽出来会算成「未定位」噪声，
# 所以只在**有请求动作的那一行**上抽，不做整函数体扫描。
_JSONFETCH = re.compile(r'jsonFetch(?:<[^>]*>)?\(\s*(?:"([^"]+)"|`([^`]+)`)')
_FETCH = re.compile(r'fetch\(\s*(?:BASE\s*\+\s*)?(?:"([^"]+)"|`([^`]+)`)')
_FETCH_TPLBASE = re.compile(r'fetch\(\s*`([^`]+)`')
_XHR = re.compile(r'xhr\.open\(\s*"[A-Z]+"\s*,\s*(?:BASE\s*\+\s*)?(?:"([^"]+)"|`([^`]+)`)')

# 三元里的字面量备选支：`jsonFetch(qs ? `/history?${qs}` : "/history")` —— 上面那个
# 模板串分支会把 `/history?${qs}` 截成 `/history?`，但字面量那支 `"/history"` 得单独捞。
# 只在这个形态上补，避免把参数默认值、注释里的路径也扫进来。
_TERNARY_ALT = re.compile(r':\s*"(\s*/(?:[^"]*)?)"')

# 从 `BASE}/voicebank/x` 这类模板串里剥掉前缀，只留 /voicebank/x
_BASE_PREFIX = re.compile(r"^\$\{BASE\}|^BASE\s*\+\s*")

# 纯 URL **拼装**函数（不自己发请求，只是把路径拼出来给 <img>/<a href>/<audio src> 用）。
# 它们不是端点，归属无从谈起 —— 与其塞进白名单掩盖，不如显式分类：脚本跳过它们，
# `--check` 也不报。加新条目必须是真的"只拼不发"，别拿它当免检后门。
URL_BUILDERS = {
    "backendPrefix",  # 返回 API base 前缀本身
    "mediaUrl",       # 后端相对音频路径 → 可播放绝对地址
    "voicePackUrl",   # 音色包导出下载链接（给 <a href>）
}


def load_client_funcs() -> dict[str, list[str]]:
    """client.ts: 导出函数名 → 它打的 URL 列表。

    ⚠️ 必须**按函数体**扫，不能只扫单行。项目里大量存在这种跨行写法：

        return jsonFetch<FtRestoreResult>(
          `/ft/corpus_restore?voice_id=${encodeURIComponent(voiceId)}`, { method: "POST" });

    只逐行看的话，`jsonFetch(` 和它的 URL 分处两行，**整条漏掉** —— 2026-09-21
    就因此把 ftCorpusRestore / petSearch / qcClips / listHistory 误报成「抽不到 URL」。
    但也不能把整个函数体无脑扫：`voicePackUrl` 那种**只拼 URL 不发请求**的辅助函数
    会把 URL 混进来。折中：按函数切块，只在**含请求动作**（jsonFetch / fetch / xhr.open）
    的行上抽，且请求动作与 URL 允许跨行。
    """
    path = os.path.join(WEB_SRC, "api", "client.ts")
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    # 先切出每个导出函数的行范围。
    # ⚠️ `export const NAME =` 只收**函数值**（`(...)=>` / `function`），不收普通常量：
    # `export const BASE = backendPrefix() + "/api"`、`BACKEND_ORIGIN = ...` 这些是
    # 配置而非端点，收进来就变成两个永远抽不到 URL 的假「未定位」（2026-09-21 踩过）。
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = re.match(r"export (?:async )?function (\w+)", line)
        if m:
            starts.append((i, m.group(1)))
            continue
        m2 = re.match(r"export const (\w+)\s*(?::[^=]+)?=\s*(.*)", line)
        if m2 and re.match(r"\(?[\w\s,{}:\[\]|]*\)?\s*(?::[^=]*)?=>|async\b|function\b", m2.group(2)):
            starts.append((i, m2.group(1)))

    out: dict[str, list[str]] = {}
    for idx, (start, name) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        if name in URL_BUILDERS:
            out[name] = []
            continue
        urls: list[str] = []
        # 在块内滑窗：请求动作行 + 其后最多 3 行（够覆盖跨行参数，又不至于串到别的调用）
        for j in range(start, end):
            window = "\n".join(lines[j : min(j + 4, end)])
            if not any(k in window for k in ("jsonFetch", "fetch(", "xhr.open")):
                continue
            for rx in (_JSONFETCH, _FETCH, _FETCH_TPLBASE, _XHR, _TERNARY_ALT):
                for groups in rx.findall(window):
                    u = next((g for g in groups if g), "") if isinstance(groups, tuple) else groups
                    if not u:
                        continue
                    u = _BASE_PREFIX.sub("", u).split("${")[0].rstrip("`/ ")
                    # `/ft/${encodeURIComponent(id)}` 截断后是 `/ft/` —— 尾部空段要抹掉，
                    # 否则 `/ft/` 匹配不上后端的 `/ft/{voice_id}`（2026-09-21 踩过，
                    # 表现为 ftDelete / petDetail 两个明明归位正确的端点假红）。
                    u = u.rstrip("/") or "/"
                    # 前端把路径参数插在自己拼的路径里，抽出的是**前缀**：
                    # `/ft/` ← `/ft/{voice_id}`、`/pet-market/detail/` ← `/.../detail/{skin_id}`。
                    # 反过来补一层「前缀匹配」在这里做，交给 path_to_module 处理。
                    if u.startswith("/"):
                        urls.append(u)
        out[name] = list(dict.fromkeys(urls))
    return out


# --------------------------------------------------------------- 前端调用点

_IMPORT_RE = re.compile(r'import\s*\{([^}]*)\}\s*from\s*"[^"]*api/client"', re.S)


def load_callers() -> dict[str, set[str]]:
    """前端文件（相对 web/src）→ 它 import 且真正调用的 client 函数名。"""
    out: dict[str, set[str]] = {}
    for dp, _dn, fn in os.walk(WEB_SRC):
        for f in fn:
            if not f.endswith((".ts", ".tsx")):
                continue
            if ".test." in f:
                continue
            p = os.path.join(dp, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    s = fh.read()
            except OSError:
                continue
            names: set[str] = set()
            for blk in _IMPORT_RE.findall(s):
                for n in blk.split(","):
                    n = n.strip().removeprefix("type ").strip()
                    if n and n[0].islower():
                        names.add(n)
            for n in re.findall(r"\bclient\.(\w+)\(", s):
                names.add(n)
            if names:
                out[os.path.relpath(p, WEB_SRC).replace(os.sep, "/")] = names
    return out


# --------------------------------------------------------------- 组件依赖闭包

# 本地组件 import：`import { X } from "@/components/..."` / `from "./X"` / `from "../X"`
_LOCAL_IMPORT = re.compile(r'import\s+(?:type\s+)?\{([^}]*)\}\s*from\s*"((?:@/|\.\.?/)[^"]+)"')


def build_local_deps() -> dict[str, set[str]]:
    """前端文件 → 它 import 的**本地模块**（相对 web/src，已解析扩展名）。

    为什么需要：共享组件（`components/voice-studio/AbChainCard.tsx`）自己调
    `sound.audition` 的端点，但**真正渲染它的**是 `pages/Voices/VoicesPage.tsx`。
    只看「谁调了 API」会把两者割裂开 —— 2026-09-21 就是这样漏掉了音色库那个 404：
    审计只看调用点，`VoicesPage.tsx` 一个 API 函数都没直接调，于是它压根不出现在
    「内核 + 可关」混合列表里，缺口对人不可见。补上这层 import 闭包，渲染方才会
    继承被渲染组件的可关依赖。
    """
    deps: dict[str, set[str]] = {}
    for dp, _dn, fn in os.walk(WEB_SRC):
        for f in fn:
            if not f.endswith((".ts", ".tsx")) or ".test." in f:
                continue
            p = os.path.join(dp, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    s = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(p, WEB_SRC).replace(os.sep, "/")
            found: set[str] = set()
            for _names, spec in _LOCAL_IMPORT.findall(s):
                if spec.startswith("@/"):
                    base = spec[2:]
                else:
                    base = os.path.normpath(os.path.join(os.path.dirname(rel), spec)).replace(os.sep, "/")
                for cand in (f"{base}.tsx", f"{base}.ts", f"{base}/index.tsx", f"{base}/index.ts"):
                    if os.path.exists(os.path.join(WEB_SRC, cand)):
                        found.add(cand)
                        break
            if found:
                deps[rel] = found
    return deps


def plugin_closure(
    start: str,
    direct: dict[str, dict[str, set[str]]],
    deps: dict[str, set[str]],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """沿 import 图传递地收集 `start` 触及的所有插件。

    → (插件 id → 贡献它的函数名, 插件 id → 从 start 到贡献者的**完整路径文件集**)

    第二项必须给**完整路径**（含中间层），不能只给直接贡献者：
    门控常常写在中间层。2026-09-21 的实际例子 —— 入口是 `pages/Voices/index.tsx`，
    中间是 `pages/Voices/VoicesPage.tsx`（门控就写在这里），末端是
    `components/voice-studio/AbChainCard.tsx`（真正调 API 的）。只报末端的话，
    「有没有人门控」会得出「没人」的**错误结论**，把一个已修好的地方报成缺口。
    """
    # 父指针不动点：`parents[f]` = 所有能直接 import 到 f 的已访问文件。
    # 用不动点而不是「首次入队即定型」，因为一个文件可能被多条路径到达，
    # 只保留首条会让「路径上有没有门控」漏判。
    parents: dict[str, set[str]] = {start: set()}
    frontier = [start]
    while frontier:
        nxt_frontier: list[str] = []
        for cur in frontier:
            for nxt in deps.get(cur, ()):
                ps = parents.setdefault(nxt, set())
                if cur not in ps:
                    ps.add(cur)
                    nxt_frontier.append(nxt)
        frontier = nxt_frontier

    plugs: dict[str, set[str]] = collections.defaultdict(set)
    chain: dict[str, set[str]] = collections.defaultdict(set)

    def path_to(target: str) -> set[str]:
        """从 start 到 target 的路径上的所有文件（含两端）。"""
        out: set[str] = set()
        stack2 = [target]
        while stack2:
            cur = stack2.pop()
            if cur in out:
                continue
            out.add(cur)
            stack2.extend(parents.get(cur, ()))
        return out

    for f, mapping in direct.items():
        if f not in parents:
            continue
        for plug, fns in mapping.items():
            plugs[plug] |= fns
            chain[plug] |= path_to(f)
    return plugs, chain


# --------------------------------------------------------------- 主流程

def analyze() -> dict:
    module2plugin, is_core, all_ids = load_plugins()
    routes = load_routes()
    funcs = load_client_funcs()
    callers = load_callers()

    def resolve(name: str) -> tuple[list[str], list[tuple[str, str | None]]]:
        """→ (未定位的 URL, [(url, 插件 id 或 None)])"""
        urls = funcs.get(name) or []
        unresolved: list[str] = []
        resolved: list[tuple[str, str | None]] = []
        if name in URL_BUILDERS:
            # 拼装函数：不是端点，归属无从谈起，**不是**「未定位」
            return unresolved, resolved
        if not urls:
            # 没抽到 URL 本身就是「未定位」——不能当没这回事
            unresolved.append(f"<抽不到 URL: {name}>")
            return unresolved, resolved
        for u in urls:
            mod = path_to_module(u, routes)
            if mod is None:
                unresolved.append(u)
                continue
            plug = module2plugin.get(mod)
            if plug is None:
                unresolved.append(f"{u} (模块 {mod} 无插件归属)")
                continue
            resolved.append((u, plug))
        return unresolved, resolved

    # 每个函数的归属
    fn_owner: dict[str, list[tuple[str, str]]] = {}
    fn_unresolved: dict[str, list[str]] = {}
    for name in sorted(funcs):
        un, res = resolve(name)
        if un:
            fn_unresolved[name] = un
        if res:
            fn_owner[name] = res

    # 每个前端文件：**直接**用到哪些插件
    direct: dict[str, dict[str, set[str]]] = {}
    file_unknown: dict[str, list[tuple[str, str]]] = {}
    for rel, names in sorted(callers.items()):
        plugs: dict[str, set[str]] = collections.defaultdict(set)
        unknown: list[tuple[str, str]] = []
        for n in sorted(names):
            for u in fn_unresolved.get(n, []):
                unknown.append((n, u))
            for u, plug in fn_owner.get(n, []):
                plugs[plug].add(n)
        if plugs:
            direct[rel] = {p: set(v) for p, v in plugs.items()}
        if unknown:
            file_unknown[rel] = unknown

    # 每个前端文件（含「只 import 组件、自己不打 API」的那些）：**含传递**用到哪些插件。
    # 这一层是 2026-09-21 补的关键一环 —— 少了它，`pages/Voices/VoicesPage.tsx`
    # 这种「一个 API 都不直接调、全靠共享组件」的核心页在报告里**完全不可见**，
    # 于是「核心页渲染了可关组件」这个缺口只能靠人肉 grep 追。见 build_local_deps 的说明。
    deps = build_local_deps()
    candidates = set(direct) | set(deps)
    file_rows = []
    for rel in sorted(candidates):
        plugs, chain = plugin_closure(rel, direct, deps)
        unknown = file_unknown.get(rel, [])
        if not plugs and not unknown:
            continue
        core_plugs = sorted(p for p in plugs if is_core.get(p))
        gate_plugs = sorted(p for p in plugs if not is_core.get(p))
        file_rows.append(
            {
                "file": rel,
                "core": core_plugs,
                "gated": gate_plugs,
                "funcs": {p: sorted(v) for p, v in sorted(plugs.items())},
                "chain": {p: sorted(v) for p, v in sorted(chain.items())},
                "direct": rel in direct,
                "unresolved": unknown,
                "mixed": bool(core_plugs and gate_plugs),
            }
        )

    # 全局未定位函数
    unknown_funcs = sorted(set(fn_unresolved))
    unknown_detail = [
        {"fn": n, "url": u} for n, us in sorted(fn_unresolved.items()) for u in us
    ]

    # ★ 核心判据：核心路由页有没有「裸渲染可关组件」
    entries = load_route_entries()
    mentions = load_source_mentions(all_ids)
    ungated = find_ungated_core_entries(file_rows, entries, is_core, mentions, deps)

    return {
        "plugins_total": len(all_ids),
        "funcs_total": len(funcs),
        "funcs_resolved": len(fn_owner),
        "module2plugin": module2plugin,
        "files": file_rows,
        "route_entries": entries,
        "ungated_core_entries": ungated,
        "unknown_funcs": unknown_funcs,
        "unknown_detail": unknown_detail,
    }


def load_source_mentions(plugin_ids: set[str]) -> dict[str, set[str]]:
    """前端文件 → 它在源码里**点名的**插件 id 集合。

    用途：判断「这个文件有没有为某插件做门控」。要门控就必须写出插件 id
    （`pluginVisible(catalog, "sound.audition")`），所以「源码里提到过这个 id」
    是「门控存在」的可靠必要条件 —— 比解析 AST 简单得多，且不会漏。
    注意它只是**必要**条件不是充分条件：写了 id 但没用它控制渲染的假门控由
    `web/src/pages/Voices/index.gate.test.ts` 那类静态门禁去钉。
    """
    out: dict[str, set[str]] = {}
    for dp, _dn, fn in os.walk(WEB_SRC):
        for f in fn:
            if not f.endswith((".ts", ".tsx")) or ".test." in f:
                continue
            p = os.path.join(dp, f)
            try:
                with open(p, encoding="utf-8") as fh:
                    s = fh.read()
            except OSError:
                continue
            hit = {pid for pid in plugin_ids if f'"{pid}"' in s or f"'{pid}'" in s}
            if hit:
                out[os.path.relpath(p, WEB_SRC).replace(os.sep, "/")] = hit
    return out


def find_ungated_core_entries(
    file_rows: list[dict],
    entries: dict[str, str],
    is_core: dict[str, bool],
    mentions: dict[str, set[str]],
    deps: dict[str, set[str]],
) -> list[dict]:
    """找出「核心路由页 → 传递依赖可关插件 → 却没人为它门控」的文件。

    这是本脚本的**核心判据**，直指 2026-09-21 那个真实缺口：
    `/voices` 是 `core.voices`（路由恒注册），却渲染了打 `sound.audition` 的组件。
    关掉试音间 → 音色库页照旧渲染那几个面板 → 点一下 404。

    判定：对每个 **core 插件的路由入口**，看它闭包里有没有可关插件 P；
    有的话，再看「从入口到 P 的路径上**有没有任何文件点过 P 的 id**」——
    点过 = 它在某处做了门控（自己门、或让下游组件自己门）；
    一个都没点过 = **没人管**，就是缺口。

    （下游组件若自带门控 —— 比如组件内部 `if (!pluginVisible(...)) return null` ——
    也算「点过」，所以不会误报。）
    """
    by_file = {r["file"]: r for r in file_rows}
    problems: list[dict] = []
    for entry, pid in sorted(entries.items()):
        if not is_core.get(pid):
            continue  # 非核心路由：整条路由会随插件消失，不需要页内门控
        row = by_file.get(entry)
        if not row or not row["gated"]:
            continue
        # 这条链上（入口 + 中间层 + 末端）任何一个点了该插件 id，都算做了门控
        for p in row["gated"]:
            # 该插件的门控是否出现在**链上任何一环**（含入口自身与所有中间层）
            chain_of_p = {entry} | set(row["chain"].get(p, ()))
            if not any(p in mentions.get(f, set()) for f in chain_of_p):
                problems.append(
                    {
                        "entry": entry,
                        "route_plugin": pid,
                        "gated": p,
                        "funcs": row["funcs"].get(p, []),
                        "chain": sorted(chain_of_p),
                    }
                )
    return problems


def load_baseline() -> set[str]:
    if not os.path.exists(BASELINE):
        return set()
    with open(BASELINE, encoding="utf-8") as fh:
        return set(json.load(fh).get("known_unresolved", []))


def main() -> int:
    # 输出编码不是装饰（2026-09-21 实测）：Windows 下 stdout 被**重定向**时（管道/文件 ——
    # pytest 的子进程、CI 日志、`> out.txt` 都是）按 ANSI(cp936) 编码，而本文件要打印的
    # `⚠️` / `✓` / `✗` 是 GBK **之外**的字 → UnicodeEncodeError。
    # 症状极具误导性：**判定其实通过，退出码却是 1**（门禁假红），且报告只打了一半
    # （崩在第 615 行，后面的章节全丢了）。控制台直连时不触发（走 WriteConsoleW），
    # 所以它只在重定向/子进程里现形。真切不了也无所谓，不该因编码设置失败而挂掉。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="门禁模式：有未登记的问题就 exit 1")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    r = analyze()

    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0

    print(f"插件 {r['plugins_total']} 个；client.ts 导出函数 {r['funcs_total']} 个，"
          f"其中 {r['funcs_resolved']} 个能定位到插件")
    print(f"清单里声明了 {len(r['route_entries'])} 条路由入口（已解析到具体文件）")
    print()

    ungated = r["ungated_core_entries"]
    print(f"=== ★★ 核心路由页裸渲染可关组件（{len(ungated)} 处）===")
    print("    核心页路由恒在，可关插件的端点却会消失 —— 这些地方用户会点出 404。")
    print("    修法：在入口页用 pluginVisible(catalog, \"<插件id>\") 把对应块条件渲染。\n")
    if not ungated:
        print("    （无）\n")
    for u in ungated:
        print(f"## {u['entry']}  （路由属 {u['route_plugin']}，恒注册）")
        print(f"    裸用了可关插件 [{u['gated']}] 的端点：{', '.join(u['funcs'])}")
        print(f"    依赖链（无人点过该插件 id）：{' → '.join(u['chain'])}")
        print()

    mixed = [f for f in r["files"] if f["mixed"]]
    # 其中「自己一个 API 都不直接调、全靠共享组件」的那些最危险：整页看着是核心页，
    # 缺口藏在渲染的组件里，人肉 review 基本必漏。单独顶出来。
    hidden = [f for f in mixed if not f["direct"]]
    print(f"=== ★ 同时用到「内核」与「可关」能力的文件（{len(mixed)} 个）===")
    print("    这些文件就是「核心页里嵌可关组件」的候选 —— 逐个人工确认要不要门控。\n")
    if hidden:
        print(f"    ⚠️ 其中 {len(hidden)} 个**自己不直接调 API**，可关依赖来自共享组件（最易漏）：")
        for f in hidden:
            print(f"       {f['file']}")
        print()
    for f in mixed:
        tag = "（含传递依赖）" if not f["direct"] else ""
        print(f"## {f['file']}{tag}")
        print(f"    内核: {', '.join(f['core'])}")
        print(f"    可关: {', '.join(f['gated'])}")
        for p in f["gated"]:
            print(f"        [{p}] {', '.join(f['funcs'][p])}")
            for src in f["chain"].get(p, ()):
                print(f"             ↑ 由 {src} 带入")
        if f["unresolved"]:
            for n, u in f["unresolved"]:
                print(f"    [未定位] {n}  {u}")
        print()

    pure_gated = [f for f in r["files"] if f["gated"] and not f["core"]]
    print(f"=== 纯可关能力的文件（{len(pure_gated)} 个，整页/整块可关）===")
    for f in pure_gated:
        tag = "" if f["direct"] else "  (含传递依赖)"
        print(f"    {f['file']}  <- {', '.join(f['gated'])}{tag}")
    print()

    print(f"=== 未定位的函数（{len(r['unknown_funcs'])} 个 / {len(r['unknown_detail'])} 条）===")
    for d in r["unknown_detail"]:
        print(f"    {d['fn']:28s} {d['url']}")
    print()

    if args.check:
        known = load_baseline()
        new = [n for n in r["unknown_funcs"] if n not in known]
        gone = sorted(known - set(r["unknown_funcs"]))
        ok = True
        if ungated:
            ok = False
            print("✗ 核心路由页裸渲染可关组件 —— 关掉那个插件后用户会点出 404：")
            for u in ungated:
                print(f"    {u['entry']}  [{u['gated']}]  链: {' → '.join(u['chain'])}")
            print("    修法：入口页用 pluginVisible(catalog, \"<插件id>\") 条件渲染对应块。")
            print()
        if new:
            ok = False
            print("✗ 出现未登记的「未定位」函数 —— 归属未知就没法判断该不该门控：")
            for n in new:
                detail = ", ".join(d["url"] for d in r["unknown_detail"] if d["fn"] == n)
                print(f"    {n}  -> {detail}")
            print()
        if gone:
            print("⚠ 基线里这些已修好，请从 endpoint_ownership_baseline.json 删掉：")
            for n in gone:
                print(f"    {n}")
            print()
        if ok:
            print(f"✓ 无核心页裸渲染可关组件；未定位函数全在基线内（{len(known)} 条）")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
