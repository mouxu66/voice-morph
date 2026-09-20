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
