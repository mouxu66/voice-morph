"""端点单测：GET /api/system/lan —— 遥控页二维码的数据源（issue #1 对症方案）。

端点本身不碰模型不碰盘，重点钉三件事：
  1. **选 IP 的判据**：本机跑 Mihomo TUN（fake-ip 198.18.x）+ Radmin VPN，候选里
     混着手机连不上的虚拟网卡地址 —— 判据是 RFC1918 三段，不是"第一个非回环"；
  2. **URL 口径**：与 remote.html 头部注释一致，设了 VM_API_TOKEN 才拼 ?api_key=；
  3. **无网降级**：一个候选都没有时仍 200（ip/url 为 null），不能让设置面板 500。
"""

import config as cfg
import pytest
import system_api
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    import server

    return TestClient(server.app, raise_server_exceptions=False)


def test_shape_and_port(client, monkeypatch):
    monkeypatch.setattr(system_api, "_lan_candidates", lambda: ["192.168.1.5"])
    monkeypatch.setattr(cfg, "API_TOKEN", "")
    r = client.get("/api/system/lan")
    assert r.status_code == 200
    body = r.json()
    assert body["ip"] == "192.168.1.5"
    assert body["port"] == cfg.SERVER_PORT
    assert body["url"] == f"http://192.168.1.5:{cfg.SERVER_PORT}/remote.html"
    assert body["token_set"] is False
    assert "VM_API_TOKEN" in body["note"]  # 未设 token 时提醒一句


def test_url_composition_skips_tun_fakeip_and_loopback(client, monkeypatch):
    """候选顺序 = 优先级，但 198.18.x（TUN fake-ip）与 127.x 都不能当选。"""
    monkeypatch.setattr(
        system_api,
        "_lan_candidates",
        lambda: ["198.18.0.5", "127.0.0.1", "192.168.1.23"],
    )
    monkeypatch.setattr(cfg, "API_TOKEN", "t0k3n")
    body = client.get("/api/system/lan").json()
    assert body["ip"] == "192.168.1.23"
    assert body["url"].endswith("/remote.html?api_key=t0k3n")
    assert body["token_set"] is True


def test_no_candidate_degrades_to_null(client, monkeypatch):
    """全虚拟网卡/没插网线：200 + ip/url=null，不 500（面板要能照常渲染）。"""
    monkeypatch.setattr(
        system_api, "_lan_candidates", lambda: ["198.18.0.5", "26.10.20.30"]
    )
    body = client.get("/api/system/lan").json()
    assert body["ip"] is None
    assert body["url"] is None


def test_loopback_only_via_real_candidates(monkeypatch):
    """_lan_candidates 本体的行为锚点：纯回环环境拿不到候选就返回空，不抛。"""
    import socket

    orig = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *a, **kw):
        if host == socket.gethostname() and kw.get("family", a[0] if a else None) == socket.AF_INET:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        return orig(host, port, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    # UDP connect 探测在 CI/离线环境可能失败也可能给回环 —— 两种都允许，关键是给 list
    result = system_api._lan_candidates()
    assert isinstance(result, list)


def test_rfc1918_matrix():
    """判据矩阵：三段放行，fake-ip/APIPA/CGNAT/回环/IPv6/垃圾串全部拒绝。"""
    yes = ["10.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.0.1", "192.168.50.23"]
    no = [
        "198.18.0.5",  # Mihomo fake-ip
        "169.254.1.1",  # APIPA
        "100.64.0.1",  # CGNAT（Tailscale 等）
        "26.10.20.30",  # Radmin VPN
        "127.0.0.1",  # 回环
        "::1",  # IPv6 回环
        "fe80::1",  # IPv6 链路本地
        "not-an-ip",
        "",
    ]
    for ip in yes:
        assert system_api._is_rfc1918(ip), ip
    for ip in no:
        assert not system_api._is_rfc1918(ip), ip
