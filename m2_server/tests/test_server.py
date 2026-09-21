import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from fastapi.testclient import TestClient  # noqa: E402


def test_server_imports_and_routes_registered():
    import server

    # include_router 在当前 FastAPI 版本是惰性挂载（_IncludedRouter），
    # 枚举 app.routes 看不到子路由路径；改用真实请求验证路由注册成功。
    client = TestClient(server.app)
    resp = client.get("/api/pipeline/status")  # 无 torch/无副作用端点
    assert resp.status_code == 200
    # 默认未配置 token 时不应有 401 拦截
    assert server.cfg.API_TOKEN == ""


def test_health_endpoint_reachable():
    """`/health` 在**装了** torch 的环境里正常。

    原写法带 `pytest.importorskip("torch")` —— 在瘦环境（CI）里整条被跳过，
    于是「缺 torch 时 `/health` 会不会 500」从来没被验过。第 5 步把 torch 划成
    extra 之后这条路径变成常态，所以下面另有两条**显式模拟缺 torch** 的用例。
    """
    import server

    client = TestClient(server.app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"


def test_api_paths_are_never_served_as_the_spa():
    """`/api/*`、`/v1/*` 没匹配上时必须 404 + JSON，不能兜回 SPA 首页。

    2026-09-21 实测（在**已安装副本**上跑出来的）：关掉 `sound.rvc-live` 后
    `/api/rvc/live/status` 返回的是 `200 text/html`（index.html）—— 因为最后的
    `@app.get("/{full_path:path}")` 会吞掉一切没匹配上的 GET 路径，包括 `/api/*`。
    后果比 404 难查得多：前端 `jsonFetch` 抛 `Unexpected token '<'`（像前端 bug）、
    Network 面板没有任何红、而验收清单里「点一下应该 404」的判据永远不成立。
    同一条也顺便盖住更早就有的问题：拼错任何 `/api/...` 路径都返回 200 HTML。
    """
    import server

    client = TestClient(server.app)
    for path in ("/api/nonexistent/whatever", "/v1/nonexistent"):
        resp = client.get(path)
        assert resp.status_code == 404, f"{path} 没 404，而是 {resp.status_code}"
        ctype = resp.headers.get("content-type", "")
        assert ctype.startswith("application/json"), f"{path} 返回了 {ctype}（不能是 HTML）"
        assert "detail" in resp.json()


def test_spa_fallback_still_serves_the_frontend(monkeypatch, tmp_path):
    """修了上面那条**不能把前端托管弄坏**：真页面路径/静态资源照旧。

    用一个临时 dist 顶掉真实构建产物：既不依赖 `web/dist` 在不在（CI 里它没入库），
    也免得"该文件恰好存在"把断言变成恒真。`_spa` 读的是模块全局，所以能替换。
    """
    import server

    if server._web_dist is None:
        pytest.skip("本环境没有构建好的前端（web/dist 缺失）→ SPA 兜底根本没注册")
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA_SENTINEL</html>", encoding="utf-8")
    (dist / "assets" / "a.js").write_text("// JS_SENTINEL", encoding="utf-8")
    monkeypatch.setattr(server, "_web_dist", dist)
    client = TestClient(server.app)

    resp = client.get("/live")  # 前端路由：交给 index.html
    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    assert "SPA_SENTINEL" in resp.text

    asset = client.get("/assets/a.js")  # 真文件：直接发文件，不走 index.html
    assert asset.status_code == 200
    assert "JS_SENTINEL" in asset.text


def test_health_degrades_when_torch_is_missing(monkeypatch):
    """缺 torch → 200 + `cuda: null`，**不是** 500。

    为什么要显式模拟而不是「靠 CI 瘦环境自然覆盖」：CI 装不装 torch 取决于
    `requirements-dev.txt`，哪天有人把它补进去，这条路径就**悄悄不再被测**了。
    这里把 `_try_import_torch` 打成返回 None，与解释器里有没有 torch 无关。
    """
    import server
    import system_api

    monkeypatch.setattr(system_api, "_try_import_torch", lambda: None)
    resp = TestClient(server.app).get("/api/health")
    assert resp.status_code == 200, (
        "缺 torch 不能 500 —— 前端会把「没装可选依赖」误报成「服务未启动」"
    )
    assert resp.json() == {"status": "ok", "cuda": None}


def test_diagnose_degrades_when_torch_is_missing(monkeypatch):
    """`/diagnose` 是「哪里缺」的面板：缺 torch 时要**报出这一项**，而不是整个 500。

    这是比 `/health` 更硬的理由 —— 一个专门回答「哪里缺」的端点，在缺得最多的时候
    自己打不开，等于把最需要它的人挡在门外。
    """
    import server
    import system_api

    monkeypatch.setattr(system_api, "_try_import_torch", lambda: None)
    resp = TestClient(server.app).get("/api/diagnose")
    assert resp.status_code == 200, "缺 torch 时体检端点必须还能答「缺什么」"
    body = resp.json()
    keys = {i["key"] for i in body["items"]}
    assert "torch" in keys, f"缺 torch 时应报一项 torch，实际 keys={sorted(keys)}"
    assert body["cuda"] is False
