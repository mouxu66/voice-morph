"""接口契约守卫：客户端发给 worker 的字段，必须是该端点**真的读**的字段。

起因（2026-09-22）：`cascade_stream` 给 `/tts` 与 `/tts_stream` 发了 `"fast": True`，
但这两个端点根本不读它 —— `fast` 是 `_transcribe()`（Whisper 解码）的参数，
只有 `/transcribe` 认。于是那两处是**死参数**：不报错、不生效、只误导读者
（会让人以为「传了 fast 就真的 fast 了」）。详见 docs/犯错指南.md §8.38。

覆盖全仓「后端 → worker」的 3 条链路：
  · cascade_stream.py —— requests.post(self.base + "/x", json={…})
  · qwen3_tts.py      —— _post("/x", payload, …)
  · finetune.py       —— _worker_post("/x", payload, …)

字段提取做了两件「完整性」工作 —— 少任何一件都会**假红**：
  ① **跟随委托**：`/analyze` 把 body 交给 `_analyze_blocking(body)`，字段在那里面读；
  ② **白名单展开**：`/tts` 通过 `_gen_kwargs(body)` 读一组动态 key
     （`for k in ("do_sample", …)`），该白名单要并进该端点的接受集。
"""

from __future__ import annotations

import ast
from pathlib import Path

_M2 = Path(__file__).resolve().parents[1]
_WORKER = _M2 / "qwen3_tts_service.py"
_CLIENTS = ("cascade_stream.py", "qwen3_tts.py", "finetune.py")
_HTTP = ("post", "get", "put", "patch")
_WRAPPERS = ("_post", "_worker_post")


# ---------------- worker 侧 ----------------


def _functions(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _decorator_path(node: ast.AST) -> str | None:
    """取 `@app.post("/x")` 里的 "/x"。"""
    for dec in getattr(node, "decorator_list", []):
        if not isinstance(dec, ast.Call):
            continue
        fn = dec.func
        if not (isinstance(fn, ast.Attribute) and fn.attr in _HTTP):
            continue
        if dec.args and isinstance(dec.args[0], ast.Constant):
            v = dec.args[0].value
            if isinstance(v, str):
                return v
    return None


def _str_seq(node: ast.AST) -> set[str] | None:
    """取 `("a", "b")` 这种纯字符串序列；含非字符串元素则返回 None。"""
    if not isinstance(node, (ast.Tuple, ast.List)):
        return None
    out: set[str] = set()
    for e in node.elts:
        if not (isinstance(e, ast.Constant) and isinstance(e.value, str)):
            return None
        out.add(e.value)
    return out


def _collect_fields(node: ast.AST, funcs: dict[str, ast.AST], seen: set[str]) -> set[str]:
    """递归收集「该函数读了 body 的哪些字段」。"""
    # ① 变量 key 的白名单：`for k in ("a", "b")` / `k = ("a", "b")`
    lists: dict[str, set[str]] = {}
    for sub in ast.walk(node):
        if isinstance(sub, ast.For) and isinstance(sub.target, ast.Name):
            seq = _str_seq(sub.iter)
            if seq:
                lists[sub.target.id] = seq
        elif (
            isinstance(sub, ast.Assign)
            and len(sub.targets) == 1
            and isinstance(sub.targets[0], ast.Name)
        ):
            seq = _str_seq(sub.value)
            if seq:
                lists[sub.targets[0].id] = seq

    out: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        # body.get("X") / body.get(K)
        if isinstance(fn, ast.Attribute) and fn.attr == "get":
            if isinstance(fn.value, ast.Name) and fn.value.id == "body" and sub.args:
                a0 = sub.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    out.add(a0.value)
                elif isinstance(a0, ast.Name) and a0.id in lists:
                    out |= lists[a0.id]
            continue
        # ② 跟随委托：f(body) → 递归进 f
        if isinstance(fn, ast.Name) and fn.id in funcs and fn.id not in seen:
            if any(isinstance(a, ast.Name) and a.id == "body" for a in sub.args):
                seen.add(fn.id)
                out |= _collect_fields(funcs[fn.id], funcs, seen)
    return out


def worker_endpoints() -> dict[str, set[str]]:
    """{端点路径: 该端点读的字段集}。"""
    tree = ast.parse(_WORKER.read_text(encoding="utf-8"))
    funcs = _functions(tree)
    out: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        path = _decorator_path(node)
        if path:
            out[path] = _collect_fields(node, funcs, set())
    return out


# ---------------- client 侧 ----------------


def _url_tail(node: ast.AST) -> str | None:
    """从 `self.base + "/tts"` 或裸字符串取端点路径。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.right, ast.Constant):
        v = node.right.value
        if isinstance(v, str):
            return v
    return None


def _dict_fields(node: ast.AST) -> set[str] | None:
    """字面量 dict 的 key 集；不是字面量则 None。"""
    if isinstance(node, ast.Dict):
        return {
            k.value
            for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
    return None


def client_calls() -> list[tuple[str, int, str, set[str] | None]]:
    """[(文件, 行号, 端点, 字段集)] —— 字段集为 None 表示 payload 不是字面量。"""
    rows: list[tuple[str, int, str, set[str] | None]] = []
    for fname in _CLIENTS:
        tree = ast.parse((_M2 / fname).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            path: str | None = None
            fields: set[str] | None = None
            # requests.post(url, json={…})
            # ★ 必须限定 `requests.` 前缀：`@router.post("/ft/upload")` 也是
            #   Attribute(attr="post")，只看 attr 会把**路由定义**误判成 HTTP 调用。
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr in _HTTP
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "requests"
            ):
                path = _url_tail(node.args[0])
                for kw in node.keywords:
                    if kw.arg == "json":
                        fields = _dict_fields(kw.value)
            elif isinstance(fn, ast.Name) and fn.id in _WRAPPERS and len(node.args) >= 2:
                path = _url_tail(node.args[0])
                fields = _dict_fields(node.args[1])
            if path and path.startswith("/"):
                rows.append((fname, node.lineno, path, fields))
    return sorted(rows)


# ---------------- 守卫 ----------------


def test_guard_actually_parsed_something():
    """自检：解析器必须真扫到东西，否则下面几条会「空跑即绿」。

    ★ 这不是形式主义 —— 守卫假绿的常见形态就是「什么都没解析到，于是断言全通过」。
    """
    eps = worker_endpoints()
    calls = client_calls()
    literal = [c for c in calls if c[3] is not None]
    assert len(eps) >= 5, sorted(eps)
    assert len(calls) >= 6, calls
    assert len(literal) >= 4, calls
    assert sum(1 for f in eps.values() if f) >= 4, eps


def test_client_fields_are_accepted_by_worker():
    """★ 核心不变量：客户端发的每个字段，都必须是该端点真的读的字段。

    防的就是「死参数」—— 发了、不报错、也不生效。
    """
    eps = worker_endpoints()
    bad: list[str] = []
    for fname, lineno, path, fields in client_calls():
        if fields is None:  # payload 是变量，静态不可判
            continue
        if path not in eps:
            bad.append(f"{fname}:{lineno} {path} —— worker 里没有这个端点")
            continue
        unknown = sorted(fields - eps[path])
        if unknown:
            bad.append(
                f"{fname}:{lineno} {path} 发了端点不读的字段 {unknown}"
                f"（该端点读 {sorted(eps[path])}）"
            )
    assert not bad, "接口契约不一致：\n" + "\n".join(bad)


def test_fast_is_only_sent_to_transcribe():
    """`fast` 是 ASR（Whisper 解码）参数 —— 只有 /transcribe 认它。

    `/tts` 与 `/tts_stream` 是合成端点；TTS 侧**没有 fast 档**
    （faster 后端自带 CUDA Graph，已是最快路径）。
    """
    for fname, lineno, path, fields in client_calls():
        if fields is None:
            continue
        if path in ("/tts", "/tts_stream"):
            assert "fast" not in fields, f"{fname}:{lineno} {path} 不该发 fast：{sorted(fields)}"
    # 反向：/transcribe 的调用点里必须**至少有一处**仍发 fast
    # （防「删死参数」被做过头）。注意 finetune.py 那处走默认 fast=False，不发是合法的。
    tr = [f for _, _, p, f in client_calls() if p == "/transcribe" and f is not None]
    assert tr, "没扫到发往 /transcribe 的字面量 payload"
    assert any("fast" in f for f in tr), f"/transcribe 的调用点都没发 fast：{tr}"


def test_tts_endpoints_do_not_read_fast():
    """反向钉住 worker 侧：合成端点不该开始读 `fast`。

    若哪天真的要给 TTS 加 fast 档，那是一次**有意的新功能** ——
    届时必须同时改这条测试与 §8.38，而不是让它悄悄通过。
    """
    eps = worker_endpoints()
    for path in ("/tts", "/tts_stream"):
        assert "fast" not in eps.get(path, set()), f"{path} 读了 fast：{sorted(eps[path])}"


def test_extractor_follows_delegation():
    """回归：`/analyze` 把 body 交给 `_analyze_blocking(body)`，字段在那里面读。

    提取器若不跟随委托，`/analyze` 的字段集会**空掉** → 一旦客户端改发字面量就假红。
    """
    fields = worker_endpoints()["/analyze"]
    assert {"clips", "sim_threshold", "min_cluster_size"} <= fields, sorted(fields)


def test_extractor_expands_dynamic_whitelist():
    """回归：`/tts` 通过 `_gen_kwargs(body)` 读一组动态 key。

    白名单（`for k in ("do_sample", …)`）必须并进接受集，否则客户端发 `temperature`
    之类的合法字段会被判成死参数。
    """
    eps = worker_endpoints()
    for path in ("/tts", "/tts_stream"):
        assert "temperature" in eps[path], sorted(eps[path])


def test_route_definitions_are_not_mistaken_for_calls():
    """回归：`@router.post("/ft/upload")` 是**路由定义**，不是 HTTP 调用。

    只看 `attr == "post"` 会把 `finetune.py` 里 10 个 `/ft/*` 路由误判成 HTTP 调用
    （实测踩过：审计脚本因此报了 10 处假的「worker 里没有这个端点」）。
    所以 client 侧必须限定 `requests.` 前缀。
    """
    ft = [c for c in client_calls() if c[2].startswith("/ft/")]
    assert not ft, f"把路由定义当成 HTTP 调用了：{ft[:3]}"
