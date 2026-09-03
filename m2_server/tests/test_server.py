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
    resp = client.get("/api/pipeline/status")   # 无 torch/无副作用端点
    assert resp.status_code == 200
    # 默认未配置 token 时不应有 401 拦截
    assert server.cfg.API_TOKEN == ""


def test_health_endpoint_reachable():
    pytest.importorskip("torch")  # /api/health 内部 import torch；无 torch 则跳过
    import server
    client = TestClient(server.app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"
