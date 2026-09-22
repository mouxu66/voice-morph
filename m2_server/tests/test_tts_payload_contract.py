"""接口契约守卫：客户端发给 worker 的字段，必须是该端点**真的读**的字段。

起因（2026-09-22）：`cascade_stream` 给 `/tts` 与 `/tts_stream` 发了 `"fast": True`，
但这两个端点根本不读它 —— `fast` 是 `_transcribe()`（Whisper 解码）的参数，
只有 `/transcribe` 认。于是那两处是**死参数**：不报错、不生效、只误导读者
（会让人以为「传了 fast 就真的 fast 了」）。详见 docs/犯错指南.md §8.38。

这里用 AST 两边对账：
  · worker 侧：每个 `@app.post("/x")` 函数体里 `body.get("…")` 读了哪些字段
  · client 侧：每个 `requests.post(self.base + "/x", json={…})` 发了哪些字段
断言「client 发的 ⊆ worker 读的」—— 死参数、拼写错误、端点改名都能被这一条抓住。
"""

from __future__ import annotations

import ast
from pathlib import Path

_M2 = Path(__file__).resolve().parents[1]
_WORKER = _M2 / "qwen3_tts_service.py"
_CLIENT = _M2 / "cascade_stream.py"

_HTTP_METHODS = ("post", "get", "put", "patch")


def _decorator_path(node: ast.AST) -> str | None:
    """取 `@app.post("/x")` 里的 "/x"。"""
    for dec in getattr(node, "decorator_list", []):
        if not isinstance(dec, ast.Call):
            continue
        fn = dec.func
        if not (isinstance(fn, ast.Attribute) and fn.attr in _HTTP_METHODS):
            continue
        if dec.args and isinstance(dec.args[0], ast.Constant):
            v = dec.args[0].value
            if isinstance(v, str):
                return v
    return None


def _body_get_fields(node: ast.AST) -> set[str]:
    """函数体内所有 `body.get("X")` 的 X（key 是变量时忽略，如 `_gen_kwargs` 的循环）。"""
    out: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if not (isinstance(f, ast.Attribute) and f.attr == "get"):
            continue
        if not (isinstance(f.value, ast.Name) and f.value.id == "body"):
            continue
        if sub.args and isinstance(sub.args[0], ast.Constant):
            v = sub.args[0].value
            if isinstance(v, str):
                out.add(v)
    return out


def worker_endpoints() -> dict[str, set[str]]:
    """{端点路径: 该端点读的字段集}。"""
    tree = ast.parse(_WORKER.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        path = _decorator_path(node)
        if path:
            out[path] = _body_get_fields(node)
    return out


def _url_tail(node: ast.AST) -> str | None:
    """从 `self.base + "/tts"` 或裸字符串取端点路径。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.right, ast.Constant):
        v = node.right.value
        if isinstance(v, str):
            return v
    return None


def client_payloads() -> dict[str, set[str]]:
    """{目标端点路径: 客户端发送的 json 字段集}（同一端点多次调用取并集）。"""
    tree = ast.parse(_CLIENT.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr in _HTTP_METHODS):
            continue
        if not node.args:
            continue
        path = _url_tail(node.args[0])
        if not path or not path.startswith("/"):
            continue
        fields: set[str] = set()
        for kw in node.keywords:
            if kw.arg == "json" and isinstance(kw.value, ast.Dict):
                for k in kw.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        fields.add(k.value)
        out.setdefault(path, set()).update(fields)
    return out


# ---------------- 守卫 ----------------


def test_guard_actually_parsed_something():
    """自检：解析器必须真扫到东西，否则下面两条会「空跑即绿」。

    ★ 这条不是形式主义 —— 守卫假绿的常见形态就是「什么都没解析到，于是断言全部通过」。
    """
    eps = worker_endpoints()
    cps = client_payloads()
    assert "/tts" in eps and "/transcribe" in eps, sorted(eps)
    assert len(eps) >= 4, sorted(eps)
    assert "/tts" in cps and "/transcribe" in cps, sorted(cps)
    assert len(cps) >= 3, sorted(cps)


def test_client_fields_are_accepted_by_worker():
    """★ 核心不变量：客户端发的每个字段，都必须是该端点真的读的字段。

    防的就是「死参数」—— 发了、不报错、也不生效。
    """
    eps = worker_endpoints()
    bad: list[str] = []
    for path, fields in client_payloads().items():
        if path not in eps:
            bad.append(f"{path}: worker 里没有这个端点")
            continue
        unknown = sorted(fields - eps[path])
        if unknown:
            bad.append(
                f"{path}: 客户端发了端点不读的字段 {unknown}"
                f"（该端点读的是 {sorted(eps[path])}）"
            )
    assert not bad, "接口契约不一致：\n" + "\n".join(bad)


def test_fast_is_only_sent_to_transcribe():
    """`fast` 是 ASR（Whisper 解码）参数 —— 只有 /transcribe 认它。

    `/tts` 与 `/tts_stream` 是合成端点；TTS 侧**没有 fast 档**
    （faster 后端自带 CUDA Graph，已是最快路径）。
    """
    cps = client_payloads()
    assert "fast" in cps.get("/transcribe", set()), sorted(cps)
    for path in ("/tts", "/tts_stream"):
        assert "fast" not in cps.get(path, set()), f"{path} 不该发 fast：{sorted(cps[path])}"


def test_tts_endpoints_do_not_read_fast():
    """反向钉住 worker 侧：合成端点不该开始读 `fast`。

    若哪天真的要给 TTS 加 fast 档，那是一次**有意的新功能** ——
    届时必须同时改这条测试与 §8.38，而不是让它悄悄通过。
    """
    eps = worker_endpoints()
    for path in ("/tts", "/tts_stream"):
        assert "fast" not in eps.get(path, set()), f"{path} 读了 fast：{sorted(eps[path])}"
