"""锁死后端对 file:// 页面（Electron 主窗口）的 CORS 放行契约。

为什么需要这个测试（2026-09-13，开源前审计 P2-5.3）：
    Electron 主窗口用 `win.loadFile(...)` 加载 SPA，页面来源是 `file://`，
    跨源请求 http://127.0.0.1:8000 时浏览器发出的 `Origin` 头是 `null`（或 `file://`）。
    历史上 main.cjs 因此关掉了 `webSecurity`（并开 `allowRunningInsecureContent`），
    代价是渲染层彻底失去同源策略。

    实测证明**不需要**：server.py 的 `LOCAL_ORIGIN_RE` 本就放行 `null` / `file://`
    并回显 ACAO，fetch 走标准 CORS 即可通过。于是 main.cjs 恢复了 webSecurity 默认值
    （见 web/electron/main.cjs 的 webPreferences 注释）。

    **这条链路现在是隐式耦合的**：谁要是收紧 LOCAL_ORIGIN_RE 或改掉默认 CORS 分支，
    桌面端会「能打开但所有接口都请求失败」——这种故障在开发态（Vite 5173 同源代理）
    根本复现不出来，只会在用户装完包后炸。本测试就是那根保险丝。

范围：只覆盖默认模式（VM_CORS_ORIGINS 未显式配置）。显式白名单是用户自己的选择，
不在本契约内。

⚠️ 探针路径为什么不用 /api/health（2026-09-13 踩过）：
    本测试要验的是**中间件**行为，与端点自身健康度无关。而 /api/health 里有
    `import torch`，CI 的瘦环境（只装 requirements-dev.txt）没有 torch → 端点抛
    ModuleNotFoundError。此时 Starlette 的 `ServerErrorMiddleware` 在 CORS
    **外层**生成 500，响应里**不带 ACAO**（浏览器同样读不到），于是断言会失败。
    第一版就是这么红的，靠 `tools/check.py --ci-fidelity` 在 push 前拦下来的
    —— 本机全绿、CI 全红。改用不依赖重库的探针路径后，两种环境行为一致。
"""

from __future__ import annotations

import config as cfg
import pytest
import server
from fastapi.testclient import TestClient

# 不存在的路径：由中间件层处理（有 web/dist 时被 SPA catch-all 兜成 200，
# 没有时是 404），两种情况都不碰任何重依赖，正好用来单测中间件。
PROBE_PATH = "/api/__cors_probe__"


@pytest.fixture(scope="module")
def client():
    if cfg.CORS_ORIGINS != ["*"]:
        pytest.skip(
            f"本机显式配置了 VM_CORS_ORIGINS={cfg.CORS_ORIGINS}，" "file:// 放行契约只覆盖默认模式"
        )
    # raise_server_exceptions=False：端点若 500，我们想看的是响应头而不是异常栈
    return TestClient(server.app, raise_server_exceptions=False)


@pytest.mark.parametrize("origin", ["null", "file://"])
def test_file_origin_get_is_allowed(client, origin):
    """file:// 页面的普通请求必须回显 ACAO，否则桌面端所有 fetch 都会被浏览器拦下。

    只断言「没被守卫拒绝 + ACAO 回显」，**不断言具体状态码**：
    探针路径的状态取决于 web/dist 在不在（200 / 404），那与 CORS 契约无关。
    """
    r = client.get(PROBE_PATH, headers={"Origin": origin})
    assert r.status_code != 403, "本机来源不应被跨站守卫拒绝"
    assert r.status_code < 500, "未处理异常不经过 CORS，探针应命中已处理的响应"
    assert r.headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize("origin", ["null", "file://"])
def test_file_origin_preflight_is_allowed(client, origin):
    """带 content-type 的 POST 会触发预检；预检不通 = 写操作全废。

    预检由 CORS 中间件直接短路，不会走到端点，因此这里用真实端点路径也安全。
    """
    r = client.options(
        "/api/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin


def test_no_credentials_header_for_file_origin(client):
    """ACAO 回显具体来源而非 `*`，且绝不带 credentials —— 回显 + credentials 是危险的组合。"""
    r = client.get(PROBE_PATH, headers={"Origin": "null"})
    assert r.headers.get("access-control-allow-origin") == "null"
    assert r.headers.get("access-control-allow-origin") != "*"
    assert r.headers.get("access-control-allow-credentials") is None


def test_remote_origin_is_rejected(client):
    """对照：远程网页不得调用本机接口（OriginGuard 直接 403）。

    这条与上面的放行是一体两面 —— 若某天为了"让 file:// 通过"把守卫改成放行一切，
    本测试会红。
    """
    r = client.get(PROBE_PATH, headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403


def test_localhost_dev_server_origin_is_allowed(client):
    """开发态：Vite dev server(5173) 与后端(8000) 不同源，同样依赖这条规则。"""
    r = client.get(PROBE_PATH, headers={"Origin": "http://localhost:5173"})
    assert r.status_code != 403
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


# ---------------- 手机遥控页（局域网来源，2026-09-29）----------------
# 为什么必须单列一组：手机浏览器打开 `http://<本机IP>:8000/remote.html` 时，
# **同源 POST 也会带 Origin 头**（Fetch 规范：非 GET/HEAD 一律带）。只放行 localhost
# 的症状极具误导性 ——
#   · 页面能打开（GET 不带 Origin，守卫看到空 Origin 就放行）；
#   · 一按「发到微信 / 按住说话」就 403，看起来像"连上了但什么都干不了"。
# 而且**预检也必须放行**：JSON POST 会先发 OPTIONS，预检由 CORS 层短路返回、
# 根本走不到 _OriginGuardMiddleware，所以只能改这个共用正则（multipart 上传不预检，
# 但 send_text/send_voice 是 JSON）。


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.168.1.5:8000",  # 家用路由默认网段
        "http://10.0.0.7:8000",
        "http://172.16.4.9:8000",
        "http://172.31.255.254:8000",  # 172.16/12 的上界
        "http://my-pc.local:8000",  # mDNS 主机名（Windows 上手机可能这么访问）
    ],
)
def test_lan_origin_is_allowed(client, origin):
    r = client.get(PROBE_PATH, headers={"Origin": origin})
    assert r.status_code != 403, f"局域网页面不该被跨站守卫拒绝：{origin}"
    assert r.headers.get("access-control-allow-origin") == origin

    pre = client.options(
        "/api/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert pre.status_code == 200, "预检不通 = 手机上所有 JSON 写操作全废"
    assert pre.headers.get("access-control-allow-origin") == origin


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example.com",
        "http://8.8.8.8:8000",  # 公网地址
        "http://172.32.0.1:8000",  # 172.16/12 之外（看着像私有网段）
        "http://169.254.1.1:8000",  # 链路本地（不在放行范围）
        "http://192.168.1.5.evil.com:8000",  # 把私有 IP 当前缀骗匹配
    ],
)
def test_private_lookalike_origins_are_still_rejected(client, origin):
    """对照：放行私有网段**不能**变成放行一切。

    最后一条是典型的"看着像内网"的伪造来源 —— 每条锚定在 `^…$` 与主机名整体匹配上，
    所以它必须仍然是 403。
    """
    r = client.get(PROBE_PATH, headers={"Origin": origin})
    assert r.status_code == 403, f"这个来源不该被放行：{origin}"
